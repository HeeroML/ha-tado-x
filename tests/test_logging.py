"""The log shows what happened, at the right level, and nothing private.

The fake cloud hands out tokens and codes with known prefixes. Each test runs
a part of the integration with every logger at DEBUG and then searches the
captured log for those prefixes and the other values the logging policy
forbids (observability contract §0 and §2). The other tests check the level
policy: one audit line per write, one info line per state change, and no
info line at all in a steady state.
"""

from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import timedelta
import json
import logging
import re
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.tado_x.const import DOMAIN
from custom_components.tado_x.runtime import TadoXRuntimeData
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.number.const import (
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch.const import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_SELECT_OPTION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from tests.tado_mock import HOME_B, HOME_NAMES, TadoCloudMock, load_fixture
from tests.test_init import make_entry, setup_entry

TOKEN_PATH = "/oauth2/token"
EXPIRED_ISO = "2026-01-01T00:00:00+00:00"
START = "2026-10-30T10:00:00+00:00"
POLL_INTERVAL = timedelta(seconds=5)
DEVICE_AUTHORIZATION = load_fixture("device_authorize.json")
ME = load_fixture("me.json")
ROOMS = f"/homes/{HOME_B}/rooms"
WEATHER = f"/api/v2/homes/{HOME_B}/weather"
MANUAL_1 = f"/homes/{HOME_B}/rooms/1/manualControl"
# The integration names the home by the last four digits of its id, never by
# the entry title, which the config flow takes from tado's name of the home.
LABEL = "home …0002"
TADO_LOGGER = "custom_components.tado_x"

SECRETS = (
    "fake-access-token",
    "fake-refresh-token",
    "fake.jwt",
    DEVICE_AUTHORIZATION["device_code"],
    DEVICE_AUTHORIZATION["user_code"],
    "user_code=",
    "Bearer ",
    ME["email"],
    ME["name"],
)
LOGIN_SECRET = "fake-refresh-token-in-a-login-body"
LATITUDE = "47.123456"
LONGITUDE = "9.654321"
# What the integration must not log even where Home Assistant itself may,
# for example in entity ids: room, home and phone names, the address,
# coordinates and full serial numbers.
PRIVATE = (
    "Wohnzimmer",
    "Kueche",
    "Schlafzimmer",
    "Kinderzimmer",
    "Arbeitszimmer",
    "Haus B",
    "Tablet",
    "Phone 2",
    "Model1",
    "Musterstrasse",
    "Musterstadt",
    LATITUDE,
    LONGITUDE,
)
FULL_SERIAL = re.compile(r"\b(?:VA|SU|IB)\d{10}\b")
NASTY_4XX = {
    "errors": [
        {
            "code": "not.allowed",
            "title": f"Token fake-access-token-9999 of {ME['email']} in home {HOME_B}",
            "detail": f"Wohnzimmer, Musterstrasse 1, {LATITUDE}, VA0000000101",
        }
    ]
}
NASTY_5XX = (
    "<html>Bearer fake-access-token-9999 user@example.invalid Wohnzimmer "
    f"Musterstrasse 1 {LATITUDE} {LONGITUDE} VA0000000101</html>"
)
NOT_FOUND_TITLE = f"Weather of home {HOME_B} not found"
# The test harness's mock Store logs every payload it writes; Home Assistant's
# real Store logs only the key and the file path.
TEST_STORAGE_LOGGER = "pytest_homeassistant_custom_component.common"
# Home Assistant logs the setup of an entry with its title under the logger of
# the integration; those lines are Home Assistant's, not the integration's.
HA_ENTRY_LOGS = "homeassistant/config_entries.py"


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Return every record of the setup and call phases, with tracebacks."""
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(
        formatter.format(record)
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if record.name != TEST_STORAGE_LOGGER
    )


def _tado_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Return the records of the integration, with tracebacks.

    The records of the coordinators that Home Assistant's DataUpdateCoordinator
    writes with the coordinator name count as the integration's.
    """
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(
        formatter.format(record)
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if record.name.startswith(TADO_LOGGER)
        and not record.pathname.endswith(HA_ENTRY_LOGS)
    )


def _messages(records: Sequence[logging.LogRecord], level: int) -> list[str]:
    """Return the messages the integration logged at exactly one level."""
    return [
        record.getMessage()
        for record in records
        if record.name.startswith(TADO_LOGGER) and record.levelno == level
    ]


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the time before the HTTP client of a test gets its access token."""
    freezer.move_to(START)


@pytest.fixture(autouse=True)
def debug_logging(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Capture every logger at DEBUG and check the log after the test."""
    caplog.set_level(logging.DEBUG)
    yield
    text = _log_text(caplog)
    assert [secret for secret in SECRETS if secret in text] == []


def _assert_requests_were_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The DEBUG request log ran, so the secret check above is not vacuous."""
    assert "GET hops:/homes/{home}/rooms -> 200 in " in caplog.text


async def _setup(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """Set up home 1000002 at START with changed options."""
    entry = make_entry()
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(freezer: FrozenDateTimeFactory, coordinator: Any) -> None:
    """Refresh a coordinator once its endpoints are due again."""
    freezer.tick(coordinator.update_interval)
    await coordinator.async_refresh()


def _entity(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def test_sign_in_and_setup(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The device-code sign-in and the setup of the new entry log no secret."""
    mock_tado.device_flow_pending = 1
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    for login_finishes in (False, True):
        freezer.tick(POLL_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=login_finishes)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].state is ConfigEntryState.LOADED
    assert result["result"].title == HOME_NAMES[HOME_B]
    assert HOME_NAMES[HOME_B] not in _tado_log_text(caplog)
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 2
    _assert_requests_were_logged(caplog)


