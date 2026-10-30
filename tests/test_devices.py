"""Tests for the device registry and dynamic entities."""

from collections.abc import Iterable
from datetime import timedelta
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryType
from homeassistant.helpers.entity import Entity, EntityDescription
from tests.tado_mock import HOME_B, TadoCloudMock
from tests.test_init import make_entry, setup_entry

START = "2026-10-30T10:00:00+00:00"


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


async def test_phones_are_registered_and_removed(
    hass: HomeAssistant,
    mock_tado: TadoCloudMock,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Phones are devices of the home while the phone feature is on."""
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

    mock_tado.home(HOME_B).mobile_devices.pop()
    await _tick(hass, freezer, 300)

    assert not device_registry.async_get_device_by_identifier(
        mobile_identifier(5000003), entry.entry_id
    )
    assert _device(device_registry, entry, mobile_identifier(5000002))


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
