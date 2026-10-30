"""Tests for the sensor platform."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.tado_x.const import DOMAIN
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from tests.tado_mock import HOME_B, TadoCloudMock, load_fixture

START = "2026-10-30T10:00:00+00:00"
CACHED_AT = datetime(2026, 10, 30, 9, 30, tzinfo=UTC)
HOME = "sensor.haus_b"
LIVING = "sensor.wohnzimmer"
KITCHEN = "sensor.kueche"
STUDY = "sensor.arbeitszimmer"
VALVE = "sensor.radiator_valve_wohnzimmer"
SERIALS = [f"{kind}00000001{room:02d}" for room in range(1, 7) for kind in ("VA", "SU")]


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Start every test at the time the fixtures describe."""
    freezer.move_to(START)


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, **options: Any
) -> None:
    """Add an entry with changed options to hass and set it up."""
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **options}
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock forward and run what is due."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def snapshot_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    entry: MockConfigEntry,
    domain: str,
) -> None:
    """Snapshot the registry entry and state of every entity of one platform."""
    entries = [
        entity_entry
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if entity_entry.domain == domain
    ]
    assert entries
    for entity_entry in entries:
        assert entity_entry == snapshot(name=f"{entity_entry.entity_id}-entry")
        assert entity_entry.disabled_by is None
        state = hass.states.get(entity_entry.entity_id)
        assert state, entity_entry.entity_id
        assert state == snapshot(name=f"{entity_entry.entity_id}-state")


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    """Return the state of an entity that must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def unique_ids(
    entity_registry: er.EntityRegistry, entry: MockConfigEntry, domain: str
) -> set[str]:
    """Return the unique ids of one platform of an entry."""
    return {
        entity_entry.unique_id
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if entity_entry.domain == domain
    }


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_sensors(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Every sensor of home B with the default options."""
    await setup_entry(hass, config_entry)

    snapshot_entities(hass, entity_registry, snapshot, config_entry, "sensor")


