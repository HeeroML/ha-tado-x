"""Tests for the telemetry of the tado X client."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import gc
import json
import logging
import re
from typing import Any
import weakref

from aiohttp import ClientSession
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.tado_x.client import (
    SENSITIVE_KEYS,
    WARN_ONCE,
    DayType,
    ErrorRecord,
    Exchange,
    MemoryTokenStore,
    Power,
    PresenceSetting,
    QuickAction,
    QuotaTracker,
    ScheduleBlock,
    Setting,
    TadoXAuthError,
    TadoXClient,
    TadoXConnectionError,
    TadoXDeviceFlowError,
    TadoXError,
    TadoXForbiddenError,
    TadoXHome,
    TadoXHomeNotXError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    TariffUnit,
    Telemetry,
    Termination,
    TokenManager,
    TokenSet,
    WarnOnce,
    describe_command,
    describe_error,
    describe_tado_error,
    describe_value,
    endpoint_template,
    home_label,
    mask_serial,
    redact_text,
    safe_title,
    shape,
)
from custom_components.tado_x.client.const import (
    ENERGY_IQ_API_URL,
    HOPS_API_URL,
    MINDER_API_URL,
    MY_API_URL,
    TOKEN_URL,
)
from custom_components.tado_x.client.telemetry import safe_code
from tests.tado_mock import HOME_B, TadoCloudMock, entry_tokens, load_fixture

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
HOPS_B = f"{HOPS_API_URL}/homes/{HOME_B}"
MY_B = f"{MY_API_URL}/homes/{HOME_B}"
CLIENT_LOGGER = "custom_components.tado_x.client"


class _Local:
    """A local variable of a frame that raised."""


def _exchange(
    endpoint: str = "GET hops:/homes/{home}/rooms", **changes: Any
) -> Exchange:
    exchange = Exchange(
        at=datetime.now(UTC),
        method=endpoint.partition(" ")[0],
        endpoint=endpoint,
        status=200,
        duration_ms=182,
    )
    return replace(exchange, **changes)


def _client_messages(
    caplog: pytest.LogCaptureFixture, level: int | None = None
) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == CLIENT_LOGGER and (level is None or record.levelno == level)
    ]


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("get", f"{HOPS_B}/rooms", "GET hops:/homes/{home}/rooms"),
        (
            "GET",
            f"{HOPS_B}/rooms/3/schedule",
            "GET hops:/homes/{home}/rooms/{room}/schedule",
        ),
        (
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/devices/VA0000000101",
            "PATCH hops:/homes/{home}/roomsAndDevices/devices/{serial}",
        ),
        (
            "POST",
            f"{HOPS_B}/roomsAndDevices/devices/IB0000000001/identify",
            "POST hops:/homes/{home}/roomsAndDevices/devices/{serial}/identify",
        ),
        (
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/rooms/6",
            "PATCH hops:/homes/{home}/roomsAndDevices/rooms/{room}",
        ),
        (
            "GET",
            f"{MY_B}/zones/3/dayReport?date=2026-10-30",
            "GET my:/homes/{home}/zones/{zone}/dayReport?date",
        ),
        (
            "GET",
            f"{MY_B}/mobileDevices/5000001/settings",
            "GET my:/homes/{home}/mobileDevices/{mobile}/settings",
        ),
        ("GET", f"{MY_API_URL}/me", "GET my:/me"),
        ("GET", MY_API_URL, "GET my:/"),
        ("GET", "https://my.tado.com/other/7", "GET my:/other/{id}"),
        (
            "GET",
            f"{MINDER_API_URL}/homes/{HOME_B}/runningTimes"
            "?from=2026-10-30&to=2026-10-30",
            "GET minder:/homes/{home}/runningTimes?from&to",
        ),
        (
            "POST",
            f"{ENERGY_IQ_API_URL}/homes/{HOME_B}/meterReadings",
            "POST eiq:/homes/{home}/meterReadings",
        ),
        ("POST", TOKEN_URL, "POST login:/oauth2/token"),
        (
            "GET",
            f"{HOPS_B}/devices/12/zip-12345/x?a=1&a=2&b=",
            "GET hops:/homes/{home}/devices/{device}/{id}/x?a&b",
        ),
        (
            "GET",
            "https://example.invalid/things/42",
            "GET example.invalid:/things/{id}",
        ),
        ("GET", "/relative/7", "GET ?:/relative/{id}"),
    ],
)
def test_endpoint_template(method: str, url: str, expected: str) -> None:
    """Ids, serials and query values never reach an endpoint template."""
    assert endpoint_template(method, URL(url)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("", ""),
        ("No open window detected", "No open window detected"),
        (f"Timezone of home {HOME_B} not found", "Timezone of home # not found"),
        ("123 is short enough", "123 is short enough"),
        ("line one\nline two\r\n\tthree\x1b[0m", "line one line two three [0m"),
        ("contact user@example.invalid please", "contact <email> please"),
        (
            "open https://login.tado.com/oauth2/device?user_code=ABC123 now",
            "open <url> now",
        ),
        ("header Bearer abc.def", "header <redacted>"),
        ("token fake-access-token-1000002 expired", "token <redacted> expired"),
        ("id 00000000-0000-4000-8000-000000000001", "id <redacted>"),
        ("$.maxFlowTemperatureConstraints.min", "$.maxFlowTemperatureConstraints.min"),
        ("unauthorized_after_refresh", "unauthorized_after_refresh"),
        ("token ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", "token <redacted>"),
        ("key AbcdEFGHijklMNOPqrstUVWXyzab+cdef/gh==", "key <redacted>"),
        (
            "$.supportsFlowTemperatureOptimization",
            "$.supportsFlowTemperatureOptimization",
        ),
        ("showSwitchToAutoGeofencingButton", "showSwitchToAutoGeofencingButton"),
        (
            "GET hops:/homes/{home}/settings/flowTemperatureOptimization",
            "GET hops:/homes/{home}/settings/flowTemperatureOptimization",
        ),
        ("refresh_token=abcdefgh&client_id=x", "refresh_token=<redacted>&client_id=x"),
        ("grant_type=refresh_token", "grant_type=refresh_token"),
        ("www.example.com/path?token=abc", "www.example.com/path?token=<redacted>"),
        ('{"access_token": "abc", "a": 1}', '{"access_token": "<redacted>", "a": 1}'),
        ("Device_Code: xyz", "Device_Code: <redacted>"),
        ("callback?code=abc&state=x", "callback?code=<redacted>&state=x"),
        ("code=not.found title=Room", "code=not.found title=Room"),
        (
            "visit tado.com/device?user_code=ABCDEF",
            "visit tado.com/device?user_code=<redacted>",
        ),
        ("user code WDJB-MJHT entered", "user code <redacted> entered"),
        ("code 7LXZ-G2AB", "code <redacted>"),
        ("a real-time long-term view", "a real-time long-term view"),
    ],
)
def test_redact_text(text: str | None, expected: str | None) -> None:
    """Numbers, addresses, URLs and tokens are blanked; words are kept."""
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    ("code", "title", "expected"),
    [
        (
            "not.found",
            f"Timezone of home {HOME_B} not found",
            "code=not.found title=Timezone of home # not found",
        ),
        (
            "not.allowed",
            "Wohnzimmer Musterstrasse 1 ABCD-EFGH",
            "code=not.allowed title=<str len=36>",
        ),
        (
            None,
            "Room 'Wohnzimmer' of home 'Familie Mueller' not found",
            "title=<str len=53>",
        ),
        ("openWindow.notDetected", None, "code=openWindow.notDetected"),
        (f"user {HOME_B}", "", "code=<str len=12>"),
        (None, None, None),
    ],
)
def test_describe_tado_error(
    code: str | None, title: str | None, expected: str | None
) -> None:
    """Only known title templates are shown; any other title by its length."""
    assert describe_tado_error(code, title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Timezone of home 1000001 not found", "Timezone of home # not found"),
        ("Timezone of home 1000001 not found ", "Timezone of home # not found"),
        ("Timezone of home Haus B not found", "<str len=33>"),
        ("Kueche", "<str len=6>"),
    ],
)
def test_safe_title(title: str, expected: str) -> None:
    """A title passes only as a known template, after redact_text()."""
    assert safe_title(title) == expected


def test_redact_text_is_cut_to_the_limit() -> None:
    """A long text ends with an ellipsis at the limit."""
    assert redact_text("word " * 50) == f"{'word ' * 23}word…"
    assert len(redact_text("x" * 50, limit=10) or "") == 10
    assert redact_text("short", limit=10) == "short"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "bool"),
        (3, "int"),
        (1.5, "float"),
        ("DEPLETED", "DEPLETED"),
        ("NO_TEMPERATURE_CONTROL", "NO_TEMPERATURE_CONTROL"),
        ("VA0000000101", "VA…0101"),
        ("Wohnzimmer", "<str len=10>"),
        ("D12345", "<str len=6>"),
        ("X" * 41, "<str len=41>"),
        ("", "<str len=0>"),
        ({"a": 1, "b": 2}, "dict[2]"),
        ([1, 2, 3], "list[3]"),
        ((1,), "list[1]"),
        (b"raw", "bytes"),
    ],
)
def test_describe_value(value: object, expected: str) -> None:
    """Only types, lengths, constants and masked serials are described."""
    assert describe_value(value) == expected


@pytest.mark.parametrize(
    ("home_id", "label"), [(HOME_B, "home …0002"), (815, "home …815")]
)
def test_home_label(home_id: int, label: str) -> None:
    """A home is named by the last four digits of its id, never by its name."""
    assert home_label(home_id) == label


def test_mask_serial() -> None:
    """A serial keeps its type prefix and its last four characters."""
    assert mask_serial("SU0000000102") == "SU…0102"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("not.found", "not.found"),
        ("openWindow.notDetected", "openWindow.notDetected"),
        ("invalid_grant", "invalid_grant"),
        ("fake-access-token-1000002", "<str len=25>"),
        ("e400", "<str len=4>"),
        ("", "<str len=0>"),
    ],
)
def test_safe_code(code: str, expected: str) -> None:
    """Only dotted words pass as an error code."""
    assert safe_code(code) == expected


def test_shape_of_home_details_hides_personal_data() -> None:
    """The home details keep their structure and lose every personal value."""
    raw = load_fixture("home_1000002.json")

    result = shape(raw)

    assert isinstance(result, dict)
    assert len(raw) == 28
    assert len(result) == 26
    assert result["…"] == "+3 keys"
    assert result["id"] == "int"
    assert result["generation"] == "LINE_X"
    assert result["temperatureUnit"] == "CELSIUS"
    assert result["dateTimeZone"] == "<str len=13>"
    assert result["partner"] == "null"
    assert result["skills"] == {"list": 1, "item": "AUTO_ASSIST"}
    assert result["enabledFeatures"] == {"list": 0}
    assert result["incidentDetection"] == {"supported": "bool", "enabled": "bool"}
    for key in ("name", "contactDetails", "address", "geolocation"):
        assert result[key] == "<redacted>"
    text = json.dumps(result)
    for secret in ("Haus", "Test User", "user@example", "Musterstr", "DEU", "50.0"):
        assert secret not in text


@pytest.mark.parametrize(
    "fixture",
    ["me.json", "mobile_devices.json", "device_authorize.json", "token.json"],
)
def test_shape_hides_tokens_and_personal_data(fixture: str) -> None:
    """Accounts, phones, codes and tokens leave no value in their shape."""
    text = json.dumps(shape(load_fixture(fixture)))
    for secret in (
        "user@example",
        "Test User",
        "Phone",
        "Model1",
        'atHome": "bool',
        "fake",
        "ABC123",
        "00000000-0000",
        "login.tado.com",
    ):
        assert secret not in text


def test_shape_of_the_topology_masks_serials() -> None:
    """Serial numbers appear masked, room names not at all."""
    result = shape(load_fixture("rooms_and_devices.json"), depth=5)
    assert isinstance(result, dict)
    room = result["rooms"]["item"]
    assert room["roomName"] == "<redacted>"
    assert room["devices"]["item"]["serialNumber"] == "VA…0101"
    assert "Wohnzimmer" not in json.dumps(result)


def test_shape_keeps_constants_of_known_enum_keys_only() -> None:
    """An upper-case value is kept only under a key known to hold an enum."""
    payload = {
        "rooms": [
            {
                "id": 3,
                "label": "KUECHE",
                "nickname": "WOHNZIMMER",
                "ssid": "HOME",
                "title": "BAD",
                "type": "HEATING",
                "setting": {"power": "ON"},
                "connection": {"state": "CONNECTED"},
                "serialNo": "VA0000000101",
            }
        ],
        "weatherState": {"value": "CLOUDY_PARTLY"},
        "freshness": {"value": "FAIR"},
        "owner": {"value": "HANS"},
        "skills": ["AUTO_ASSIST"],
        "tags": ["MUELLER"],
        "callForHeat": {"dataIntervals": [{"value": "LOW"}]},
    }

    assert shape(payload) == {
        "rooms": {
            "list": 1,
            "item": {
                "id": "int",
                "label": "<str len=6>",
                "nickname": "<str len=10>",
                "ssid": "<str len=4>",
                "title": "<str len=3>",
                "type": "HEATING",
                "setting": "dict[1]",
                "connection": "dict[1]",
                "serialNo": "VA…0101",
            },
        },
        "weatherState": {"value": "CLOUDY_PARTLY"},
        "freshness": {"value": "FAIR"},
        "owner": {"value": "<str len=4>"},
        "skills": {"list": 1, "item": "AUTO_ASSIST"},
        "tags": {"list": 1, "item": "<str len=7>"},
        "callForHeat": {"dataIntervals": {"list": 1, "item": "dict[1]"}},
    }
    deeper = shape(payload, depth=4)
    assert isinstance(deeper, dict)
    assert deeper["rooms"]["item"]["setting"] == {"power": "ON"}
    assert deeper["rooms"]["item"]["connection"] == {"state": "CONNECTED"}
    assert deeper["callForHeat"]["dataIntervals"]["item"] == {"value": "LOW"}
    assert shape("HOME") == "<str len=4>"


def test_shape_limits() -> None:
    """Containers below the depth are only described; keys are made safe."""
    nested = {"a": {"b": {"c": {"d": 1}}}, "list": [[1, 2]]}
    assert shape(nested) == {
        "a": {"b": {"c": "dict[1]"}},
        "list": {"list": 1, "item": {"list": 2, "item": "int"}},
    }
    assert shape(nested, depth=1) == {"a": "dict[1]", "list": "list[1]"}
    assert shape([[1]], depth=1) == {"list": 1, "item": "list[1]"}
    assert shape([]) == {"list": 0}
    assert shape("Wohnzimmer") == "<str len=10>"
    assert shape({"VA0000000101": 1, "2026-10-30": True}) == {
        "VA…0101": "int",
        "#-10-30": "bool",
    }
    assert shape({str(key): key for key in range(5)}, max_keys=2) == {
        "0": "int",
        "1": "int",
        "…": "+3 keys",
    }
    assert {"email", "name", "address", "refresh_token"} <= SENSITIVE_KEYS


ROOM_3 = f"{HOPS_B}/rooms/3"


@pytest.mark.parametrize(
    ("method", "url", "body", "expected"),
    [
        (
            "POST",
            f"{ROOM_3}/manualControl",
            None,
            "manualControl room 3: null until null",
        ),
        (
            "POST",
            f"{ROOM_3}/manualControl",
            {"setting": {"power": "ON"}, "termination": {"type": "MANUAL"}},
            "manualControl room 3: ON until MANUAL",
        ),
        (
            "PATCH",
            f"{HOPS_B}/roomsAndDevices/devices/VA0000000101",
            {"temperatureOffset": "Wohnzimmer", "name": "Fenster"},
            "device VA…0101: temperatureOffset=<str len=10>",
        ),
        (
            "POST",
            f"{ROOM_3}/schedule",
            {"dayType": "MONDAY", "daySchedule": "all day"},
            "schedule room 3 MONDAY: 0 blocks",
        ),
        (
            "PATCH",
            f"{HOPS_B}/settings/holidayMode",
            {"enabled": False, "startDate": "2026-12-24T00:00:00Z", "rooms": []},
            "holiday mode enabled=False",
        ),
        (
            "PUT",
            f"{HOPS_B}/something/5",
            {"secret": "x"},
            "PUT hops:/homes/{home}/something/{id}",
        ),
    ],
)
def test_describe_command_uses_allow_listed_fields(
    method: str, url: str, body: object, expected: str
) -> None:
    """Missing fields read as null, other fields and unknown writes stay hidden."""
    assert describe_command(method, URL(url), body) == expected


async def _schedule(home: TadoXHome) -> None:
    blocks = [
        ScheduleBlock(
            day=DayType.MONDAY,
            start_minute=start,
            end_minute=end,
            setting=Setting(power=Power.ON, temperature=temperature),
        )
        for start, end, temperature in ((0, 390, 18.0), (390, 1440, 21.0))
    ]
    await home.async_set_day_schedule(1, DayType.MONDAY, blocks)


type Write = Callable[[TadoXHome], Awaitable[None]]

WRITES: list[tuple[Write, str, int]] = [
    (
        lambda home: home.async_set_manual_control(
            3,
            Setting(power=Power.ON, temperature=21.0),
            Termination.timer(timedelta(hours=1)),
        ),
        "manualControl room 3: ON 21.0 °C until TIMER 3600 s",
        204,
    ),
    (
        lambda home: home.async_set_manual_control(
            3, Setting(power=Power.OFF), Termination.manual()
        ),
        "manualControl room 3: OFF until MANUAL",
        204,
    ),
    (
        lambda home: home.async_set_manual_control(
            2,
            Setting(power=Power.ON, temperature=19.5),
            Termination.next_time_block(),
        ),
        "manualControl room 2: ON 19.5 °C until NEXT_TIME_BLOCK",
        204,
    ),
    (lambda home: home.async_resume_schedule(3), "resume schedule room 3", 204),
    (
        lambda home: home.async_set_open_window(5, active=True),
        "open window room 5: on",
        204,
    ),
    (
        lambda home: home.async_set_open_window(5, active=False),
        "open window room 5: off",
        204,
    ),
    (
        lambda home: home.async_quick_action(QuickAction.BOOST),
        "quick action boost",
        200,
    ),
    (
        lambda home: home.async_quick_action(QuickAction.ALL_OFF),
        "quick action allOff",
        200,
    ),
    (
        lambda home: home.async_quick_action(QuickAction.RESUME_SCHEDULE),
        "quick action resumeSchedule",
        200,
    ),
    (
        lambda home: home.async_set_presence(PresenceSetting.HOME),
        "presence HOME",
        204,
    ),
    (
        lambda home: home.async_set_presence(PresenceSetting.AWAY),
        "presence AWAY",
        204,
    ),
    (
        lambda home: home.async_set_presence(PresenceSetting.AUTO),
        "presence AUTO",
        204,
    ),
    (
        lambda home: home.async_update_device("VA0000000101", temperature_offset=-0.5),
        "device VA…0101: temperatureOffset=-0.5",
        204,
    ),
    (
        lambda home: home.async_update_device("VA0000000101", child_lock=True),
        "device VA…0101: childLockEnabled=True",
        204,
    ),
    (
        lambda home: home.async_update_device(
            "VA0000000102", temperature_offset=0.0, child_lock=False
        ),
        "device VA…0102: temperatureOffset=0.0, childLockEnabled=False",
        204,
    ),
    (
        lambda home: home.async_identify_device("SU0000000102"),
        "identify SU…0102",
        204,
    ),
    (
        lambda home: home.async_set_room_termination_default(
            6, Termination.timer(timedelta(minutes=30))
        ),
        "room 6 termination default TIMER 1800 s",
        204,
    ),
    (
        lambda home: home.async_set_room_termination_default(
            1, Termination.next_time_block()
        ),
        "room 1 termination default NEXT_TIME_BLOCK",
        204,
    ),
    (_schedule, "schedule room 1 MONDAY: 2 blocks", 200),
    (
        lambda home: home.async_set_holiday_mode(
            enabled=True,
            start=datetime(2026, 12, 24, tzinfo=UTC),
            end=datetime(2026, 12, 27, tzinfo=UTC),
        ),
        "holiday mode enabled=True",
        204,
    ),
    (
        lambda home: home.async_set_flow_temperature(maximum=45),
        "flow temperature max=45",
        204,
    ),
    (
        lambda home: home.async_set_flow_temperature(auto_adaptation=True),
        "flow temperature autoAdaptation=True",
        204,
    ),
    (
        lambda home: home.async_set_flow_temperature(maximum=55, auto_adaptation=False),
        "flow temperature max=55, autoAdaptation=False",
        204,
    ),
    (
        lambda home: home.async_add_meter_reading(987654, date(2026, 10, 30)),
        "meter reading (value hidden)",
        200,
    ),
    (
        lambda home: home.async_add_tariff(
            cents=4321, unit=TariffUnit.KWH, start=date(2026, 10, 1), end=None
        ),
        "tariff (value hidden)",
        200,
    ),
]


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session bound to aioclient_mock."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def client(session: ClientSession) -> TadoXClient:
    """Return a client with a fresh token of home 1000002."""
    access, refresh = entry_tokens(HOME_B)
    tokens = TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return TadoXClient(
        session, TokenManager(session, MemoryTokenStore(tokens)), QuotaTracker()
    )


@pytest.mark.parametrize(
    ("write", "description", "status"),
    WRITES,
    ids=[description for _, description, _ in WRITES],
)
async def test_every_write_is_described_and_audited(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    caplog: pytest.LogCaptureFixture,
    write: Write,
    description: str,
    status: int,
) -> None:
    """Each write of the client is described from its request and logged at info."""
    mock_tado.home(HOME_B).flow_temperature = load_fixture("flow_temperature.json")
    caplog.set_level(logging.DEBUG)

    await write(client.home(HOME_B))

    call = next(call for call in mock_tado.calls() if call.method != "GET")
    assert describe_command(call.method, call.url, call.body) == description
    [audit] = _client_messages(caplog, logging.INFO)
    assert re.fullmatch(
        rf"Write: {re.escape(description)} -> ok \(HTTP {status}, \d+ ms\)", audit
    )
    for secret in ("VA0000000101", "VA0000000102", "SU0000000102", "987654", "4321"):
        assert secret not in caplog.text
    [exchange] = [e for e in client.telemetry.exchanges() if e.method == call.method]
    assert exchange.status == status
    assert exchange.outcome == "ok"


async def test_failed_write_is_audited_and_recorded(
    mock_tado: TadoCloudMock,
    client: TadoXClient,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refused write logs one info line with code and title, and is recorded."""
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TadoXRequestError):
        await client.home(HOME_B).async_set_open_window(1, active=True)

    assert _client_messages(caplog, logging.INFO) == [
        "Write failed: open window room 1: on -> HTTP 422 "
        "code=openWindow.notDetected title=<str len=23>"
    ]
    assert client.telemetry.last_error == ErrorRecord(
        at=datetime.now(UTC),
        kind="TadoXRequestError",
        context="command openWindow",
        endpoint="POST hops:/homes/{home}/rooms/{room}/openWindow",
        status=422,
        message="code=openWindow.notDetected title=<str len=23>",
    )


