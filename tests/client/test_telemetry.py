"""Tests for the telemetry of the tado X client."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from yarl import URL

from custom_components.tado_x.client import (
    SENSITIVE_KEYS,
    WARN_ONCE,
    ErrorRecord,
    Exchange,
    TadoXAuthError,
    TadoXConnectionError,
    TadoXDeviceFlowError,
    TadoXError,
    TadoXForbiddenError,
    TadoXHomeNotXError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
    Telemetry,
    WarnOnce,
    describe_command,
    describe_error,
    describe_value,
    endpoint_template,
    mask_serial,
    redact_text,
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
from tests.tado_mock import HOME_B, load_fixture

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
HOPS_B = f"{HOPS_API_URL}/homes/{HOME_B}"
MY_B = f"{MY_API_URL}/homes/{HOME_B}"
CLIENT_LOGGER = "custom_components.tado_x.client"


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
    ],
)
def test_redact_text(text: str | None, expected: str | None) -> None:
    """Numbers, addresses, URLs and tokens are blanked; words are kept."""
    assert redact_text(text) == expected


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
            "code=<str len=25> title=<redacted>",
            "HTTP 400 code=<str len=25> title=<redacted>",
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
    err = TadoXConnectionError("cannot reach hops.tado.com")
    err.__cause__ = TimeoutError("connect to 192.168.100.200 timed out")
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