async def test_rotation_and_401_cycle(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rotation at setup, a 401 with refresh and retry, and a failed refresh."""
    entry = make_entry(token_expiry=EXPIRED_ISO)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    rooms = entry.runtime_data.rooms

    mock_tado.revoke_access_tokens()
    freezer.tick(timedelta(minutes=1))
    await rooms.async_refresh()
    assert rooms.last_update_success
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 2
    assert [call.url.path for call in mock_tado.calls("GET")][-2:] == [
        "/homes/1000002/rooms",
        "/homes/1000002/rooms",
    ]

    freezer.tick(timedelta(minutes=10))
    mock_tado.fail("POST", TOKEN_PATH, exc=ClientConnectionError(), times=1)
    await rooms.async_refresh()
    assert not rooms.last_update_success
    assert "Refreshing the tado access token failed" in caplog.text

    freezer.tick(timedelta(seconds=10))
    await rooms.async_refresh()
    assert rooms.last_update_success
    _assert_requests_were_logged(caplog)


async def test_rejected_refresh_logs_no_login_body(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An invalid_grant answer is logged by its error code, never by its body."""
    entry = make_entry()
    await setup_entry(hass, entry)
    freezer.tick(timedelta(minutes=10))
    mock_tado.fail(
        "POST",
        TOKEN_PATH,
        status=400,
        body={"error": "invalid_grant", "error_description": LOGIN_SECRET},
    )

    start = len(caplog.records)
    await entry.runtime_data.rooms.async_refresh()
    await hass.async_block_till_done()
    for coordinator in (entry.runtime_data.home, entry.runtime_data.devices):
        await coordinator.async_refresh()

    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert _messages(caplog.records[start:], logging.INFO) == [
        "tado rejected the login (invalid_grant); a new login is needed"
    ]
    assert LOGIN_SECRET not in caplog.text


async def test_401_after_a_refresh_logs_the_reauth_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 that a fresh token gets too starts a reauth, logged once with its reason.

    Every coordinator fails the same way; the line is not repeated.
    """
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data
    mock_tado.fail("GET", re.compile(r"/homes/1000002/"), status=401)
    start = len(caplog.records)

    freezer.tick(timedelta(minutes=1))
    for coordinator in (data.rooms, data.home, data.devices, data.rooms):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert _messages(caplog.records[start:], logging.INFO) == [
        f"tado no longer accepts the login of {LABEL} "
        "(unauthorized_after_refresh); a new login is needed"
    ]


async def test_401_of_a_write_logs_the_reauth_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write that gets 401 after a refresh starts a reauth and logs it.

    The polls that fail the same way afterwards log nothing more.
    """
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data
    mock_tado.fail("DELETE", MANUAL_1, status=401)
    mock_tado.fail("GET", ROOMS, status=401)
    start = len(caplog.records)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {
                ATTR_ENTITY_ID: _entity(hass, CLIMATE_DOMAIN, f"{HOME_B}_1_climate"),
                "hvac_mode": HVACMode.AUTO,
            },
            blocking=True,
        )
    await hass.async_block_till_done()
    await _poll(freezer, data.rooms)

    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert not data.rooms.last_update_success
    assert _messages(caplog.records[start:], logging.INFO) == [
        "Write failed: resume schedule room 1 -> HTTP 401 unauthorized_after_refresh",
        f"tado no longer accepts the login of {LABEL} "
        "(unauthorized_after_refresh); a new login is needed",
    ]


