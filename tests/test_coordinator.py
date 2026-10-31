"""Tests for the coordinators: cadence, quota holds, failures and optimistic patches."""

from collections.abc import Callable
import copy
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tado_x.budget import PlanMode
from custom_components.tado_x.client import (
    Power,
    Setting,
    TadoXAuthError,
    TadoXConnectionError,
    TadoXError,
    TadoXForbiddenError,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXServerError,
)
from custom_components.tado_x.const import DOMAIN
from custom_components.tado_x.coordinator import RoomsCoordinator, TadoXCoordinator
from custom_components.tado_x.entity import TadoXEntity, handle_tado_errors
from custom_components.tado_x.quota import ApiStatus
from custom_components.tado_x.runtime import (
    FeatureStatus,
    RoomsSnapshot,
    TadoXRuntimeData,
)
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate.const import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number.const import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_climate import climate_id, entity_id, state_of
from tests.test_init import make_entry, setup_entry

START = "2026-10-30T10:00:00+00:00"
ROOMS = "/homes/1000002/rooms"
STATE = "/api/v2/homes/1000002/state"
WEATHER = "/api/v2/homes/1000002/weather"
AIR_COMFORT = "/homes/1000002/airComfort"
TOPOLOGY = "/homes/1000002/roomsAndDevices"
HOME_INFO = "/api/v2/homes/1000002"
RUNNING_TIMES = "/v1/homes/1000002/runningTimes"
FLOW = "/homes/1000002/settings/flowTemperatureOptimization"
HOLIDAY = "/homes/1000002/settings/holidayMode"
OFF = Setting(power=Power.OFF, temperature=None)
ROOMS_REQUEST = "GET hops:/homes/{home}/rooms"
DEVICE_PATH = "/homes/1000002/roomsAndDevices/devices/VA0000000101"
DEVICE_REQUEST = "PATCH hops:/homes/{home}/roomsAndDevices/devices/{serial}"
RESET = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry | None = None,
    **options: Any,
) -> TadoXRuntimeData:
    """Set up an entry at START and keep its rooms coordinator polling."""
    freezer.move_to(START)
    entry = entry or make_entry()
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    data: TadoXRuntimeData = entry.runtime_data
    entry.async_on_unload(data.rooms.async_add_listener(lambda: None))
    return data


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _poll(freezer: FrozenDateTimeFactory, coordinator: Any) -> None:
    """Refresh a coordinator once its endpoints are due again."""
    freezer.tick(coordinator.update_interval)
    await coordinator.async_refresh()


async def _run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, ticks: int, step: int = 45
) -> None:
    for _ in range(ticks):
        await _tick(hass, freezer, step)


def _count(mock_tado: TadoCloudMock, path: str) -> int:
    return len(mock_tado.calls("GET", path))


def _stale_since(coordinator: TadoXCoordinator[Any]) -> datetime | None:
    return coordinator.stale_since


def _status(data: TadoXRuntimeData) -> ApiStatus:
    return data.monitor.status


def _mode(data: TadoXRuntimeData) -> PlanMode:
    return data.monitor.mode


def _flow_status(data: TadoXRuntimeData) -> FeatureStatus:
    return data.devices.data.flow_status


def _room_power(data: TadoXRuntimeData, room_id: int) -> Power:
    return data.rooms.data.rooms[room_id].setting.power