async def test_entity_count_of_home_b(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The read platforms register their share of the 216 entities of home B."""
    await setup_entry(hass, config_entry)

    entries = [
        entity_entry
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, config_entry.entry_id
        )
        if entity_entry.domain in {"sensor", "binary_sensor", "device_tracker"}
    ]
    disabled = {
        entity_entry.unique_id
        for entity_entry in entries
        if entity_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    }
    assert len(entries) == 158
    assert disabled == {
        f"{HOME_B}_api_quota_limit",
        f"{HOME_B}_api_usage_percentage",
        f"{HOME_B}_presence_mode",
        *(f"{HOME_B}_{room}_next_time_block" for room in range(1, 7)),
        *(f"{HOME_B}_{room}_boost" for room in range(1, 7)),
        *(f"{serial}_device_temperature" for serial in SERIALS),
        *(f"{serial}_temperature_offset" for serial in SERIALS),
    }
    assert len(entries) - len(disabled) == 119


async def test_battery_states(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Battery states are lowercase and DEPLETED is a state of its own (#85)."""
    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{VALVE}_battery") == "normal"
    assert state_of(hass, "sensor.radiator_valve_kueche_battery") == "low"
    assert state_of(hass, "sensor.radiator_valve_schlafzimmer_battery") == "depleted"


async def test_unknown_battery_value(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A battery state tado adds later is unknown and warned about once."""
    home = mock_tado.home(HOME_B)
    for serial in ("VA0000000101", "VA0000000102"):
        device = home.device(serial)
        assert device is not None
        device["batteryState"] = "ALMOST_EMPTY_SENSOR_TEST"
    sensor = home.device("SU0000000101")
    assert sensor is not None
    sensor["batteryState"] = None

    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert state_of(hass, f"{VALVE}_battery") == STATE_UNKNOWN
    assert state_of(hass, "sensor.radiator_valve_kueche_battery") == (STATE_UNKNOWN)
    assert (
        state_of(hass, "sensor.temperature_sensor_wohnzimmer_battery") == STATE_UNKNOWN
    )
    assert caplog.text.count("ALMOST_EMPTY_SENSOR_TEST") == 1
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name == "homeassistant.components.sensor"
    ]


async def test_battery_sensor_appears_with_the_field(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device that reports its battery only later gets the sensor then (#85)."""
    device = mock_tado.home(HOME_B).device("VA0000000101")
    assert device is not None
    battery = device.pop("batteryState")

    await setup_entry(hass, config_entry)

    assert hass.states.get(f"{VALVE}_battery") is None

    device["batteryState"] = battery
    await tick(hass, freezer, 900)

    assert state_of(hass, f"{VALVE}_battery") == "normal"


async def test_control_mode(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Devices that report a control mode get a sensor for it (#66)."""
    device = mock_tado.home(HOME_B).device("VA0000000101")
    assert device is not None
    device["controlMode"] = "UNDERFLOOR_HEATING"

    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{VALVE}_control_mode") == "underfloor_heating"
    assert hass.states.get("sensor.radiator_valve_kueche_control_mode") is None


async def test_enum_states_are_lowercase(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Every ENUM sensor uses lowercase options and states."""
    device = mock_tado.home(HOME_B).device("VA0000000101")
    assert device is not None
    device["controlMode"] = "HEAT_PUMP"
    mock_tado.home(HOME_B).state = {"presence": "AWAY", "presenceLocked": True}

    await setup_entry(hass, config_entry)

    enums = [
        state
        for state in hass.states.async_all("sensor")
        if state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.ENUM
    ]
    # api_status, presence, weather, 2 air comfort levels per room, 12 batteries,
    # and the control mode of one valve.
    assert len(enums) == 3 + 6 * 2 + 12 + 1
    for state in enums:
        options = state.attributes["options"]
        assert options == [option.lower() for option in options]
        assert state.state in [*options, STATE_UNKNOWN], state.entity_id
    assert state_of(hass, f"{HOME}_presence") == "away"
    assert state_of(hass, f"{HOME}_weather") == "cloudy_partly"
    assert state_of(hass, f"{VALVE}_control_mode") == "heat_pump"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_timestamps_are_aware_and_never_past(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Room timestamps carry a zone; times tado reports in the past are unknown."""
    home = mock_tado.home(HOME_B)
    kitchen = home.room(2)
    assert kitchen is not None
    kitchen["nextScheduleChange"]["start"] = "2026-10-30T09:59:00Z"
    kitchen["manualControlTermination"]["projectedExpiry"] = "2026-10-30T09:59:00Z"
    kitchen["nextTimeBlock"]["start"] = "2026-10-30T09:59:00Z"

    await setup_entry(hass, config_entry)

    change = dt_util.parse_datetime(state_of(hass, f"{LIVING}_next_schedule_change"))
    assert change == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    assert change.tzinfo is not None
    assert state_of(hass, f"{LIVING}_next_schedule_temperature") == "18.0"
    assert state_of(hass, f"{KITCHEN}_next_schedule_change") == STATE_UNKNOWN
    assert state_of(hass, f"{KITCHEN}_next_schedule_temperature") == STATE_UNKNOWN
    assert state_of(hass, f"{KITCHEN}_manual_control_ends") == STATE_UNKNOWN
    assert state_of(hass, f"{KITCHEN}_next_time_block") == STATE_UNKNOWN


async def test_manual_control_end(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """The end of a manual control shows for timers and the next time block only."""
    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{KITCHEN}_manual_control_ends") == (
        "2026-10-30T10:45:10+00:00"
    )
    assert state_of(hass, "sensor.kinderzimmer_manual_control_ends") == (
        "2026-10-30T16:00:00+00:00"
    )
    assert state_of(hass, "sensor.schlafzimmer_manual_control_ends") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING}_manual_control_ends") == STATE_UNKNOWN


async def test_next_schedule_temperature_of_an_off_block(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A next block that turns the room off has a time but no temperature."""
    living = mock_tado.home(HOME_B).room(1)
    assert living is not None
    living["nextScheduleChange"]["setting"] = {"power": "OFF", "temperature": None}

    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{LIVING}_next_schedule_change") == (
        "2026-10-30T21:00:00+00:00"
    )
    assert state_of(hass, f"{LIVING}_next_schedule_temperature") == STATE_UNKNOWN


async def test_offset_sensor_uses_temperature_delta(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """An offset is a difference, so Fahrenheit users see it unconverted."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "VA0000000101_temperature_offset",
        suggested_object_id="valve_offset",
        disabled_by=None,
    )
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "VA0000000101_device_temperature",
        suggested_object_id="valve_temperature",
        disabled_by=None,
    )

    await setup_entry(hass, config_entry)

    offset = hass.states.get("sensor.valve_offset")
    assert offset is not None
    assert offset.state == "-1.5"
    assert offset.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TEMPERATURE_DELTA
    assert offset.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.CELSIUS
    measured = hass.states.get("sensor.valve_temperature")
    assert measured is not None
    assert measured.attributes[ATTR_UNIT_OF_MEASUREMENT] == (
        UnitOfTemperature.FAHRENHEIT
    )


async def test_api_status_stays_available_during_a_quota_hold(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """During a hold the status sensors report it and room values stay."""
    await setup_entry(hass, config_entry)
    assert state_of(hass, f"{HOME}_api_status") == "ok"
    assert state_of(hass, f"{HOME}_data_updated") == START
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=2))

    await tick(hass, freezer, 45)

    blocked_until = config_entry.runtime_data.monitor.blocked_until
    assert blocked_until is not None
    assert state_of(hass, f"{HOME}_api_status") == "rate_limited"
    assert state_of(hass, f"{HOME}_api_quota_reset") == blocked_until.isoformat()
    assert state_of(hass, f"{HOME}_api_quota_remaining") == "0"
    assert state_of(hass, f"{HOME}_api_calls_used_account") == "20000"
    assert state_of(hass, f"{HOME}_data_updated") == START
    assert state_of(hass, f"{LIVING}_temperature") == "21.1"

    await tick(hass, freezer, 2 * 3600 + 60)
    await tick(hass, freezer, 45)

    assert state_of(hass, f"{HOME}_api_status") == "ok"
    assert state_of(hass, f"{HOME}_data_updated") == dt_util.utcnow().isoformat()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_restart_into_a_quota_hold(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
) -> None:
    """After a restart in a hold, what tado did not say is unknown, not invented."""
    blocked_until = datetime.fromisoformat(START) + timedelta(hours=1)
    key = f"{DOMAIN}.{config_entry.entry_id}"
    fixtures = {
        "rooms": "rooms.json",
        "topology": "rooms_and_devices.json",
        "home_state": "state.json",
        "home_info": "home_1000002.json",
    }
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "schema": 1,
            "home_id": HOME_B,
            "payloads": {
                name: {"at": CACHED_AT.isoformat(), "raw": load_fixture(fixture)}
                for name, fixture in fixtures.items()
            },
            "quota": {
                "latest": {
                    "limit": 20000,
                    "window": 86400,
                    "remaining": None,
                    "reset_in": None,
                    "observed_at": CACHED_AT.isoformat(),
                },
                "blocked_until": blocked_until.isoformat(),
                "backoff_step": 0,
            },
            "calls": [],
            "flow_status": "absent",
        },
    }

    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert mock_tado.calls() == []
    assert state_of(hass, f"{HOME}_api_status") == "rate_limited"
    assert state_of(hass, f"{HOME}_api_quota_reset") == blocked_until.isoformat()
    assert state_of(hass, f"{HOME}_api_quota_limit") == "20000"
    assert state_of(hass, f"{HOME}_api_quota_remaining") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_api_calls_used_account") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_api_quota_used") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_api_calls_by_this_integration_24_h") == "0"
    assert state_of(hass, f"{HOME}_data_updated") == CACHED_AT.isoformat()
    assert state_of(hass, f"{LIVING}_temperature") == "21.1"
    assert state_of(hass, f"{HOME}_outdoor_temperature") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING}_comfort_level") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING}_heating_time_today") == STATE_UNKNOWN