@pytest.mark.parametrize(
    ("err", "status", "message", "text"),
    [
        (
            TadoXHomeNotXError(
                1000001, "not.found", "Timezone of home 1000001 not found"
            ),
            404,
            "code=not.found title=Timezone of home # not found",
            "HTTP 404 code=not.found title=Timezone of home # not found",
        ),
        (TadoXForbiddenError(403), 403, None, "HTTP 403"),
        (
            TadoXRequestError(400, "fake-access-token-1000002", "Bearer x"),
            400,
            "code=<str len=25> title=<str len=8>",
            "HTTP 400 code=<str len=25> title=<str len=8>",
        ),
        (TadoXServerError(503), 503, None, "HTTP 503"),
        (
            TadoXRateLimitError(None, None, local=True),
            None,
            "rate limited by the local quota gate",
            "TadoXRateLimitError: rate limited by the local quota gate",
        ),
        (
            TadoXRateLimitError(None, None, local=False),
            429,
            "rate limited by the tado API",
            "HTTP 429 rate limited by the tado API",
        ),
        (
            TadoXAuthError("invalid_grant"),
            None,
            "invalid_grant",
            "TadoXAuthError: invalid_grant",
        ),
        (
            TadoXAuthError("Login of user@example.invalid failed"),
            None,
            "<str len=36>",
            "TadoXAuthError: <str len=36>",
        ),
        (
            TadoXParseError("rooms", "$[0].id", found="missing"),
            None,
            "unexpected rooms payload at $[0].id (missing)",
            "TadoXParseError: unexpected rooms payload at $[0].id (missing)",
        ),
        (
            TadoXParseError("token", "$.refresh_token"),
            None,
            "unexpected token payload at $.refresh_token",
            "TadoXParseError: unexpected token payload at $.refresh_token",
        ),
        (
            TadoXConnectionError("cannot reach hops.tado.com"),
            None,
            "cannot reach hops.tado.com",
            "TadoXConnectionError: cannot reach hops.tado.com",
        ),
        (
            TadoXDeviceFlowError("expired"),
            None,
            "expired",
            "TadoXDeviceFlowError: expired",
        ),
    ],
)
def test_record_tado_error_maps_the_taxonomy(
    freezer: FrozenDateTimeFactory,
    err: TadoXError,
    status: int | None,
    message: str | None,
    text: str,
) -> None:
    """Every error class gives its kind, status and a log-safe message."""
    telemetry = Telemetry()
    err.request = "GET hops:/homes/{home}/rooms"

    telemetry.record_tado_error(err, context="poll rooms")

    assert telemetry.last_error == ErrorRecord(
        at=datetime.now(UTC),
        kind=type(err).__name__,
        context="poll rooms",
        endpoint="GET hops:/homes/{home}/rooms",
        status=status,
        message=message,
    )
    assert describe_error(err) == text