async def test_nothing_private_reaches_the_log(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup, polls, writes, refreshes, a 401, 4xx and 5xx bodies and diagnostics.

    None of them logs a room, home or phone name, the address, coordinates,
    a full serial number or a token, at any level; a tado error title is
    logged with its numbers replaced.
    """
    home = mock_tado.home(HOME_B)
    home.info["geolocation"] = {
        "latitude": float(LATITUDE),
        "longitude": float(LONGITUDE),
    }
    entry = await _setup(hass, mobile_devices=True, holiday_mode=True)
    data: TadoXRuntimeData = entry.runtime_data
    climate = _entity(hass, CLIMATE_DOMAIN, f"{HOME_B}_1_climate")

    await _poll(freezer, data.home)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: climate, "temperature": 22},
        blocking=True,
    )
    mock_tado.revoke_access_tokens()
    await _poll(freezer, data.rooms)
    mock_tado.fail("GET", ROOMS, status=422, body=NASTY_4XX, times=1)
    await _poll(freezer, data.rooms)
    mock_tado.fail("GET", ROOMS, status=500, body=NASTY_5XX, times=1)
    await _poll(freezer, data.rooms)
    mock_tado.fail(
        "GET",
        WEATHER,
        status=404,
        body={"errors": [{"code": "not.found", "title": NOT_FOUND_TITLE}]},
        times=1,
    )
    freezer.tick(timedelta(minutes=15))
    await data.home.async_refresh()
    mock_tado.fail("POST", MANUAL_1, status=422, body=NASTY_4XX, times=1)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: climate, "temperature": 23},
            blocking=True,
        )
    await get_diagnostics_for_config_entry(hass, hass_client, entry)

    text = _tado_log_text(caplog)
    assert [item for item in PRIVATE if item in text] == []
    assert FULL_SERIAL.findall(text) == []
    assert NOT_FOUND_TITLE not in text
    assert f"title=<str len={len(NOT_FOUND_TITLE)}>" in text
    assert f"title=<str len={len(NASTY_4XX['errors'][0]['title'])}>" in text
    assert "HTTP 500" in text
    assert "Write: manualControl room 1: ON 22.0 °C" in text
    assert "attempt 2 after 401" in text
    assert len(mock_tado.calls("POST", TOKEN_PATH)) == 2
    _assert_requests_were_logged(caplog)


async def test_the_home_name_stays_out_of_logs_and_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry title, which is tado's name of the home, is never logged.

    The integration names the home by a label from its id, in its own lines
    and in the coordinator names Home Assistant logs; diagnostics show the
    label instead of the title.
    """
    mock_tado.home(HOME_B).info["generation"] = "O2_NEW_GENERATION"
    entry = await _setup(hass, holiday_mode=True)
    data: TadoXRuntimeData = entry.runtime_data
    assert entry.title == HOME_NAMES[HOME_B]
    assert [
        coordinator.name for coordinator in (data.rooms, data.home, data.devices)
    ] == [
        f"tado_x rooms {LABEL}",
        f"tado_x home {LABEL}",
        f"tado_x devices {LABEL}",
    ]

    mock_tado.revoke_access_tokens()
    await _poll(freezer, data.rooms)
    mock_tado.fail("GET", ROOMS, status=500, times=1)
    await _poll(freezer, data.rooms)
    for service, service_data in (
        ("add_meter_reading", {"reading": 1234}),
        ("set_eiq_tariff", {"tariff": 0.285, "unit": "kwh"}),
        ("set_holiday_mode", {"enabled": True}),
    ):
        await hass.services.async_call(DOMAIN, service, service_data, blocking=True)
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=1))
    await _poll(freezer, data.rooms)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert diagnostics["entry"]["title"] == LABEL
    assert entry.title not in json.dumps(diagnostics)
    text = _tado_log_text(caplog)
    assert entry.title not in text
    for line in (
        f"Set up {LABEL}: ",
        f"Error fetching tado_x rooms {LABEL} data",
        f"tado reports no known generation for {LABEL}",
        f"Serving cached rooms data of {LABEL}",
        f"The tado API quota of {LABEL} is used up",
        f"Writing the rotated tado tokens of {LABEL}",
    ):
        assert line in text


