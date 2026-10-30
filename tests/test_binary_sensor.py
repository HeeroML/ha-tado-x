"""Tests for the binary sensor platform."""

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_sensor import (
    START,
    setup_entry,
    snapshot_entities,
    state_of,
    tick,
    unique_ids,
)

HOME = "binary_sensor.haus_b"
LIVING = "binary_sensor.wohnzimmer"
LIVING_VALVE = "binary_sensor.radiator_valve_wohnzimmer"
KITCHEN_VALVE = "binary_sensor.radiator_valve_kueche"
BEDROOM_VALVE = "binary_sensor.radiator_valve_schlafzimmer"


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Start every test at the time the fixtures describe."""
    freezer.move_to(START)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_binary_sensors(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Every binary sensor of home B with phones and holiday mode turned on."""
    await setup_entry(hass, config_entry, mobile_devices=True, holiday_mode=True)

    snapshot_entities(hass, entity_registry, snapshot, config_entry, "binary_sensor")


async def test_depleted_battery_is_low(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """A DEPLETED battery is low too, not just a LOW one (#85, #87)."""
    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{BEDROOM_VALVE}_battery_low") == STATE_ON
    assert state_of(hass, "sensor.radiator_valve_schlafzimmer_battery") == "depleted"
    assert state_of(hass, f"{KITCHEN_VALVE}_battery_low") == STATE_ON
    assert state_of(hass, f"{LIVING_VALVE}_battery_low") == STATE_OFF


async def test_battery_low_appears_with_the_field(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A battery field tado sends only later still creates the sensor (#85)."""
    device = mock_tado.home(HOME_B).device("VA0000000101")
    assert device is not None
    del device["batteryState"]
    await setup_entry(hass, config_entry)

    assert hass.states.get(f"{LIVING_VALVE}_battery_low") is None
    assert state_of(hass, f"{LIVING_VALVE}_connectivity") == STATE_ON

    device["batteryState"] = "DEPLETED"
    await tick(hass, freezer, 900)

    assert state_of(hass, f"{LIVING_VALVE}_battery_low") == STATE_ON


async def test_values_tado_does_not_know_are_unknown(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Unknown battery and connection values and a missing heating power."""
    home = mock_tado.home(HOME_B)
    device = home.device("VA0000000101")
    assert device is not None
    device["batteryState"] = "ALMOST_EMPTY_BINARY_TEST"
    device["connection"] = {"state": "SLEEPING_BINARY_TEST"}
    living = home.room(1)
    assert living is not None
    living["heatingPower"] = None

    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{LIVING_VALVE}_battery_low") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING_VALVE}_connectivity") == STATE_UNKNOWN
    assert state_of(hass, f"{LIVING}_heating") == STATE_UNKNOWN


async def test_room_states(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Window, heating, manual control and connectivity of rooms and devices."""
    living = mock_tado.home(HOME_B).room(1)
    assert living is not None
    living["openWindow"] = {"activated": False, "expiryInSeconds": 600}

    await setup_entry(hass, config_entry)

    assert state_of(hass, f"{LIVING}_window_open") == STATE_OFF
    assert state_of(hass, "binary_sensor.bad_window_open") == STATE_ON
    assert state_of(hass, f"{LIVING}_heating") == STATE_ON
    assert state_of(hass, "binary_sensor.bad_heating") == STATE_OFF
    assert state_of(hass, f"{LIVING}_manual_control") == STATE_OFF
    assert state_of(hass, "binary_sensor.kueche_manual_control") == STATE_ON
    assert state_of(hass, f"{BEDROOM_VALVE}_connectivity") == STATE_OFF
    assert state_of(hass, "binary_sensor.bridge_x_connectivity") == STATE_ON


async def test_boost_is_disabled_by_default(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Only the boost sensors are disabled by default."""
    await setup_entry(hass, config_entry)

    disabled = {
        entity_entry.unique_id
        for entity_entry in er.async_entries_for_config_entry(
            entity_registry, config_entry.entry_id
        )
        if entity_entry.domain == "binary_sensor" and entity_entry.disabled_by
    }
    assert disabled == {f"{HOME_B}_{room}_boost" for room in range(1, 7)}


async def test_holiday_mode(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The holiday sensor follows the active flag tado reports."""
    await setup_entry(hass, config_entry, holiday_mode=True)
    assert state_of(hass, f"{HOME}_holiday_mode") == STATE_OFF

    holiday = mock_tado.home(HOME_B).holiday_mode
    holiday.update(enabled=True, active=True)
    await tick(hass, freezer, 900)

    assert state_of(hass, f"{HOME}_holiday_mode") == STATE_ON


async def test_holiday_mode_without_an_answer(
    hass: HomeAssistant, mock_tado: TadoCloudMock, config_entry: MockConfigEntry
) -> None:
    """Holiday mode tado never answered for is unknown."""
    mock_tado.fail("GET", "/homes/1000002/settings/holidayMode", status=500)

    await setup_entry(hass, config_entry, holiday_mode=True)

    assert state_of(hass, f"{HOME}_holiday_mode") == STATE_UNKNOWN


async def test_phones_at_home(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At home follows tado's geofencing; phones without geofencing are unknown."""
    phones = mock_tado.home(HOME_B).mobile_devices
    await setup_entry(hass, config_entry, mobile_devices=True)

    assert state_of(hass, "binary_sensor.phone_at_home") == STATE_ON
    assert state_of(hass, "binary_sensor.phone_2_at_home") == STATE_UNKNOWN
    assert state_of(hass, "binary_sensor.tablet_at_home") == STATE_UNKNOWN

    phones.append({**phones[0], "id": 5000004, "name": "Watch"})
    await tick(hass, freezer, 300)

    assert state_of(hass, "binary_sensor.watch_at_home") == STATE_ON


async def test_stale_phone_location_is_unknown(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An outdated location is unknown, like the tracker shows it, until it is fresh."""
    phone = mock_tado.home(HOME_B).mobile_devices[1]
    assert phone["location"] == {**phone["location"], "stale": True, "atHome": False}
    await setup_entry(hass, config_entry, mobile_devices=True)

    assert state_of(hass, "binary_sensor.phone_2_at_home") == STATE_UNKNOWN
    assert state_of(hass, "device_tracker.phone_2") == STATE_UNKNOWN

    phone["location"]["stale"] = False
    await tick(hass, freezer, 300)

    assert state_of(hass, "binary_sensor.phone_2_at_home") == STATE_OFF
    assert state_of(hass, "device_tracker.phone_2") == "not_home"


async def test_optional_sensors_follow_the_options(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without the options there is no holiday sensor and no phone sensor."""
    await setup_entry(hass, config_entry)

    binary_sensors = unique_ids(entity_registry, config_entry, "binary_sensor")
    assert f"{HOME_B}_holiday_mode" not in binary_sensors
    assert not [unique_id for unique_id in binary_sensors if "mobile" in unique_id]
    assert len(binary_sensors) == 6 * 4 + 12 * 2 + 1