def test_connection_error_names_its_cause() -> None:
    """A connection error tells which exception caused it, not its text."""
    err = TadoXConnectionError("cannot reach hops.tado.com", cause="TimeoutError")
    assert describe_error(err) == (
        "TadoXConnectionError: cannot reach hops.tado.com (TimeoutError)"
    )


def test_error_messages_are_safe() -> None:
    """The message of an error never holds a raw title or an odd reason."""
    err = TadoXHomeNotXError(1000001, "not.found", "Timezone of home 1000001 not found")
    assert str(err) == (
        "tado answered HTTP 404: code=not.found title=Timezone of home # not found"
    )
    assert err.title == "Timezone of home 1000001 not found"
    assert str(TadoXRequestError(409)) == "tado answered HTTP 409"
    assert str(TadoXAuthError("no_tokens")) == "no_tokens"
    assert str(TadoXAuthError("token fake-refresh-token-1")) == "<str len=26>"


def test_an_error_object_is_recorded_once(freezer: FrozenDateTimeFactory) -> None:
    """The same error recorded again, by a caller further up, changes nothing."""
    telemetry = Telemetry()
    calls: list[None] = []
    telemetry.add_listener(lambda: calls.append(None))
    err = TadoXServerError(502)

    telemetry.record_tado_error(err, context="token refresh")
    telemetry.record_tado_error(err, context="poll rooms")
    telemetry.record_tado_error(TadoXServerError(502), context="poll rooms")

    assert [record.context for record in telemetry.errors()] == [
        "token refresh",
        "poll rooms",
    ]
    assert telemetry.errors_by_kind() == {"TadoXServerError": 2}
    assert len(calls) == 2