@pytest.mark.parametrize(
    "cause",
    ["timezone_not_found", "previous_generation"],
    ids=["hops_404", "home_info"],
)
async def test_a_home_that_is_no_tado_x_home_is_logged_by_its_label(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    issue_registry: ir.IssueRegistry,
    cause: str,
) -> None:
    """The error that Home Assistant logs, with its traceback, names the label.

    A hops 404 "Timezone of home … not found" and home details of the
    previous generation both end the polling with a ConfigEntryError. Only
    the repair issue, which the log never shows, keeps the entry title.
    """
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data
    if cause == "timezone_not_found":
        title = f"Timezone of home {HOME_B} not found"
        body = {"errors": [{"code": "not.found", "title": title}]}
        mock_tado.fail("GET", ROOMS, status=404, body=body)
        coordinator: Any = data.rooms
    else:
        mock_tado.home(HOME_B).info["generation"] = "PRE_LINE_X"
        coordinator = data.devices
    freezer.tick(timedelta(days=1))
    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, ConfigEntryError)
    text = _tado_log_text(caplog)
    assert f"{LABEL} is not a tado X home" in text
    assert entry.title not in text
    issue = issue_registry.async_get_issue(DOMAIN, f"not_tado_x_home_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"home": entry.title}


async def test_steady_state_logs_nothing_at_info(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Six hours of polls and routine token refreshes log nothing at INFO or above."""
    await _setup(hass)
    start = len(caplog.records)

    for _ in range(6 * 3600 // 45):
        freezer.tick(timedelta(seconds=45))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    loud = [
        record.getMessage()
        for record in caplog.records[start:]
        if record.name.startswith(TADO_LOGGER) and record.levelno >= logging.INFO
    ]
    assert loud == []
    assert len(mock_tado.calls("POST", TOKEN_PATH)) >= 6 * 6
    assert len(mock_tado.calls("GET", ROOMS)) >= 6 * 3600 // 45 - 1
    assert "Stored the rotated tado tokens" in caplog.text


@pytest.mark.parametrize(
    ("domain", "service", "target", "data", "action", "status"),
    [
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            (CLIMATE_DOMAIN, f"{HOME_B}_1_climate"),
            {"temperature": 21},
            "manualControl room 1: ON 21.0 °C until NEXT_TIME_BLOCK",
            204,
            id="manual_control",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            (CLIMATE_DOMAIN, f"{HOME_B}_2_climate"),
            {"temperature": 19.5},
            "manualControl room 2: ON 19.5 °C until TIMER 3600 s",
            204,
            id="manual_control_timer",
        ),
        pytest.param(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            (CLIMATE_DOMAIN, f"{HOME_B}_3_climate"),
            {"hvac_mode": HVACMode.AUTO},
            "resume schedule room 3",
            204,
            id="resume",
        ),
        pytest.param(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            (SELECT_DOMAIN, f"{HOME_B}_presence_mode"),
            {"option": "away"},
            "presence AWAY",
            204,
            id="presence",
        ),
        pytest.param(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            (SELECT_DOMAIN, f"{HOME_B}_1_manual_termination"),
            {"option": "manual"},
            "room 1 termination default MANUAL",
            204,
            id="room_default",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            (SWITCH_DOMAIN, "VA0000000101_child_lock"),
            {},
            "device VA…0101: childLockEnabled=True",
            204,
            id="child_lock",
        ),
        pytest.param(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            (SWITCH_DOMAIN, f"{HOME_B}_1_open_window"),
            {},
            "open window room 1: off",
            204,
            id="open_window",
        ),
        pytest.param(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            (NUMBER_DOMAIN, "VA0000000101_offset"),
            {"value": -0.5},
            "device VA…0101: temperatureOffset=-0.5",
            204,
            id="offset",
        ),
        pytest.param(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            (BUTTON_DOMAIN, f"{HOME_B}_boost_all"),
            {},
            "quick action boost",
            200,
            id="quick_action",
        ),
        pytest.param(
            DOMAIN,
            "set_schedule",
            (CLIMATE_DOMAIN, f"{HOME_B}_1_climate"),
            {
                "days": ["monday"],
                "blocks": [{"start": "00:00", "end": "24:00", "temperature": 20}],
            },
            "schedule room 1 MONDAY: 1 blocks",
            200,
            id="schedule",
        ),
        pytest.param(
            DOMAIN,
            "set_holiday_mode",
            None,
            {"enabled": True},
            "holiday mode enabled=True",
            204,
            id="holiday_mode",
        ),
        pytest.param(
            DOMAIN,
            "add_meter_reading",
            None,
            {"reading": 1234},
            "meter reading (value hidden)",
            200,
            id="meter_reading",
        ),
        pytest.param(
            DOMAIN,
            "set_eiq_tariff",
            None,
            {"tariff": 0.285, "unit": "kwh"},
            "tariff (value hidden)",
            200,
            id="tariff",
        ),
    ],
)
async def test_every_write_logs_one_audit_line(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    domain: str,
    service: str,
    target: tuple[str, str] | None,
    data: dict[str, Any],
    action: str,
    status: int,
) -> None:
    """Each write logs exactly one INFO line: the audit short form and its result."""
    await _setup(hass)
    if target is not None:
        data = {**data, ATTR_ENTITY_ID: _entity(hass, *target)}
    start = len(caplog.records)

    await hass.services.async_call(domain, service, data, blocking=True)

    [line] = _messages(caplog.records[start:], logging.INFO)
    assert line.startswith(f"Write: {action} -> ok (HTTP {status}, ")
    assert line.endswith(" ms)")


async def test_failed_write_logs_its_status(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused write logs one INFO line with the status, code and title length."""
    entry = await _setup(hass)
    climate = _entity(hass, CLIMATE_DOMAIN, f"{HOME_B}_1_climate")
    mock_tado.fail("POST", MANUAL_1, status=422, body=NASTY_4XX, times=1)
    start = len(caplog.records)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: climate, "temperature": 21},
            blocking=True,
        )

    assert _messages(caplog.records[start:], logging.INFO) == [
        "Write failed: manualControl room 1: ON 21.0 °C until NEXT_TIME_BLOCK "
        f"-> HTTP 422 code=not.allowed title=<str len={len(NASTY_4XX['errors'][0]['title'])}>"
    ]
    assert raised.value.translation_placeholders == {
        "status": "422",
        "code": "not.allowed",
    }
    [error] = entry.runtime_data.telemetry.errors()
    assert (error.context, error.status) == ("command manualControl", 422)