async def test_intervals_follow_the_plan(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Each coordinator polls at the shortest period of its endpoints."""
    data = await _setup(hass, freezer)

    assert data.monitor.mode is PlanMode.OK
    assert data.rooms.update_interval == timedelta(seconds=45)
    assert data.home.update_interval == timedelta(seconds=300)
    assert data.devices.update_interval == timedelta(seconds=900)
    assert data.monitor.status is ApiStatus.OK


async def test_endpoints_follow_their_own_period(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """After the first refresh only due endpoints are read."""
    await _setup(hass, freezer)
    mock_tado.clear_calls()

    await _tick(hass, freezer, 45)
    assert [call.url.path for call in mock_tado.calls()] == [ROOMS]

    await _run(hass, freezer, 6)  # 315 s
    assert _count(mock_tado, ROOMS) == 7
    assert _count(mock_tado, STATE) == 1
    assert _count(mock_tado, WEATHER) == 0

    await _run(hass, freezer, 14)  # 945 s
    assert _count(mock_tado, STATE) == 3
    assert _count(mock_tado, WEATHER) == 1
    assert _count(mock_tado, AIR_COMFORT) == 1
    assert _count(mock_tado, TOPOLOGY) == 1
    assert _count(mock_tado, RUNNING_TIMES) == 0

    await _run(hass, freezer, 9, step=300)  # 3645 s
    assert _count(mock_tado, TOPOLOGY) == 4
    assert _count(mock_tado, RUNNING_TIMES) == 1
    assert _count(mock_tado, HOME_INFO) == 0
    assert _count(mock_tado, FLOW) == 0
    assert _count(mock_tado, HOLIDAY) == 0


async def test_requested_refresh_reads_required_endpoints(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A requested refresh reads the required endpoints, after a 5 s cooldown."""
    data = await _setup(hass, freezer)
    mock_tado.clear_calls()

    await data.home.async_request_refresh()
    await _tick(hass, freezer, 5)

    assert [call.url.path for call in mock_tado.calls()] == [STATE]


async def test_quota_hold_serves_cached_values(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A 429 keeps the values, sends nothing until the reset, then resumes (#62)."""
    data = await _setup(hass, freezer)
    before = data.rooms.data
    entry_id = data.rooms.config_entry.entry_id
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await _tick(hass, freezer, 45)

    assert data.rooms.data == before
    assert data.rooms.last_update_success
    assert data.rooms.stale_since == datetime.fromisoformat(START) + timedelta(
        seconds=45
    )
    assert data.monitor.status is ApiStatus.RATE_LIMITED
    assert data.monitor.mode is PlanMode.BLOCKED
    assert data.monitor.reset_time == data.monitor.blocked_until
    issue = issue_registry.async_get_issue(DOMAIN, f"quota_exhausted_{entry_id}")
    assert issue is not None

    mock_tado.clear_calls()
    await _run(hass, freezer, 23, step=300)  # until just before the reset

    assert mock_tado.calls() == []
    assert data.rooms.last_update_success
    assert data.home.last_update_success

    await _run(hass, freezer, 2, step=300)

    assert mock_tado.calls("GET", ROOMS)
    assert _stale_since(data.rooms) is None
    assert _status(data) is ApiStatus.OK
    assert not issue_registry.async_get_issue(DOMAIN, f"quota_exhausted_{entry_id}")


async def test_error_after_a_quota_hold_ends_the_hold(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A server error after the reset is an outage, not a quota hold."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=0, reset_in=timedelta(minutes=10))
    await _tick(hass, freezer, 45)
    assert data.monitor.status is ApiStatus.RATE_LIMITED
    mock_tado.fail("GET", ROOMS, status=500)

    await _run(hass, freezer, 15)

    assert not data.rooms.last_update_success
    assert _stale_since(data.rooms) is None
    assert _status(data) is ApiStatus.OK


async def test_quota_hold_without_reset_longer_than_max_stale(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without a reset from tado, room values older than 6 hours go unavailable."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=0)
    await _tick(hass, freezer, 45)
    assert data.rooms.stale_since is not None

    await _tick(hass, freezer, 6 * 3600)

    assert not data.rooms.last_update_success
    error = data.rooms.last_exception
    assert isinstance(error, UpdateFailed)
    assert error.translation_key == "quota_exhausted"
    assert data.home.last_update_success
    assert data.monitor.status is ApiStatus.RATE_LIMITED


async def test_quota_hold_with_reset_serves_until_the_reset(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A hold that ends at a reset tado sent serves the values until then, past 6 h."""
    data = await _setup(hass, freezer)
    before = data.rooms.data
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=8))
    await _tick(hass, freezer, 45)

    await _tick(hass, freezer, 6 * 3600)
    await _run(hass, freezer, 23, step=300)

    assert data.rooms.last_update_success
    assert data.rooms.data == before
    assert data.monitor.status is ApiStatus.RATE_LIMITED

    await _run(hass, freezer, 2, step=300)

    assert data.rooms.last_update_success
    assert _stale_since(data.rooms) is None
    assert _status(data) is ApiStatus.OK


async def test_reload_during_a_known_hold_serves_until_the_reset(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A reload 7 h into an 8 h hold serves the room values until the reset."""
    data = await _setup(hass, freezer)
    entry = data.rooms.config_entry
    before = data.rooms.data
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=8))
    await _tick(hass, freezer, 45)
    began = data.rooms.stale_since
    await _tick(hass, freezer, 7 * 3600)
    assert data.rooms.last_update_success

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    reloaded: TadoXRuntimeData = entry.runtime_data
    assert reloaded.rooms.data == before
    assert reloaded.rooms.stale_since == began
    assert _status(reloaded) is ApiStatus.RATE_LIMITED

    mock_tado.clear_calls()
    await _run(hass, freezer, 11, step=300)  # until just before the reset

    assert mock_tado.calls() == []
    assert reloaded.rooms.last_update_success
    assert reloaded.rooms.data == before

    await _run(hass, freezer, 2, step=300)

    assert mock_tado.calls("GET", ROOMS)
    assert _stale_since(reloaded.rooms) is None
    assert _status(reloaded) is ApiStatus.OK


async def test_command_during_a_known_hold_keeps_its_start_across_a_reload(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A boost 7 h into an 8 h hold at the poll floor does not restart the hold.

    Its answer is newer quota information inside the same hold, so a reload
    still serves the room values read when the hold began until the reset.
    """
    data = await _setup(hass, freezer)
    entry = data.rooms.config_entry
    before = data.rooms.data
    mock_tado.set_quota(remaining=20, reset_in=timedelta(hours=8))
    await _tick(hass, freezer, 45)
    await data.rooms.async_request_refresh()
    await _tick(hass, freezer, 5)
    began = data.rooms.stale_since
    assert began is not None
    await _tick(hass, freezer, 7 * 3600)

    await _press(hass, "boost_all")
    await _tick(hass, freezer, 5)

    assert mock_tado.calls("POST", "/homes/1000002/quickActions/boost")
    assert data.monitor.info is not None
    assert data.monitor.info.observed_at > began + timedelta(hours=7)
    assert data.rooms.stale_since == began

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    reloaded: TadoXRuntimeData = entry.runtime_data
    assert reloaded.rooms.data == before
    assert reloaded.rooms.stale_since == began
    assert _status(reloaded) is ApiStatus.RATE_LIMITED

    mock_tado.clear_calls()
    await _run(hass, freezer, 11, step=300)  # until just before the reset

    assert mock_tado.calls() == []
    assert reloaded.rooms.last_update_success

    await _run(hass, freezer, 2, step=300)

    assert mock_tado.calls("GET", ROOMS)
    assert _stale_since(reloaded.rooms) is None


async def test_hold_does_not_revive_values_older_than_max_stale(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Room values already older than 6 h when a hold begins stay unavailable."""
    data = await _setup(hass, freezer)
    mock_tado.fail("GET", ROOMS, status=503)
    await _tick(hass, freezer, 6 * 3600 + 60)
    assert not data.rooms.last_update_success
    mock_tado.clear_failures()
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=8))

    await _run(hass, freezer, 3)

    assert not data.rooms.last_update_success
    error = data.rooms.last_exception
    assert isinstance(error, UpdateFailed)
    assert error.translation_key == "quota_exhausted"


async def test_network_error_makes_data_unavailable(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Network failures are not a quota hold: the data goes unavailable."""
    data = await _setup(hass, freezer)
    mock_tado.fail("GET", ROOMS, exc=ClientConnectionError(), times=1)

    await _poll(freezer, data.rooms)

    assert not data.rooms.last_update_success
    assert isinstance(data.rooms.last_exception, UpdateFailed)
    assert data.rooms.last_exception.translation_key == "cannot_connect"
    assert data.rooms.stale_since is None

    await _poll(freezer, data.rooms)

    assert data.rooms.last_update_success


@pytest.mark.parametrize(
    ("status", "body", "key", "placeholders"),
    [
        (503, None, "server_error", {"status": "503"}),
        (
            422,
            {"errors": [{"code": "not.allowed", "title": "Wohnzimmer of Haus B"}]},
            "request_rejected",
            {"status": "422", "code": "not.allowed"},
        ),
        (422, b"", "request_rejected", {"status": "422", "code": "-"}),
        (200, {"not": "a list"}, "api_error", {"error": "rooms"}),
        (200, b"<html>maintenance</html>", "api_error", {"error": ROOMS_REQUEST}),
    ],
    ids=["server", "request", "request_without_body", "parse", "not_json"],
)
async def test_failures_of_a_required_endpoint(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    status: int,
    body: Any,
    key: str,
    placeholders: dict[str, str] | None,
) -> None:
    """Server, request and parse errors are translated update failures.

    The message names tado's error code or the endpoint template, never the
    error title, a home id or the JSON path.
    """
    data = await _setup(hass, freezer)
    mock_tado.fail("GET", ROOMS, status=status, body=body, times=1)

    await _poll(freezer, data.rooms)

    error = data.rooms.last_exception
    assert isinstance(error, UpdateFailed)
    assert error.translation_key == key
    assert error.translation_placeholders == placeholders


async def test_optional_failure_keeps_the_previous_value(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing optional endpoint keeps its value and warns once until it recovers."""
    data = await _setup(hass, freezer)
    weather = data.home.data.weather
    mock_tado.fail("GET", WEATHER, status=500)
    mock_tado.home(HOME_B).weather["outsideTemperature"]["celsius"] = 1.0

    await _run(hass, freezer, 6, step=300)

    assert data.home.last_update_success
    assert data.home.data.weather == weather
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "weather" in record.getMessage()
    ]
    assert len(warnings) == 1

    mock_tado.clear_failures()
    await _run(hass, freezer, 3, step=300)

    assert data.home.data.weather is not None
    assert data.home.data.weather.outdoor_temperature == 1.0
    assert "Reading weather of home …0002 works again" in caplog.text


async def test_optional_rate_limit_is_not_swallowed(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A 429 of an optional endpoint starts a quota hold for the whole tick."""
    data = await _setup(hass, freezer)
    mock_tado.fail("GET", WEATHER, status=429, times=1)

    await _run(hass, freezer, 3, step=300)

    assert data.home.stale_since is not None
    assert data.monitor.status is ApiStatus.RATE_LIMITED


async def test_flow_temperature_absent_is_reprobed_daily(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A 404 marks flow temperature absent; it is read again once a day (C20)."""
    del mock_tado.home(HOME_B).info["supportsFlowTemperatureOptimization"]
    data = await _setup(hass, freezer)

    assert data.devices.data.flow_status is FeatureStatus.ABSENT
    assert _count(mock_tado, FLOW) == 1

    await _run(hass, freezer, 4, step=900)
    assert _count(mock_tado, FLOW) == 1

    mock_tado.home(HOME_B).flow_temperature = {
        "maxFlowTemperature": 55,
        "maxFlowTemperatureConstraints": {"min": 30, "max": 80},
        "autoAdaptation": {"enabled": True, "maxFlowTemperature": 50},
    }
    await _tick(hass, freezer, 86400)

    assert _count(mock_tado, FLOW) == 2
    assert _flow_status(data) is FeatureStatus.PRESENT
    assert data.devices.data.flow_temperature is not None
    assert data.devices.data.flow_temperature.max_flow_temperature == 55


async def test_flow_temperature_unsupported_is_never_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A home that reports no flow temperature support costs no calls for it."""
    await _setup(hass, freezer)

    await _tick(hass, freezer, 2 * 86400)

    assert _count(mock_tado, FLOW) == 1
    assert _count(mock_tado, HOME_INFO) == 2


async def test_air_comfort_follows_home_eligibility(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A home without air comfort eligibility never reads airComfort."""
    mock_tado.home(HOME_B).info["isAirComfortEligible"] = False

    await _setup(hass, freezer)

    assert _count(mock_tado, AIR_COMFORT) == 0


async def test_optional_features_follow_the_options(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Phones and holiday mode are read when enabled; phones are never cached."""
    data = await _setup(hass, freezer, mobile_devices=True, holiday_mode=True)

    assert data.home.data.mobile_devices is not None
    assert set(data.home.data.mobile_devices) == {5000001, 5000002, 5000003}
    assert data.devices.data.holiday_mode is not None
    assert data.monitor.plan.rooms == timedelta(seconds=55)
    assert data.cache.payload("mobile_devices") is None
    assert data.cache.payload("holiday_mode") is not None


async def test_unchanged_data_notifies_nobody(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """An unchanged poll writes no states; a change notifies once."""
    data = await _setup(hass, freezer)
    updates: list[None] = []
    data.rooms.async_add_listener(lambda: updates.append(None))

    await _run(hass, freezer, 3)
    assert updates == []

    room = mock_tado.home(HOME_B).room(1)
    assert room is not None
    room["sensorDataPoints"]["insideTemperature"]["value"] = 23.0
    await _tick(hass, freezer, 45)

    assert len(updates) == 1


async def test_throttle_stretches_intervals(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Little quota left without a known reset stretches every period (§4.4)."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=1800)

    await _tick(hass, freezer, 45)

    assert data.monitor.mode is PlanMode.THROTTLED
    assert data.monitor.status is ApiStatus.THROTTLED
    assert data.monitor.factor == pytest.approx(2521 / 799, rel=0.01)
    assert data.rooms.update_interval == timedelta(seconds=142)
    assert data.home.update_interval == timedelta(seconds=947)


async def test_conserving_pauses_optional_endpoints(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At the reserve only required endpoints poll, slowly; commands still pass."""
    data = await _setup(hass, freezer)
    entry_id = data.rooms.config_entry.entry_id
    mock_tado.set_quota(remaining=900)

    await _tick(hass, freezer, 45)

    assert data.monitor.mode is PlanMode.CONSERVING
    assert data.monitor.status is ApiStatus.CONSERVING
    assert data.rooms.update_interval == timedelta(hours=1)
    assert data.home.update_interval == timedelta(hours=6)
    assert "polling slows down" in caplog.text
    assert not issue_registry.async_get_issue(DOMAIN, f"quota_low_{entry_id}")

    mock_tado.clear_calls()
    await _tick(hass, freezer, 6 * 3600)

    assert {call.url.path for call in mock_tado.calls("GET")} == {ROOMS, STATE}
    issue = issue_registry.async_get_issue(DOMAIN, f"quota_low_{entry_id}")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["reserve"] == "1000"

    mock_tado.set_quota(remaining=15000)
    await _poll(freezer, data.rooms)

    assert _mode(data) is PlanMode.OK
    assert not issue_registry.async_get_issue(DOMAIN, f"quota_low_{entry_id}")
    assert "back to its plan" in caplog.text


async def test_low_quota_tier_issue(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A free-tier quota plans for 100 calls and raises an issue until it grows."""
    mock_tado.set_quota(limit=100, remaining=100)
    data = await _setup(hass, freezer)
    entry_id = data.rooms.config_entry.entry_id

    issue = issue_registry.async_get_issue(DOMAIN, f"low_quota_tier_{entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {
        "home": "Haus B",
        "limit": "100",
        "rooms_minutes": str(round(data.monitor.plan.rooms.total_seconds() / 60)),
    }
    assert data.monitor.plan.calls_per_day <= 72

    mock_tado.set_quota(limit=20000, remaining=19000)
    await _poll(freezer, data.rooms)

    assert not issue_registry.async_get_issue(DOMAIN, f"low_quota_tier_{entry_id}")


async def test_optimistic_patch_survives_a_stale_read(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A patch is re-applied until the server agrees, for at most 60 s."""
    data = await _setup(hass, freezer)
    rooms = data.rooms
    room = mock_tado.home(HOME_B).room(1)
    assert room is not None

    def turn_off(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        return snapshot.with_room(1, setting=OFF)

    async def verify() -> None:
        await rooms.async_request_refresh()
        await _tick(hass, freezer, 5)

    rooms.async_apply_optimistic({1: turn_off})
    assert _room_power(data, 1) is Power.OFF
    mock_tado.clear_calls()

    await verify()
    assert len(mock_tado.calls("GET", ROOMS)) == 1
    assert _room_power(data, 1) is Power.OFF

    room["setting"] = {"power": "OFF", "temperature": None}
    await verify()
    room["setting"] = {"power": "ON", "temperature": {"value": 21.0}}
    await verify()

    assert len(mock_tado.calls("GET", ROOMS)) == 3
    assert _room_power(data, 1) is Power.ON

    rooms.async_apply_optimistic({1: turn_off}, ttl=timedelta(seconds=60))
    freezer.tick(timedelta(seconds=16))
    await _poll(freezer, rooms)

    assert _room_power(data, 1) is Power.ON


async def test_optimistic_patch_needs_data(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A coordinator without data ignores a patch."""
    data = await _setup(hass, freezer)
    entry = data.rooms.config_entry
    rooms = RoomsCoordinator(
        hass,
        entry,
        data.api,
        data.monitor,
        None,
        telemetry=data.telemetry,
        reauth=data.reauth,
    )

    rooms.async_apply_optimistic({1: lambda snapshot: snapshot})

    assert rooms.data is None


async def test_refresh_before_anything_is_due_sends_nothing(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A refresh of the plan only reads endpoints whose period has passed.

    Nothing came from tado, so the update times stay as they are.
    """
    data = await _setup(hass, freezer)
    before = data.rooms.data
    updated = {
        coordinator.KIND: coordinator.last_live_update
        for coordinator in (data.rooms, data.home, data.devices)
    }
    mock_tado.clear_calls()
    freezer.tick(timedelta(seconds=30))

    for coordinator in (data.rooms, data.home, data.devices):
        await coordinator.async_refresh()

    assert mock_tado.calls() == []
    assert data.rooms.data is before
    assert {
        coordinator.KIND: coordinator.last_live_update
        for coordinator in (data.rooms, data.home, data.devices)
    } == updated


async def test_a_longer_interval_leaves_a_tick_that_changes_no_update_time(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """The timer armed before the plan slowed down sends nothing and moves nothing.

    Home Assistant arms the next poll before the quota monitor lengthens the
    interval, so one tick at the old interval finds nothing due.
    """
    data = await _setup(hass, freezer)
    updated = entity_id(hass, "sensor", f"{HOME_B}_data_updated")
    mock_tado.set_quota(remaining=900)
    await _tick(hass, freezer, 45)
    assert _mode(data) is PlanMode.CONSERVING
    assert data.rooms.update_interval == timedelta(hours=1)
    fetched = data.rooms.last_live_update
    assert fetched == dt_util.utcnow()
    mock_tado.clear_calls()

    await _tick(hass, freezer, 45)

    assert mock_tado.calls() == []
    assert data.rooms.last_live_update == fetched
    assert state_of(hass, updated).state == fetched.isoformat()


async def test_poll_floor_without_reset_fails_setup(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Below the poll floor without a known reset, setup waits; the reason says so."""
    freezer.move_to(START)
    mock_tado.set_quota(remaining=15)
    entry = make_entry()

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "quota_exhausted_unknown_reset"


def _sent(err: TadoXError) -> TadoXError:
    """Tag an error with its request, as the transport does."""
    err.request = DEVICE_REQUEST
    return err


class _Probe(TadoXEntity[RoomsCoordinator]):
    """Entity whose action raises a given client error."""

    def __init__(self, coordinator: RoomsCoordinator, error: TadoXError) -> None:
        super().__init__(
            coordinator,
            EntityDescription(key="probe"),
            unique_id="probe",
            device=(DOMAIN, str(HOME_B)),
        )
        self.error = error

    @handle_tado_errors
    async def async_act(self, value: int) -> int:
        if value:
            raise self.error
        return value


@pytest.mark.parametrize(
    ("error", "key", "placeholders"),
    [
        (
            TadoXRateLimitError(None, RESET, local=True),
            "quota_exhausted",
            {"reset": "2026-10-30 03:00"},
        ),
        (
            TadoXRateLimitError(None, None, local=False),
            "quota_exhausted_unknown_reset",
            None,
        ),
        (TadoXConnectionError("down"), "cannot_connect", None),
        (TadoXServerError(502), "cannot_connect", None),
        (TadoXHomeNotXError(HOME_B), "not_tado_x_home", {"home": "Haus B"}),
        (TadoXNotFoundError(404), "not_found", None),
        (
            TadoXForbiddenError(403, "forbidden", "Not allowed in Haus B"),
            "request_rejected",
            {"status": "403", "code": "forbidden"},
        ),
        (TadoXRequestError(422), "request_rejected", {"status": "422", "code": "-"}),
        (
            TadoXRequestError(422, "Haus B 1000002", None),
            "request_rejected",
            {"status": "422", "code": "<str len=14>"},
        ),
        (TadoXParseError("rooms", "$.id"), "api_error", {"error": "rooms"}),
        (
            _sent(TadoXParseError(DEVICE_PATH, "$")),
            "api_error",
            {"error": DEVICE_REQUEST},
        ),
        (TadoXError("odd"), "api_error", {"error": "TadoXError"}),
    ],
    ids=[
        "quota",
        "quota_unknown",
        "connection",
        "server",
        "not_x",
        "not_found",
        "forbidden",
        "request",
        "odd_code",
        "parse",
        "parse_after_request",
        "other",
    ],
)
async def test_entity_actions_raise_translated_errors(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    error: TadoXError,
    key: str,
    placeholders: dict[str, str] | None,
) -> None:
    """Client errors of entity actions become translated Home Assistant errors."""
    data = await _setup(hass, freezer)
    probe = _Probe(data.rooms, error)

    assert await probe.async_act(0) == 0
    with pytest.raises(HomeAssistantError) as raised:
        await probe.async_act(1)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == placeholders
    assert probe.available


async def test_entity_action_auth_error_starts_reauth(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """An auth error of an action starts a reauth flow."""
    data = await _setup(hass, freezer)
    entry = data.rooms.config_entry
    probe = _Probe(data.rooms, TadoXAuthError("invalid_grant"))

    with pytest.raises(HomeAssistantError) as raised:
        await probe.async_act(1)
    await hass.async_block_till_done()

    assert raised.value.translation_key == "auth_failed"
    assert entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_monitor_listeners(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Status listeners run after every response and poll until removed."""
    data = await _setup(hass, freezer)
    calls: list[None] = []
    remove: Callable[[], None] = data.monitor.async_add_listener(
        lambda: calls.append(None)
    )

    await _poll(freezer, data.rooms)
    seen = len(calls)
    remove()
    remove()
    await _poll(freezer, data.rooms)

    assert seen >= 2
    assert len(calls) == seen
    assert data.monitor.calls_24h() == len(mock_tado.calls())
    assert data.monitor.reset_time is None


@pytest.mark.parametrize("writes", [False, True], ids=["idle", "offset_every_10_min"])
async def test_optimistic_writes_keep_the_poll_cadence(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    writes: bool,
) -> None:
    """Writes that show at once do not postpone the next poll of their coordinator."""
    await _setup(hass, freezer)
    offset = er.async_get(hass).async_get_entity_id(
        NUMBER_DOMAIN, DOMAIN, "VA0000000101_offset"
    )
    assert offset is not None
    mock_tado.clear_calls()

    for minute in range(4 * 60):
        await _tick(hass, freezer, 60)
        if writes and minute % 10 == 0:
            await hass.services.async_call(
                NUMBER_DOMAIN,
                SERVICE_SET_VALUE,
                {ATTR_ENTITY_ID: offset, ATTR_VALUE: (minute % 20) / 10},
                blocking=True,
            )

    assert _count(mock_tado, TOPOLOGY) == 16
    assert _count(mock_tado, RUNNING_TIMES) == 4


async def _climate(
    hass: HomeAssistant, service: str, room_id: int, **data: Any
) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        service,
        {ATTR_ENTITY_ID: climate_id(hass, room_id), **data},
        blocking=True,
    )


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, BUTTON_DOMAIN, f"{HOME_B}_{key}")},
        blocking=True,
    )


def _modes(hass: HomeAssistant) -> dict[int, str]:
    return {
        room_id: state_of(hass, climate_id(hass, room_id)).state
        for room_id in range(1, 7)
    }


@pytest.mark.parametrize(
    ("first", "data"),
    [
        (SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 24}),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.OFF}),
    ],
    ids=["heat", "off"],
)
async def test_resume_replaces_an_earlier_write_of_the_room(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    first: str,
    data: dict[str, Any],
) -> None:
    """Auto right after a manual write shows the schedule tado runs again."""
    await _setup(hass, freezer)

    await _climate(hass, first, 1, **data)
    await _climate(hass, SERVICE_SET_HVAC_MODE, 1, hvac_mode=HVACMode.AUTO)
    await _tick(hass, freezer, 5)

    state = state_of(hass, climate_id(hass, 1))
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes[ATTR_HVAC_ACTION] != HVACAction.OFF


@pytest.mark.parametrize(
    ("first", "data"),
    [
        (SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: 23}),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.OFF}),
    ],
    ids=["heat", "off"],
)
async def test_boost_replaces_an_earlier_write_of_the_room(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    first: str,
    data: dict[str, Any],
) -> None:
    """Boost right after a manual write shows the boost tado runs."""
    await _setup(hass, freezer)

    await _climate(hass, first, 1, **data)
    await _press(hass, "boost_all")
    await _tick(hass, freezer, 5)

    state = state_of(hass, climate_id(hass, 1))
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 25.0
    assert state.attributes[ATTR_HVAC_ACTION] != HVACAction.OFF


