"""Binary sensor platform of the tado X integration."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ConnectionState, Device, RoomState
from .coordinator import DevicesCoordinator, HomeCoordinator, RoomsCoordinator
from .devices import (
    device_identifier,
    home_identifier,
    mobile_identifier,
    room_identifier,
)
from .entity import TadoXEntity, async_add_dynamic_entities
from .runtime import TadoXConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TadoXRoomBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a binary sensor of the live state of a room."""

    is_on_fn: Callable[[RoomState], bool | None]


ROOM_BINARY_SENSORS: tuple[TadoXRoomBinarySensorEntityDescription, ...] = (
    TadoXRoomBinarySensorEntityDescription(
        key="window_open",
        translation_key="window_open",
        device_class=BinarySensorDeviceClass.WINDOW,
        is_on_fn=lambda room: (
            room.open_window is not None and room.open_window.activated
        ),
    ),
    TadoXRoomBinarySensorEntityDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda room: (
            None if room.heating_power is None else room.heating_power > 0
        ),
    ),
    TadoXRoomBinarySensorEntityDescription(
        key="overlay_active",
        translation_key="overlay_active",
        is_on_fn=lambda room: room.manual_control is not None,
    ),
    TadoXRoomBinarySensorEntityDescription(
        key="boost",
        translation_key="boost",
        entity_registry_enabled_default=False,
        is_on_fn=lambda room: room.boost is not None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class TadoXDeviceBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a binary sensor of a physical device that reports the field it reads."""

    is_on_fn: Callable[[Device], bool | None]
    exists_fn: Callable[[Device], bool] = lambda _: True


DEVICE_BINARY_SENSORS: tuple[TadoXDeviceBinarySensorEntityDescription, ...] = (
    TadoXDeviceBinarySensorEntityDescription(
        key="connectivity",
        translation_key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda device: (
            None
            if device.connection is None
            else device.connection is ConnectionState.CONNECTED
        ),
    ),
    TadoXDeviceBinarySensorEntityDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda device: device.has_battery,
        is_on_fn=lambda device: (
            None if device.battery is None else device.battery.is_low
        ),
    ),
)

HOLIDAY_MODE = BinarySensorEntityDescription(
    key="holiday_mode", translation_key="holiday_mode"
)
AT_HOME = BinarySensorEntityDescription(
    key="at_home",
    translation_key="at_home",
    device_class=BinarySensorDeviceClass.PRESENCE,
)


class TadoXRoomBinarySensor(TadoXEntity[RoomsCoordinator], BinarySensorEntity):
    """Binary sensor of the live state of a room."""

    entity_description: TadoXRoomBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: RoomsCoordinator,
        description: TadoXRoomBinarySensorEntityDescription,
        room_id: int,
    ) -> None:
        """Initialize a binary sensor of a room device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            description,
            unique_id=f"{home_id}_{room_id}_{description.key}",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id

    @override
    def _present(self) -> bool:
        return self._room_id in self.coordinator.data.rooms

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.is_on_fn(
            self.coordinator.data.rooms[self._room_id]
        )


class TadoXDeviceBinarySensor(TadoXEntity[DevicesCoordinator], BinarySensorEntity):
    """Binary sensor of a physical device."""

    entity_description: TadoXDeviceBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: DevicesCoordinator,
        description: TadoXDeviceBinarySensorEntityDescription,
        serial: str,
    ) -> None:
        """Initialize a binary sensor of a physical device."""
        super().__init__(
            coordinator,
            description,
            unique_id=f"{serial}_{description.key}",
            device=device_identifier(serial),
        )
        self._serial = serial

    @override
    def _present(self) -> bool:
        return self._serial in self.coordinator.data.topology.devices

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.is_on_fn(
            self.coordinator.data.topology.devices[self._serial]
        )


class TadoXHolidayBinarySensor(TadoXEntity[DevicesCoordinator], BinarySensorEntity):
    """Whether the holiday mode of the home is active."""

    def __init__(self, coordinator: DevicesCoordinator) -> None:
        """Initialize a binary sensor of the home device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            HOLIDAY_MODE,
            unique_id=f"{home_id}_{HOLIDAY_MODE.key}",
            device=home_identifier(home_id),
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True while the holiday mode is active."""
        holiday = self.coordinator.data.holiday_mode
        return None if holiday is None else holiday.active


class TadoXAtHomeBinarySensor(TadoXEntity[HomeCoordinator], BinarySensorEntity):
    """Whether tado's geofencing sees a phone at the home, independent of zones.

    The state is unknown while geofencing is off for the phone or its
    location is unknown or outdated, as on the phone's tracker.
    """

    def __init__(self, coordinator: HomeCoordinator, mobile_id: int) -> None:
        """Initialize a binary sensor of a phone device."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            AT_HOME,
            unique_id=f"{home_id}_mobile_{mobile_id}_{AT_HOME.key}",
            device=mobile_identifier(mobile_id),
        )
        self._mobile_id = mobile_id

    @override
    def _present(self) -> bool:
        return self._mobile_id in (self.coordinator.data.mobile_devices or {})

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the state; None without geofencing or a current location."""
        phone = (self.coordinator.data.mobile_devices or {})[self._mobile_id]
        if not phone.geo_tracking or phone.stale:
            return None
        return phone.at_home


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of a tado X home."""
    data = entry.runtime_data
    if data.monitor.enabled("holiday_mode"):
        async_add_entities([TadoXHolidayBinarySensor(data.devices)])

    def build_topology() -> Iterator[TadoXEntity[Any]]:
        topology = data.devices.data.topology
        for room_id in topology.rooms:
            for room_sensor in ROOM_BINARY_SENSORS:
                yield TadoXRoomBinarySensor(data.rooms, room_sensor, room_id)
        for device in topology.devices.values():
            for device_sensor in DEVICE_BINARY_SENSORS:
                if device_sensor.exists_fn(device):
                    yield TadoXDeviceBinarySensor(
                        data.devices, device_sensor, device.serial
                    )

    async_add_dynamic_entities(entry, data.devices, async_add_entities, build_topology)
    if not data.monitor.enabled("mobile_devices"):
        return

    def build_phones() -> Iterator[TadoXEntity[Any]]:
        for mobile_id in data.home.data.mobile_devices or {}:
            yield TadoXAtHomeBinarySensor(data.home, mobile_id)

    async_add_dynamic_entities(entry, data.home, async_add_entities, build_phones)