def test_recorded_errors_keep_nothing_alive() -> None:
    """Telemetry holds no error, so no traceback keeps frames and their locals."""
    telemetry = Telemetry()
    locals_: list[weakref.ref[_Local]] = []

    def fail(index: int) -> None:
        local = _Local()
        locals_.append(weakref.ref(local))
        raise TadoXServerError(500 + index)

    for index in range(Telemetry.ERRORS + 5):
        try:
            fail(index)
        except TadoXServerError as err:
            telemetry.record_tado_error(err, context="poll rooms")
    gc.collect()

    assert [ref() for ref in locals_ if ref() is not None] == []
    assert len(telemetry.errors()) == Telemetry.ERRORS


def test_repeated_errors_are_counted_in_one_record(
    freezer: FrozenDateTimeFactory,
) -> None:
    """An error like the newest one counts there; it still counts per kind."""
    freezer.move_to(NOW)
    telemetry = Telemetry()
    calls: list[None] = []
    telemetry.add_listener(lambda: calls.append(None))

    for _ in range(3):
        telemetry.record_tado_error(TadoXServerError(503), context="poll rooms")
        freezer.tick(timedelta(minutes=1))
    telemetry.record_error(kind="TadoXServerError", context="poll home", status=503)
    telemetry.record_error(kind="TadoXServerError", context="poll rooms", status=503)
    telemetry.record_error(
        kind="TadoXServerError", context="poll rooms", status=503, message="x"
    )

    first, home, again, other = telemetry.errors()
    assert first == ErrorRecord(
        at=NOW,
        kind="TadoXServerError",
        context="poll rooms",
        status=503,
        count=3,
        last_at=NOW + timedelta(minutes=2),
    )
    assert (home.context, home.count, home.last_at) == ("poll home", 1, None)
    assert (again.context, again.count) == ("poll rooms", 1)
    assert (other.message, other.count) == ("x", 1)
    assert telemetry.errors_by_kind() == {"TadoXServerError": 6}
    assert len(calls) == 6