async def test_heating_time_today(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Hours per room today; yesterday's total does not carry over midnight."""
    times = mock_tado.home(HOME_B).running_times
    times["runningTimes"][0]["zones"].pop()
    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{LIVING}_heating_time_today") == "1.0"
    assert state_of(hass, f"{KITCHEN}_heating_time_today") == "0.75"
    assert state_of(hass, f"{STUDY}_heating_time_today") == STATE_UNKNOWN

    mock_tado.fail("GET", "/v1/homes/1000002/runningTimes", status=500)
    device = mock_tado.home(HOME_B).device("SU0000000101")
    assert device is not None
    device["temperatureAsMeasured"] = 20.0
    # 23:05 UTC is five past midnight in the home's zone (Europe/Berlin).
    freezer.move_to("2026-10-30T23:05:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    data = config_entry.runtime_data.devices.data
    assert data.running_times is not None
    assert data.running_times.day.isoformat() == "2026-10-30"
    assert state_of(hass, f"{LIVING}_heating_time_today") == "0.0"


async def test_features_turned_off(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Weather, air comfort and running time sensors follow the feature options."""
    await setup_entry(
        hass, config_entry, weather=False, air_comfort=False, running_times=False
    )

    sensors = unique_ids(entity_registry, config_entry, "sensor")
    assert not [
        unique_id
        for unique_id in sensors
        if unique_id.startswith(f"{HOME_B}_weather_")
        or unique_id.endswith(
            ("_air_freshness", "_comfort_level", "_heating_time_today")
        )
    ]
    assert f"{HOME_B}_1_temperature" in sensors
    assert len(sensors) == 109 - 3 - 6 * 3


async def test_failed_optional_endpoints_leave_values_unknown(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Weather and air comfort that never answered are unknown, not unavailable."""
    mock_tado.fail("GET", "/api/v2/homes/1000002/weather", status=500)
    mock_tado.fail("GET", "/homes/1000002/airComfort", status=500)

    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert state_of(hass, f"{HOME}_outdoor_temperature") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_solar_intensity") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_weather") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING}_humidity_level") == STATE_UNKNOWN
    assert state_of(hass, f"{HOME}_presence") == "home"


async def test_rooms_and_devices_follow_the_topology(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new room gets sensors; its live values follow once tado reports them."""
    home = mock_tado.home(HOME_B)
    await setup_entry(hass, config_entry)
    guest_room = "sensor.gaestezimmer"
    guest_valve = "sensor.radiator_valve_gaestezimmer"
    room = {
        **load_fixture("rooms_and_devices.json")["rooms"][0],
        "roomId": 7,
        "roomName": "Gaestezimmer",
    }
    room["devices"] = [{**room["devices"][0], "serialNumber": "VA0000000107"}]
    home.topology["rooms"].append(room)

    await tick(hass, freezer, 900)

    assert state_of(hass, f"{guest_room}_temperature") == STATE_UNAVAILABLE
    assert state_of(hass, f"{guest_room}_heating_time_today") == STATE_UNKNOWN
    assert state_of(hass, f"{guest_valve}_battery") == "normal"

    home.rooms.append({**load_fixture("rooms.json")[0], "id": 7})
    await tick(hass, freezer, 45)

    assert state_of(hass, f"{guest_room}_temperature") == "21.1"

    home.topology["rooms"].pop()
    await tick(hass, freezer, 900)

    assert hass.states.get(f"{guest_valve}_battery") is None
    assert hass.states.get(f"{guest_room}_heating_time_today") is None
