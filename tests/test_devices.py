"""Tests for the device registry and dynamic entities."""

from collections.abc import Iterable
import copy
from datetime import timedelta
import logging
from typing import Any, override

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
    async_fire_time_changed,
)

from custom_components.tado_x.const import DOMAIN
from custom_components.tado_x.coordinator import DevicesCoordinator
from custom_components.tado_x.devices import (
    device_identifier,
    home_identifier,
    mobile_identifier,
    room_identifier,
)
from custom_components.tado_x.entity import (
    TadoXEntity,
    TadoXStatusEntity,
    async_add_dynamic_entities,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryType
from homeassistant.helpers.entity import Entity, EntityDescription
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_init import make_entry, setup_entry

START = "2026-10-30T10:00:00+00:00"
TABLET_ENTITIES = ("device_tracker.tablet", "binary_sensor.tablet_at_home")


@pytest.fixture(autouse=True)
def start_time(freezer: FrozenDateTimeFactory) -> None:
    """Start every test at the same time."""
    freezer.move_to(START)


def _device(
    registry: dr.DeviceRegistry, entry: MockConfigEntry, identifier: tuple[str, str]
) -> DeviceEntry:
    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None
    return device


def _names(registry: dr.DeviceRegistry, entry: MockConfigEntry) -> set[str | None]:
    return {
        device.name
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    }


def _states(hass: HomeAssistant, entity_ids: Iterable[str]) -> set[str]:
    states = {hass.states.get(entity_id) for entity_id in entity_ids}
    assert None not in states
    return {current.state for current in states if current is not None}


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _add_room(mock_tado: TadoCloudMock, room_id: int, serial: str) -> None:
    mock_tado.home(HOME_B).topology["rooms"].append(
        {
            "roomId": room_id,
            "roomName": "Gaestezimmer",
            "deviceManualControlTermination": {"type": "MANUAL"},
            "devices": [
                {
                    "serialNumber": serial,
                    "type": "VA04",
                    "firmwareVersion": "280.1",
                    "connection": {"state": "CONNECTED"},
                    "batteryState": "NORMAL",
                }
            ],
        }
    )


async def test_device_hierarchy(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    """Rooms hang off the home, valves and sensors off the Bridge X (C5)."""
    await setup_entry(hass, config_entry)

    home = _device(device_registry, config_entry, home_identifier(HOME_B))
    bridge = _device(device_registry, config_entry, device_identifier("IB0000000001"))
    room = _device(device_registry, config_entry, room_identifier(HOME_B, 1))
    valve = _device(device_registry, config_entry, device_identifier("VA0000000101"))
    sensor = _device(device_registry, config_entry, device_identifier("SU0000000101"))

    assert home.name == "Haus B"
    assert home.model == "tado X home"
    assert home.manufacturer == "Tado"
    assert home.entry_type is DeviceEntryType.SERVICE
    assert home.configuration_url == "https://app.tado.com"
    assert home.via_device_id is None

    assert bridge.name == "Bridge X"
    assert (bridge.model, bridge.model_id) == ("Bridge X", "IB02")
    assert bridge.serial_number == "IB0000000001"
    assert bridge.sw_version == "279.1"
    assert bridge.via_device_id == home.id

    assert room.name == "Wohnzimmer"
    assert room.model == "tado X room"
    assert room.via_device_id == home.id
    assert room.area_id is None

    assert valve.name == "Radiator valve Wohnzimmer"
    assert (valve.model, valve.model_id) == ("Radiator Valve X", "VA04")
    assert valve.serial_number == "VA0000000101"
    assert valve.sw_version == "280.1"
    assert valve.via_device_id == bridge.id
    assert valve.area_id is None

    assert sensor.name == "Temperature sensor Wohnzimmer"
    assert sensor.model == "Temperature Sensor X"
    assert sensor.via_device_id == bridge.id

    assert (
        len(dr.async_entries_for_config_entry(device_registry, config_entry.entry_id))
        == 20
    )


async def test_device_names_in_german(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Device names follow the language of Home Assistant."""
    hass.config.language = "de"

    await setup_entry(hass, config_entry)

    names = _names(device_registry, config_entry)
    assert "Heizkörperthermostat Wohnzimmer" in names
    assert "Temperatursensor Kueche" in names
    assert "Bridge X" in names
    assert "Haus B" in names


async def test_device_names_of_several_devices_and_unknown_types(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Devices of one type in a room are numbered; unassigned ones use the serial."""
    topology = mock_tado.home(HOME_B).topology
    topology["rooms"][0]["devices"].append(
        {"serialNumber": "VA0000000199", "type": "VA04", "firmwareVersion": "280.1"}
    )
    topology["otherDevices"] += [
        {"serialNumber": "TR0000000042", "type": "TR04", "firmwareVersion": "1.0"},
        {"serialNumber": "RU0000000043", "type": "RU04", "roomId": 2},
        {"serialNumber": "ZZ0000000044", "type": "ZZ99"},
    ]

    await setup_entry(hass, config_entry)

    second = _device(device_registry, config_entry, device_identifier("VA0000000199"))
    receiver = _device(device_registry, config_entry, device_identifier("TR0000000042"))
    thermostat = _device(
        device_registry, config_entry, device_identifier("RU0000000043")
    )
    unknown = _device(device_registry, config_entry, device_identifier("ZZ0000000044"))
    bridge = _device(device_registry, config_entry, device_identifier("IB0000000001"))

    assert second.name == "Radiator valve Wohnzimmer 2"
    assert receiver.name == "Wireless receiver 0042"
    assert receiver.area_id is None
    assert receiver.via_device_id == bridge.id
    assert thermostat.name == "Smart thermostat Kueche"
    assert thermostat.model == "Smart Thermostat X"
    assert thermostat.sw_version is None
    assert unknown.name == "Device 0044"
    assert (unknown.model, unknown.model_id) == ("ZZ99", "ZZ99")


async def test_devices_without_bridge_hang_off_the_home(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Without a Bridge X the physical devices are connected via the home."""
    mock_tado.home(HOME_B).topology["otherDevices"] = []

    await setup_entry(hass, config_entry)

    home = _device(device_registry, config_entry, home_identifier(HOME_B))
    valve = _device(device_registry, config_entry, device_identifier("VA0000000101"))
    assert valve.via_device_id == home.id


async def test_new_and_removed_devices_follow_live_topology(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new room appears after a topology read; a removed one goes, live only."""
    await setup_entry(hass, config_entry)
    _add_room(mock_tado, 7, "VA0000000107")

    await _tick(hass, freezer, 900)

    assert _device(device_registry, config_entry, room_identifier(HOME_B, 7))
    assert _device(device_registry, config_entry, device_identifier("VA0000000107"))

    mock_tado.home(HOME_B).topology["rooms"].pop()
    mock_tado.set_quota(remaining=0, reset_in=timedelta(hours=1))
    await _tick(hass, freezer, 900)

    assert config_entry.runtime_data.devices.stale_since is not None
    assert _device(device_registry, config_entry, room_identifier(HOME_B, 7))

    await _tick(hass, freezer, 3600)

    assert config_entry.runtime_data.devices.stale_since is None
    for identifier in (room_identifier(HOME_B, 7), device_identifier("VA0000000107")):
        assert not device_registry.async_get_device_by_identifier(
            identifier, config_entry.entry_id
        )
    assert _device(device_registry, config_entry, room_identifier(HOME_B, 1))
    assert _device(device_registry, config_entry, home_identifier(HOME_B))


async def test_unreadable_topology_items_remove_nothing(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device or room the parser skips keeps its device, entities and settings."""
    await setup_entry(hass, config_entry)
    valve = _device(device_registry, config_entry, device_identifier("VA0000000101"))
    device_registry.async_update_device(valve.id, name_by_user="Fenster links")
    devices = len(
        dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    )
    entities = len(
        er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    )
    topology = mock_tado.home(HOME_B).topology
    del topology["rooms"][0]["devices"][0]["type"]
    del topology["rooms"][1]["roomName"]

    await _tick(hass, freezer, 900)
    await _tick(hass, freezer, 900)

    assert config_entry.runtime_data.devices.last_update_success
    assert not config_entry.runtime_data.devices.data.topology.complete
    assert (
        len(dr.async_entries_for_config_entry(device_registry, config_entry.entry_id))
        == devices
    )
    assert (
        len(er.async_entries_for_config_entry(entity_registry, config_entry.entry_id))
        == entities
    )
    valve = _device(device_registry, config_entry, device_identifier("VA0000000101"))
    assert valve.name_by_user == "Fenster links"
    assert _device(device_registry, config_entry, room_identifier(HOME_B, 2))
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "malformed topology" in record.getMessage()
    ]
    assert len(warnings) == 2


async def test_unreadable_phone_items_remove_nothing(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A phone the parser skips keeps its device, tracker and at-home sensor."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "mobile_devices": True}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tablet = _device(device_registry, entry, mobile_identifier(5000003))
    device_registry.async_update_device(tablet.id, name_by_user="Kuechentablet")
    entities = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    phones = mock_tado.home(HOME_B).mobile_devices
    readable = copy.deepcopy(phones)
    for phone in phones[1:]:
        del phone["name"]

    await _tick(hass, freezer, 300)
    await _tick(hass, freezer, 300)

    tablet = _device(device_registry, entry, mobile_identifier(5000003))
    assert tablet.name_by_user == "Kuechentablet"
    assert _device(device_registry, entry, mobile_identifier(5000002))
    home = entry.runtime_data.home
    assert home.last_update_success
    assert home.data.mobile_devices is not None
    assert set(home.data.mobile_devices) == {5000001}
    assert not home.data.mobile_devices_complete
    assert (
        len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
        == entities
    )
    assert _states(hass, TABLET_ENTITIES) == {STATE_UNAVAILABLE}
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "malformed mobile_devices" in record.getMessage()
    ]
    assert len(warnings) == 1

    phones[:] = readable
    await _tick(hass, freezer, 300)

    assert home.data.mobile_devices_complete
    assert STATE_UNAVAILABLE not in _states(hass, TABLET_ENTITIES)


async def test_entities_return_with_their_device(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A room or device that drops out of a live read gets its entities back."""
    await setup_entry(hass, config_entry)
    _add_room(mock_tado, 7, "VA0000000107")
    live_rooms = mock_tado.home(HOME_B).rooms
    live_rooms.append({**copy.deepcopy(live_rooms[0]), "id": 7, "name": "Gaestezimmer"})
    await _tick(hass, freezer, 900)
    entities = {
        ("climate", f"{HOME_B}_7_climate"),
        ("binary_sensor", "VA0000000107_connectivity"),
        ("switch", "VA0000000101_child_lock"),
    }
    entity_ids = {
        unique_id: entity_registry.async_get_entity_id(domain, DOMAIN, unique_id)
        for domain, unique_id in entities
    }
    boost = ("binary_sensor", DOMAIN, f"{HOME_B}_7_boost")
    assert entity_registry.async_get_entity_id(*boost)
    for unique_id, entity_id in entity_ids.items():
        state = hass.states.get(str(entity_id))
        assert state is not None, unique_id
        assert state.state != STATE_UNAVAILABLE, unique_id

    topology = mock_tado.home(HOME_B).topology
    room = topology["rooms"].pop()
    valve = topology["rooms"][0]["devices"].pop(0)
    await _tick(hass, freezer, 900)

    for domain, unique_id in entities:
        assert entity_registry.async_get_entity_id(domain, DOMAIN, unique_id) is None
    assert entity_registry.async_get_entity_id(*boost) is None

    topology["rooms"].append(room)
    topology["rooms"][0]["devices"].insert(0, valve)
    await _tick(hass, freezer, 900)

    for unique_id, entity_id in entity_ids.items():
        state = hass.states.get(str(entity_id))
        assert state is not None, unique_id
        assert state.state != STATE_UNAVAILABLE, unique_id
    boost_id = entity_registry.async_get_entity_id(*boost)
    assert boost_id is not None
    boost_entry = entity_registry.async_get(boost_id)
    assert boost_entry is not None
    assert boost_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_phones_are_registered_and_removed(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Phones are devices of the home while the phone feature is on.

    A phone that returns gets its tracker and at-home sensor back.
    """
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "mobile_devices": True}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    home = _device(device_registry, entry, home_identifier(HOME_B))
    phone = _device(device_registry, entry, mobile_identifier(5000001))
    assert phone.name == "Phone"
    assert phone.model == "Android Model1"
    assert phone.sw_version == "15"
    assert phone.via_device_id == home.id

    phones = mock_tado.home(HOME_B).mobile_devices
    tablet = phones.pop()
    await _tick(hass, freezer, 300)

    assert not device_registry.async_get_device_by_identifier(
        mobile_identifier(5000003), entry.entry_id
    )
    assert _device(device_registry, entry, mobile_identifier(5000002))
    assert hass.states.get("device_tracker.tablet") is None

    phones.append(tablet)
    await _tick(hass, freezer, 300)

    assert _device(device_registry, entry, mobile_identifier(5000003))
    assert hass.states.get("device_tracker.tablet") is not None
    assert hass.states.get("binary_sensor.tablet_at_home") is not None


async def test_phones_stay_when_the_feature_is_off(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Without phone data the phone devices are left alone, but are removable."""
    config_entry.add_to_hass(hass)
    old_phone = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={mobile_identifier(5000001)},
        name="Phone",
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert device_registry.async_get(old_phone.id) is not None


class _RoomEntity(TadoXEntity[DevicesCoordinator]):
    """Entity of one room of the topology."""

    def __init__(self, coordinator: DevicesCoordinator, room_id: int) -> None:
        super().__init__(
            coordinator,
            EntityDescription(key="room"),
            unique_id=f"{HOME_B}_{room_id}_probe",
            device=room_identifier(HOME_B, room_id),
        )
        self.room_id = room_id

    @override
    def _present(self) -> bool:
        return self.room_id in self.coordinator.data.topology.rooms


class _Collector:
    """Stand-in for the add_entities callback of a platform."""

    def __init__(self) -> None:
        self.added: list[Entity] = []

    def __call__(
        self,
        new_entities: Iterable[Entity],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        self.added.extend(new_entities)


async def test_dynamic_entities_are_added_once(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Entities of new objects are added after an update; known ones are not."""
    await setup_entry(hass, config_entry)
    devices = config_entry.runtime_data.devices
    collector = _Collector()

    def build() -> Iterable[_RoomEntity]:
        return [
            _RoomEntity(devices, room_id) for room_id in devices.data.topology.rooms
        ]

    async_add_dynamic_entities(config_entry, devices, collector, build)
    assert [entity.unique_id for entity in collector.added] == [
        f"{HOME_B}_{room_id}_probe" for room_id in range(1, 7)
    ]
    first: Any = collector.added[0]
    assert first.available
    assert first.device_info == {"identifiers": {(DOMAIN, f"{HOME_B}_1")}}

    _add_room(mock_tado, 7, "VA0000000107")
    await _tick(hass, freezer, 900)

    assert len(collector.added) == 7
    assert collector.added[-1].unique_id == f"{HOME_B}_7_probe"

    mock_tado.home(HOME_B).topology["rooms"][0]["roomName"] = "Stube"
    await _tick(hass, freezer, 900)

    assert devices.data.topology.rooms[1].name == "Stube"
    assert len(collector.added) == 7
    room = dr.async_get(hass).async_get_device_by_identifier(
        room_identifier(HOME_B, 1), config_entry.entry_id
    )
    assert room is not None
    assert room.name == "Stube"


class _Status(TadoXStatusEntity):
    """Status entity whose state counts how often it was written."""

    _attr_name = "Probe status"
    _attr_unique_id = "probe_status"

    def __init__(self) -> None:
        self.writes = 0

    @property
    @override
    def state(self) -> str:
        self.writes += 1
        return str(self.writes)


async def test_status_entities_follow_the_monitor(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A status entity writes its state on every monitor update until removed."""
    await setup_entry(hass, config_entry)
    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    platform.config_entry = config_entry
    status = _Status()
    await platform.async_add_entities([status])
    writes = status.writes

    await _tick(hass, freezer, 300)

    assert status.writes > writes
    assert status.available
    assert not status.should_poll

    await platform.async_reset()
    writes = status.writes
    await _tick(hass, freezer, 300)

    assert status.writes == writes


async def test_status_entities_outside_an_entry_do_not_listen(
    hass: HomeAssistant,
) -> None:
    """Without a config entry there is no monitor to listen to."""
    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    status = _Status()

    await platform.async_add_entities([status])

    assert status.writes == 1


async def test_device_changes_are_logged_without_names(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Changes after the setup are logged by room id, type and masked serial."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "mobile_devices": True}
    )
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={device_identifier("VA0000000999")},
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    home = mock_tado.home(HOME_B)
    home.topology["rooms"].pop()
    home.mobile_devices.pop()

    await _tick(hass, freezer, 900)

    changes = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == "custom_components.tado_x"
        and record.getMessage().startswith(("Added", "Removed"))
    ]
    assert ("DEBUG", "Added room 1 to Haus 1000002") in changes
    assert ("DEBUG", "Added a phone to Haus 1000002") in changes
    assert ("DEBUG", "Added IB02 IB…0001 to Haus 1000002") in changes
    assert sorted(message for level, message in changes if level != "DEBUG") == [
        "Removed SU04 SU…0106 from Haus 1000002",
        "Removed VA04 VA…0106 from Haus 1000002",
        "Removed a phone from Haus 1000002",
        "Removed device VA…0999 from Haus 1000002",
        "Removed room 6 from Haus 1000002",
    ]