def test_ring_buffers_keep_the_newest(freezer: FrozenDateTimeFactory) -> None:
    """At most 50 exchanges and 20 errors are kept, the oldest are dropped."""
    telemetry = Telemetry()
    for index in range(Telemetry.EXCHANGES + 10):
        telemetry.record_exchange(_exchange(duration_ms=index))
    for index in range(Telemetry.ERRORS + 5):
        telemetry.record_error(kind="test", context=f"error {index}")

    exchanges = telemetry.exchanges()
    errors = telemetry.errors()
    assert (Telemetry.EXCHANGES, Telemetry.ERRORS) == (50, 20)
    assert [exchange.duration_ms for exchange in exchanges] == list(range(10, 60))
    assert [error.context for error in errors] == [f"error {i}" for i in range(5, 25)]
    assert telemetry.last_error == errors[-1]
    assert telemetry.requests_by_endpoint() == {"GET hops:/homes/{home}/rooms": 60}
    assert telemetry.errors_by_kind() == {"test": 25}
    exchanges.clear()
    assert len(telemetry.exchanges()) == 50


def test_counters_cover_a_rolling_day(freezer: FrozenDateTimeFactory) -> None:
    """Requests and errors count for 24 hours, by the minute."""
    freezer.move_to(NOW)
    telemetry = Telemetry()
    telemetry.record_exchange(_exchange())
    telemetry.record_error(kind="TadoXServerError", context="poll rooms")
    freezer.tick(timedelta(hours=12))
    telemetry.record_exchange(_exchange("GET my:/homes/{home}/state"))
    telemetry.record_exchange(_exchange(at=NOW - timedelta(minutes=5)))
    telemetry.record_error(kind="TadoXConnectionError", context="poll home")

    assert telemetry.requests_by_endpoint() == {
        "GET hops:/homes/{home}/rooms": 2,
        "GET my:/homes/{home}/state": 1,
    }
    assert telemetry.errors_by_kind(NOW + timedelta(hours=23, minutes=59)) == {
        "TadoXConnectionError": 1,
        "TadoXServerError": 1,
    }
    assert telemetry.errors_by_kind(NOW + timedelta(hours=24)) == {
        "TadoXConnectionError": 1
    }
    # The late exchange counted in the minute of the newest one.
    assert telemetry.requests_by_endpoint(NOW + timedelta(hours=35, minutes=59)) == {
        "GET hops:/homes/{home}/rooms": 1,
        "GET my:/homes/{home}/state": 1,
    }
    assert telemetry.requests_by_endpoint(NOW + timedelta(hours=36)) == {}


