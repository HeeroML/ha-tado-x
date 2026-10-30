"""Device registry of the tado X integration.

Devices are registered centrally before the platforms load, in the order
home, bridge, rooms, physical devices, phones; entities carry identifiers
only.
"""

from collections import defaultdict
from collections.abc import Iterator, Mapping
from typing import Final

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryType

from .client import Device, DeviceType, MobileDevice, Topology
from .const import CONFIGURATION_URL, DOMAIN, MANUFACTURER
from .runtime import TadoXConfigEntry

HOME_MODEL: Final = "tado X home"
ROOM_MODEL: Final = "tado X room"
BRIDGE_KEY: Final = "bridge"
DEVICE_KEY: Final = "device"
BRIDGE_MODEL: Final = "Bridge X"

# Translation key of the device name, and the model shown in the registry.
DEVICE_TYPES: Final[Mapping[DeviceType, tuple[str, str]]] = {
    DeviceType.VA04: ("radiator_valve", "Radiator Valve X"),
    DeviceType.SU04: ("temperature_sensor", "Temperature Sensor X"),
    DeviceType.TR04: ("wireless_receiver", "Wireless Receiver X"),
    DeviceType.RU04: ("smart_thermostat", "Smart Thermostat X"),
    DeviceType.IB02: (BRIDGE_KEY, BRIDGE_MODEL),
}

_MOBILE_PREFIX: Final = "mobile_"


def home_identifier(home_id: int) -> tuple[str, str]:
    """Return the identifier of the home device."""
    return DOMAIN, str(home_id)


def room_identifier(home_id: int, room_id: int) -> tuple[str, str]:
    """Return the identifier of a room device."""
    return DOMAIN, f"{home_id}_{room_id}"


def device_identifier(serial: str) -> tuple[str, str]:
    """Return the identifier of a physical device."""
    return DOMAIN, serial


def mobile_identifier(mobile_id: int) -> tuple[str, str]:
    """Return the identifier of a phone."""
    return DOMAIN, f"{_MOBILE_PREFIX}{mobile_id}"


def _type_key(device: Device) -> tuple[str, str]:
    if device.type is not None and device.type in DEVICE_TYPES:
        return DEVICE_TYPES[device.type]
    return DEVICE_KEY, device.type_raw


def device_name_placeholders(topology: Topology) -> dict[str, dict[str, str]]:
    """Return the name placeholders of every physical device by serial.

    room is the room name, or the last four characters of the serial for an
    unassigned device; index numbers devices of the same type in a room in
    serial order, starting with the second.
    """
    groups: defaultdict[tuple[int | None, str], list[str]] = defaultdict(list)
    for serial, device in topology.devices.items():
        groups[device.room_id, device.type_raw].append(serial)
    placeholders: dict[str, dict[str, str]] = {}
    for (room_id, _), serials in groups.items():
        room = topology.rooms.get(room_id) if room_id is not None else None
        for position, serial in enumerate(sorted(serials), start=1):
            placeholders[serial] = {
                "room": room.name if room is not None else serial[-4:],
                "index": "" if position == 1 else f" {position}",
            }
    return placeholders


def _phones(entry: TadoXConfigEntry) -> Mapping[int, MobileDevice]:
    data = entry.runtime_data
    if not data.monitor.enabled("mobile_devices"):
        return {}
    return data.home.data.mobile_devices or {}


def _current_identifiers(entry: TadoXConfigEntry) -> Iterator[tuple[str, str]]:
    """Yield the identifiers of every device tado reports for the entry."""
    data = entry.runtime_data
    home_id = data.api.home_id
    topology = data.devices.data.topology
    yield home_identifier(home_id)
    yield from (room_identifier(home_id, room_id) for room_id in topology.rooms)
    yield from (device_identifier(serial) for serial in topology.devices)
    yield from (mobile_identifier(mobile_id) for mobile_id in _phones(entry))


@callback
def _async_register(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Register or update every device, parents first."""
    registry = dr.async_get(hass)
    data = entry.runtime_data
    home_id = data.api.home_id
    home = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={home_identifier(home_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=HOME_MODEL,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
    topology = data.devices.data.topology
    parent = home.id
    if (bridge := topology.bridge) is not None:
        parent = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={device_identifier(bridge.serial)},
            translation_key=BRIDGE_KEY,
            manufacturer=MANUFACTURER,
            model=BRIDGE_MODEL,
            model_id=bridge.type_raw,
            serial_number=bridge.serial,
            sw_version=bridge.firmware,
            via_device_id=home.id,
        ).id
    for room in topology.rooms.values():
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={room_identifier(home_id, room.id)},
            name=room.name,
            manufacturer=MANUFACTURER,
            model=ROOM_MODEL,
            via_device_id=home.id,
        )
    names = device_name_placeholders(topology)
    for serial, device in topology.devices.items():
        if bridge is not None and serial == bridge.serial:
            continue
        key, model = _type_key(device)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={device_identifier(serial)},
            translation_key=key,
            translation_placeholders=names[serial],
            manufacturer=MANUFACTURER,
            model=model,
            model_id=device.type_raw,
            serial_number=serial,
            sw_version=device.firmware,
            via_device_id=parent,
        )
    for mobile_id, phone in _phones(entry).items():
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={mobile_identifier(mobile_id)},
            name=phone.name,
            model=" ".join(part for part in (phone.platform, phone.model) if part)
            or None,
            sw_version=phone.os_version,
            via_device_id=home.id,
        )


@callback
def _async_remove_stale(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Remove devices tado stopped reporting, judged on live, complete data only."""
    data = entry.runtime_data
    devices_live = (
        data.devices.last_update_success
        and data.devices.stale_since is None
        and data.devices.data.topology.complete
    )
    phones_live = (
        data.monitor.enabled("mobile_devices")
        and data.home.last_update_success
        and data.home.stale_since is None
        and data.home.data.mobile_devices is not None
        and data.home.data.mobile_devices_complete
    )
    if not devices_live and not phones_live:
        return
    registry = dr.async_get(hass)
    current = set(_current_identifiers(entry))
    home = home_identifier(data.api.home_id)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ours = {item for item in device.identifiers if item[0] == DOMAIN}
        if not ours or ours & current or home in ours:
            continue
        is_phone = any(value.startswith(_MOBILE_PREFIX) for _, value in ours)
        if phones_live if is_phone else devices_live:
            registry.async_remove_device(device.id)


@callback
def _async_sync(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    _async_register(hass, entry)
    _async_remove_stale(hass, entry)


@callback
def async_setup_device_registry(hass: HomeAssistant, entry: TadoXConfigEntry) -> None:
    """Register every device now and keep the registry in sync with the data."""
    _async_sync(hass, entry)

    @callback
    def _async_on_update() -> None:
        _async_sync(hass, entry)

    data = entry.runtime_data
    entry.async_on_unload(data.devices.async_add_listener(_async_on_update))
    entry.async_on_unload(data.home.async_add_listener(_async_on_update))


@callback
def async_is_current_device(entry: TadoXConfigEntry, device: DeviceEntry) -> bool:
    """Return True if the device is still reported by tado."""
    if entry.state is not ConfigEntryState.LOADED:
        return False
    return not device.identifiers.isdisjoint(_current_identifiers(entry))