async def test_unexpected_answer_to_a_device_write_names_no_serial(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write answered by a page that is not JSON fails with the endpoint template.

    The error shown to the user names neither the home id nor the serial.
    """
    await _setup(hass)
    offset = _entity(hass, NUMBER_DOMAIN, "VA0000000101_offset")
    mock_tado.fail(
        "PATCH",
        f"/homes/{HOME_B}/roomsAndDevices/devices/VA0000000101",
        status=200,
        body=b"<html>OK</html>",
        times=1,
    )

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: offset, "value": 1.0},
            blocking=True,
        )

    assert raised.value.translation_key == "api_error"
    assert raised.value.translation_placeholders == {
        "error": "PATCH hops:/homes/{home}/roomsAndDevices/devices/{serial}"
    }
    assert FULL_SERIAL.findall(str(raised.value)) == []
    assert str(HOME_B) not in str(raised.value)


async def test_quota_hold_below_the_floor_logs_each_change_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Start and end of a hold and of the cache fallback each log one INFO line."""
    entry = await _setup(hass, quota_reserve=0)
    rooms = entry.runtime_data.rooms
    mock_tado.set_quota(remaining=15, reset_in=timedelta(hours=1))

    start = len(caplog.records)
    await _poll(freezer, rooms)
    assert _messages(caplog.records[start:], logging.INFO) == [
        f"Polling of {LABEL} holds until 2026-10-30T11:00:00+00:00: "
        "only 14 tado API calls are left"
    ]

    start = len(caplog.records)
    await _poll(freezer, rooms)
    assert rooms.stale_since is not None
    assert _messages(caplog.records[start:], logging.INFO) == [
        f"Serving cached rooms data of {LABEL} from 2026-10-30T10:00:45+00:00 "
        "during a quota hold"
    ]

    start = len(caplog.records)
    freezer.move_to("2026-10-30T11:01:00+00:00")
    await rooms.async_refresh()
    assert rooms.stale_since is None
    assert sorted(_messages(caplog.records[start:], logging.INFO)) == [
        f"Polling of {LABEL} resumes after the quota hold",
        f"Stopped serving cached rooms data of {LABEL}",
    ]
    assert _messages(caplog.records, logging.WARNING) == []


async def test_used_up_quota_warns_once_and_logs_the_end(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A used-up quota is one WARNING; the cache fallback and the end are INFO."""
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    start = len(caplog.records)
    await _poll(freezer, data.rooms)
    records = caplog.records[start:]
    assert _messages(records, logging.WARNING) == [
        f"The tado API quota of {LABEL} is used up; polling pauses until "
        "2026-10-30 12:00:30+00:00"
    ]
    assert _messages(records, logging.INFO) == [
        f"Serving cached rooms data of {LABEL} from 2026-10-30T10:00:00+00:00 "
        "during a quota hold"
    ]

    start = len(caplog.records)
    await _poll(freezer, data.rooms)
    assert _messages(caplog.records[start:], logging.INFO) == []

    start = len(caplog.records)
    freezer.move_to("2026-10-30T12:01:00+00:00")
    await data.rooms.async_refresh()
    records = caplog.records[start:]
    assert sorted(_messages(records, logging.INFO)) == [
        f"Polling of {LABEL} resumes after the quota hold",
        f"Stopped serving cached rooms data of {LABEL}",
    ]
    assert _messages(records, logging.WARNING) == []
    assert [error.kind for error in data.telemetry.errors()] == ["quota_gate"]


@pytest.mark.parametrize(
    ("remaining", "reset_in", "options", "loads", "expected"),
    [
        pytest.param(
            15,
            None,
            {"quota_reserve": 0},
            True,
            [
                (
                    logging.INFO,
                    f"Set up {LABEL}: 6 rooms, 13 devices, "
                    "2617 planned API calls a day",
                ),
                (logging.INFO, f"Stopped serving cached rooms data of {LABEL}"),
            ],
            id="floor_without_reset",
        ),
        pytest.param(
            15,
            timedelta(hours=2),
            {"quota_reserve": 0},
            False,
            [
                (
                    logging.INFO,
                    f"Polling of {LABEL} holds until 2026-10-30T12:00:00+00:00: "
                    "only 14 tado API calls are left",
                ),
            ],
            id="below_the_floor",
        ),
        pytest.param(
            15,
            timedelta(hours=2),
            {},
            False,
            [
                (
                    logging.INFO,
                    f"Only 14 tado API calls are left for {LABEL}; polling slows "
                    "down until the quota resets",
                ),
                (
                    logging.INFO,
                    f"Polling of {LABEL} holds until 2026-10-30T12:00:00+00:00: "
                    "only 14 tado API calls are left",
                ),
            ],
            id="conserving_below_the_floor",
        ),
        pytest.param(
            0,
            timedelta(hours=2),
            {},
            False,
            [
                (
                    logging.WARNING,
                    f"The tado API quota of {LABEL} is used up; polling pauses "
                    "until 2026-10-30 12:00:30+00:00",
                ),
            ],
            id="used_up",
        ),
    ],
)
async def test_setup_retries_during_a_hold_log_it_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    remaining: int,
    reset_in: timedelta | None,
    options: dict[str, Any],
    loads: bool,
    expected: list[tuple[int, str]],
) -> None:
    """Setup retries during a quota hold log the hold once, not on every retry.

    A retry restores the quota state the previous attempt stored, so the hold
    and the cached data it serves are no news. Without a reset time, a poll
    goes out every 15 minutes, and the retries load the entry once each
    coordinator has data.
    """
    mock_tado.set_quota(remaining=remaining, reset_in=reset_in)
    entry = await _setup(hass, **options)

    for _ in range(110):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert entry.state is (
        ConfigEntryState.LOADED if loads else ConfigEntryState.SETUP_RETRY
    )
    own = Counter(
        (record.levelno, record.getMessage())
        for record in caplog.records
        if "custom_components/tado_x/" in record.pathname
        and record.levelno >= logging.INFO
    )
    assert sorted(own.items()) == sorted((line, 1) for line in expected)
    assert f"Restored the quota state of {LABEL}" in caplog.text


async def test_failed_refresh_and_recovery_log_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed refresh and its recovery each log one INFO line; polls go on."""
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data
    mock_tado.fail("POST", TOKEN_PATH, status=503, times=1)

    start = len(caplog.records)
    freezer.tick(timedelta(minutes=9, seconds=30))
    await data.rooms.async_refresh()
    assert data.rooms.last_update_success
    assert _messages(caplog.records[start:], logging.INFO) == [
        "Refreshing the tado access token failed (TadoXServerError, HTTP 503), "
        "retrying in 5 s"
    ]

    start = len(caplog.records)
    await _poll(freezer, data.rooms)
    assert data.rooms.last_update_success
    assert _messages(caplog.records[start:], logging.INFO) == [
        "Refreshing the tado access token works again after 1 failed attempts"
    ]
    assert [(error.context, error.status) for error in data.telemetry.errors()] == [
        ("token refresh", 503)
    ]


async def test_removed_and_added_device_log_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device tado stops and starts reporting logs one INFO line each time."""
    entry = await _setup(hass)
    devices = entry.runtime_data.devices
    room = mock_tado.home(HOME_B).topology_room(6)
    assert room is not None
    valve = room["devices"].pop(0)

    start = len(caplog.records)
    await _poll(freezer, devices)
    assert _messages(caplog.records[start:], logging.INFO) == [
        f"Removed VA04 VA…0106 from {LABEL}"
    ]

    room["devices"].insert(0, valve)
    start = len(caplog.records)
    await _poll(freezer, devices)
    assert _messages(caplog.records[start:], logging.INFO) == [
        f"Added VA04 VA…0106 to {LABEL}"
    ]


async def test_unparseable_fields_warn_once_per_field(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown values warn once per field however often they come back.

    The warn-once set lives as long as the process, so the values are
    unique to this test.
    """
    home = mock_tado.home(HOME_B)
    for room in home.topology["rooms"][:2]:
        room["devices"][0]["batteryState"] = "O2_ODD_BATTERY"
    home.weather["weatherState"]["value"] = "O2_ODD_WEATHER"
    entry = await _setup(hass)
    data: TadoXRuntimeData = entry.runtime_data

    for _ in range(3):
        await _poll(freezer, data.devices)
        await _poll(freezer, data.home)

    assert sorted(_messages(caplog.records, logging.WARNING)) == [
        "Unknown value O2_ODD_BATTERY for device.batteryState from the tado API; "
        "it is treated as unknown",
        "Unknown value O2_ODD_WEATHER for weatherState.value from the tado API; "
        "it is treated as unknown",
    ]
    assert len(mock_tado.calls("GET", "/homes/1000002/roomsAndDevices")) == 4