def test_diagnostics(freezer: FrozenDateTimeFactory) -> None:
    """Diagnostics hold plain data, newest last."""
    freezer.move_to(NOW)
    telemetry = Telemetry()
    assert telemetry.as_diagnostics() == {
        "exchanges": [],
        "errors": [],
        "requests_by_endpoint_24h": {},
        "errors_by_kind_24h": {},
        "last_error": None,
    }
    telemetry.record_exchange(_exchange(shape={"list": 6, "item": "dict[12]"}))
    telemetry.record_error(
        kind="TadoXServerError",
        context="poll rooms",
        endpoint="GET hops:/homes/{home}/rooms",
        status=503,
        message=f"home {HOME_B}",
    )

    diagnostics = telemetry.as_diagnostics(NOW)

    error = {
        "at": NOW.isoformat(),
        "kind": "TadoXServerError",
        "context": "poll rooms",
        "endpoint": "GET hops:/homes/{home}/rooms",
        "status": 503,
        "message": "home #",
        "count": 1,
        "last_at": None,
    }
    assert diagnostics == {
        "exchanges": [
            {
                "at": NOW.isoformat(),
                "method": "GET",
                "endpoint": "GET hops:/homes/{home}/rooms",
                "status": 200,
                "duration_ms": 182,
                "attempt": 1,
                "limit": None,
                "remaining": None,
                "reset_in_s": None,
                "outcome": "ok",
                "shape": {"list": 6, "item": "dict[12]"},
                "error": None,
            }
        ],
        "errors": [error],
        "requests_by_endpoint_24h": {"GET hops:/homes/{home}/rooms": 1},
        "errors_by_kind_24h": {"TadoXServerError": 1},
        "last_error": error,
    }
    json.dumps(diagnostics)