async def test_resume_schedules_replaces_disable_all(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Resume schedules right after disable all shows every room on its schedule."""
    await _setup(hass, freezer)

    await _press(hass, "disable_all")
    await _press(hass, "resume_schedules")

    assert set(_modes(hass).values()) == {HVACMode.AUTO}

    await _tick(hass, freezer, 5)

    assert set(_modes(hass).values()) == {HVACMode.AUTO}
    assert state_of(hass, climate_id(hass, 1)).attributes[ATTR_TEMPERATURE] == 21.0


async def test_room_write_after_disable_all_keeps_the_other_rooms_off(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A write to one room supersedes disable all for that room only."""
    await _setup(hass, freezer)
    home = mock_tado.home(HOME_B)
    before = copy.deepcopy(home.rooms)

    await _press(hass, "disable_all")
    await _climate(hass, SERVICE_SET_TEMPERATURE, 1, temperature=23)
    home.rooms = before
    await _tick(hass, freezer, 5)

    modes = _modes(hass)
    assert modes.pop(1) == HVACMode.HEAT
    assert state_of(hass, climate_id(hass, 1)).attributes[ATTR_TEMPERATURE] == 23.0
    assert set(modes.values()) == {HVACMode.OFF}


async def _until_polled(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_tado: TadoCloudMock,
    path: str,
    limit: int,
) -> int:
    """Tick in 15 s steps until path was read; return the seconds it took."""
    for elapsed in range(15, limit + 1, 15):
        await _tick(hass, freezer, 15)
        if _count(mock_tado, path):
            return elapsed
    raise AssertionError(f"{path} was not read within {limit} s")


async def test_recovery_from_conserving_rearms_every_coordinator(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Once a poll sees the quota refilled, home and devices poll at their plan again."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=900)
    await _run(hass, freezer, 40)
    assert data.monitor.mode is PlanMode.CONSERVING
    assert data.devices.update_interval == timedelta(hours=24)
    mock_tado.set_quota(remaining=19000)
    mock_tado.clear_calls()

    await _until_polled(hass, freezer, mock_tado, ROOMS, 3600)
    assert _mode(data) is PlanMode.OK
    mock_tado.clear_calls()

    assert await _until_polled(hass, freezer, mock_tado, STATE, 300) <= 300
    assert _count(mock_tado, TOPOLOGY) == 1
    await _run(hass, freezer, 20)
    assert _count(mock_tado, ROOMS) >= 20
    assert _count(mock_tado, TOPOLOGY) == 2


async def test_known_reset_ends_conserving_on_time(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """At a reset time tado sent, polling returns to the plan without waiting an hour."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=900, reset_in=timedelta(minutes=30))
    await _tick(hass, freezer, 45)
    await _tick(hass, freezer, 30 * 60 - 45 - 60)
    assert data.monitor.mode is PlanMode.CONSERVING
    mock_tado.clear_calls()

    took = await _until_polled(hass, freezer, mock_tado, ROOMS, 3600)

    assert took <= 60 + 60
    assert _mode(data) is PlanMode.OK
    assert data.rooms.update_interval == timedelta(seconds=45)


async def test_required_read_that_closes_the_gate_keeps_its_data(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the required read uses up the quota, the optional reads stay local."""
    data = await _setup(hass, freezer)
    topology = mock_tado.home(HOME_B).topology
    topology["rooms"][0]["devices"][0]["batteryState"] = "LOW"
    mock_tado.fail(
        "GET",
        TOPOLOGY,
        status=200,
        body=topology,
        headers={"ratelimit": '"perday";r=0;t=3600'},
        times=1,
    )
    mock_tado.clear_calls()

    freezer.tick(timedelta(hours=1))
    await data.devices.async_refresh()

    # Rooms and home are due too; they may send their required reads while
    # the stale access token is refreshed, before the topology answer arrives.
    paths = [call.path for call in mock_tado.calls("GET")]
    assert paths[-1] == TOPOLOGY
    assert paths.count(TOPOLOGY) == 1
    assert not {HOME_INFO, RUNNING_TIMES, FLOW, HOLIDAY} & set(paths)
    assert data.devices.last_update_success
    assert data.devices.data.topology.devices["VA0000000101"].battery == "LOW"
    assert data.devices.data.running_times is not None
    assert _mode(data) is PlanMode.BLOCKED
    assert "Reading running_times" not in caplog.text


@pytest.mark.parametrize("generation", [None, "LINE_Y"], ids=["missing", "new"])
async def test_unknown_home_generation_warns_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    generation: str | None,
) -> None:
    """A home without a known generation loads as tado X and is logged once."""
    info = mock_tado.home(HOME_B).info
    if generation is None:
        del info["generation"]
    else:
        info["generation"] = generation

    data = await _setup(hass, freezer)
    await _tick(hass, freezer, 86400)

    assert data.rooms.config_entry.state is ConfigEntryState.LOADED
    assert _count(mock_tado, HOME_INFO) == 2
    warnings = [
        record
        for record in caplog.records
        if record.name == "custom_components.tado_x"
        and record.levelno == logging.WARNING
        and "no known generation" in record.getMessage()
    ]
    assert len(warnings) == 1


async def test_home_details_of_the_previous_generation_fail_setup(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Home details of the previous generation refuse the home even when hops answers."""
    mock_tado.home(HOME_B).info["generation"] = "PRE_LINE_X"
    freezer.move_to(START)
    entry = make_entry()

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert issue_registry.async_get_issue(DOMAIN, f"not_tado_x_home_{entry.entry_id}")


async def test_poll_errors_are_recorded_once_with_their_context(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """Failed polls are recorded as "poll <kind>"; answers and holds are not.

    The 404 of a home without flow temperature is an answer, and a token
    refresh that fails a poll keeps the context of the refresh.
    """
    data = await _setup(hass, freezer)
    assert data.telemetry.errors() == []
    mock_tado.fail("GET", ROOMS, status=503, times=1)
    mock_tado.fail("GET", WEATHER, status=500, times=2)

    await _poll(freezer, data.rooms)
    for _ in range(2):
        freezer.tick(timedelta(seconds=900))
        await data.home.async_refresh()
    freezer.tick(timedelta(minutes=10))
    mock_tado.fail("POST", "/oauth2/token", exc=ClientConnectionError(), times=1)
    await data.rooms.async_refresh()

    assert not data.rooms.last_update_success
    assert [
        (error.context, error.kind, error.endpoint, error.status, error.count)
        for error in data.telemetry.errors()
    ] == [
        ("poll rooms", "TadoXServerError", "GET hops:/homes/{home}/rooms", 503, 1),
        ("poll home", "TadoXServerError", "GET my:/homes/{home}/weather", 500, 2),
        ("token refresh", "TadoXConnectionError", "POST login:/oauth2/token", None, 1),
    ]


async def test_quota_holds_record_the_gate_once(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """A 429 and the refusals after it are one quota_gate error, not one per poll."""
    data = await _setup(hass, freezer)
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=1))

    for _ in range(3):
        await _poll(freezer, data.rooms)

    assert data.rooms.stale_since is not None
    assert [(error.kind, error.context) for error in data.telemetry.errors()] == [
        ("quota_gate", "quota")
    ]


async def test_entity_action_errors_are_recorded_as_commands(
    hass: HomeAssistant, mock_tado: TadoCloudMock, freezer: FrozenDateTimeFactory
) -> None:
    """An entity action records an error the client did not, by the entity key."""
    data = await _setup(hass, freezer)
    probe = _Probe(data.rooms, TadoXServerError(502))

    with pytest.raises(HomeAssistantError):
        await probe.async_act(1)

    error = data.telemetry.last_error
    assert error is not None
    assert (error.context, error.kind, error.status) == (
        "command probe",
        "TadoXServerError",
        502,
    )


async def test_optimistic_patches_are_logged_without_ids(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Applying and dropping a patch is logged by its target name only."""
    caplog.set_level(logging.DEBUG)
    data = await _setup(hass, freezer)
    rooms = data.rooms

    rooms.async_apply_optimistic(
        {("control", 1): lambda snapshot: snapshot.with_room(1, humidity=99.0)},
        ttl=timedelta(seconds=30),
    )
    await _poll(freezer, rooms)

    assert (
        "Applied the optimistic control change of the rooms data for up to 30 s"
        in caplog.text
    )
    assert (
        "Dropped the optimistic control change of the rooms data: "
        "tado did not confirm it in time" in caplog.text
    )