def test_listeners_fire_on_errors_only(freezer: FrozenDateTimeFactory) -> None:
    """A listener runs after every recorded error until it is removed."""
    telemetry = Telemetry()
    calls: list[ErrorRecord | None] = []
    remove = telemetry.add_listener(lambda: calls.append(telemetry.last_error))

    telemetry.record_exchange(_exchange())
    assert calls == []
    telemetry.record_error(kind="test", context="setup")
    assert [record.kind if record else None for record in calls] == ["test"]

    remove()
    remove()
    telemetry.record_error(kind="test", context="setup")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("changes", "line"),
    [
        (
            {"limit": 20000, "remaining": 18342, "reset_in_s": 7082},
            "GET hops:/homes/{home}/rooms -> 200 in 182 ms, "
            "quota 18342/20000 left, reset in 7082 s",
        ),
        (
            {"remaining": 5},
            "GET hops:/homes/{home}/rooms -> 200 in 182 ms, quota 5 left",
        ),
        (
            {"attempt": 2, "limit": 20000, "remaining": 1},
            "GET hops:/homes/{home}/rooms -> 200 in 182 ms, "
            "quota 1/20000 left, attempt 2 after 401",
        ),
        (
            {"status": 401, "outcome": "retried", "error": "HTTP 401"},
            "GET hops:/homes/{home}/rooms -> 401 in 182 ms: retried (HTTP 401)",
        ),
        (
            {
                "status": None,
                "duration_ms": 20000,
                "outcome": "TadoXConnectionError",
                "error": "TimeoutError",
            },
            "GET hops:/homes/{home}/rooms -> no answer in 20000 ms: "
            "TadoXConnectionError (TimeoutError)",
        ),
    ],
)
def test_exchange_log_line(
    caplog: pytest.LogCaptureFixture, changes: dict[str, Any], line: str
) -> None:
    """Every exchange is one debug line with quota, retry and error details."""
    caplog.set_level(logging.DEBUG)
    Telemetry().record_exchange(_exchange(**changes))
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == CLIENT_LOGGER
    ] == [(logging.DEBUG, line)]


def test_warn_once_is_bounded() -> None:
    """A full set forgets its oldest key, whose warning may come again."""
    warn = WarnOnce(max_keys=3)
    assert [warn.should_warn((key,)) for key in "abc"] == [True] * 3
    assert not warn.should_warn(("a",))
    assert warn.should_warn(("d",))
    assert warn.should_warn(("a",))
    assert not warn.should_warn(("d",))
    assert warn.should_warn(("b",))


def test_warn_once_default_bound() -> None:
    """The default set holds 512 keys, like the process-wide WARN_ONCE."""
    assert WarnOnce.MAX_KEYS == 512
    assert isinstance(WARN_ONCE, WarnOnce)
    warn = WarnOnce()
    assert all(warn.should_warn(("key", str(index))) for index in range(512))
    assert not warn.should_warn(("key", "0"))
    assert warn.should_warn(("key", "512"))
    assert warn.should_warn(("key", "0"))
