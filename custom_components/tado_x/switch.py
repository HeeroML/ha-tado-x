"""Switch platform: child lock, open-window mode and flow auto-adaptation."""

from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import Device, FlowTemperature, OpenWindow, RoomState, TadoXRequestError
from .const import DOMAIN
from .coordinator import DevicesCoordinator, RoomsCoordinator, TadoXCoordinator
from .devices import device_identifier, home_identifier, room_identifier
from .entity import TadoXEntity, async_add_dynamic_entities, handle_tado_errors
from .runtime import RoomsSnapshot, TadoXConfigEntry, TadoXRuntimeData

PARALLEL_UPDATES = 1

CHILD_LOCK_DESCRIPTION = SwitchEntityDescription(
    key="child_lock",
    translation_key="child_lock",
    entity_category=EntityCategory.CONFIG,
)
OPEN_WINDOW_DESCRIPTION = SwitchEntityDescription(
    key="open_window", translation_key="open_window"
)
AUTO_ADAPTATION_DESCRIPTION = SwitchEntityDescription(
    key="flow_temp_auto_adaptation",
    translation_key="flow_temp_auto_adaptation",
    entity_category=EntityCategory.CONFIG,
)


def _window_open(room: RoomState) -> bool:
    return room.open_window is not None and room.open_window.activated


def _open_window_patch(
    room_id: int, active: bool
) -> Callable[[RoomsSnapshot], RoomsSnapshot]:
    """Return a patch that shows open-window mode until the server agrees."""

    def patch(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        room = snapshot.rooms.get(room_id)
        if room is None or _window_open(room) is active:
            return snapshot
        if not active:
            return snapshot.with_room(room_id, open_window=None)
        expires_in = None if room.open_window is None else room.open_window.expires_in
        return snapshot.with_room(
            room_id, open_window=OpenWindow(activated=True, expires_in=expires_in)
        )

    return patch


def _entities(data: TadoXRuntimeData) -> Iterator[TadoXEntity[Any]]:
    """Yield a switch for every lock, room and boiler the topology reports."""
    snapshot = data.devices.data
    for serial, device in snapshot.topology.devices.items():
        if device.child_lock is not None:
            yield TadoXChildLockSwitch(data.devices, serial)
    for room_id in snapshot.topology.rooms:
        yield TadoXOpenWindowSwitch(data.rooms, room_id)
    if snapshot.flow_temperature is not None:
        yield TadoXAutoAdaptationSwitch(data.devices)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch entities of a tado X home."""
    data = entry.runtime_data
    async_add_dynamic_entities(
        entry, data.devices, async_add_entities, lambda: list(_entities(data))
    )


class _TadoXSwitch[C: TadoXCoordinator[Any]](TadoXEntity[C], SwitchEntity):
    """Switch whose on and off differ only in the value written."""

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set(False)

    @abstractmethod
    def _async_set(self, on: bool) -> Awaitable[None]:
        """Write the new state."""


class TadoXChildLockSwitch(_TadoXSwitch[DevicesCoordinator]):
    """Child lock of a device that reports one."""

    def __init__(self, coordinator: DevicesCoordinator, serial: str) -> None:
        """Initialize the switch of a device."""
        super().__init__(
            coordinator,
            CHILD_LOCK_DESCRIPTION,
            unique_id=f"{serial}_child_lock",
            device=device_identifier(serial),
        )
        self._serial = serial

    @property
    def _device(self) -> Device | None:
        return self.coordinator.data.topology.devices.get(self._serial)

    @override
    def _present(self) -> bool:
        return (device := self._device) is not None and device.child_lock is not None

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True while the child lock is on."""
        return None if (device := self._device) is None else device.child_lock

    @handle_tado_errors
    @override
    async def _async_set(self, on: bool) -> None:
        serial = self._serial
        await self.coordinator.api.async_update_device(serial, child_lock=on)
        self.coordinator.async_apply_optimistic(
            lambda snapshot: snapshot.with_device(serial, child_lock=on)
        )


class TadoXOpenWindowSwitch(_TadoXSwitch[RoomsCoordinator]):
    """Open-window mode of a room; it can only start after tado detected a window."""

    def __init__(self, coordinator: RoomsCoordinator, room_id: int) -> None:
        """Initialize the switch of a room."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            OPEN_WINDOW_DESCRIPTION,
            unique_id=f"{home_id}_{room_id}_open_window",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id

    @override
    def _present(self) -> bool:
        return self._room_id in self.coordinator.data.rooms

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True while open-window mode pauses the heating."""
        room = self.coordinator.data.rooms.get(self._room_id)
        return None if room is None else _window_open(room)

    @handle_tado_errors
    @override
    async def _async_set(self, on: bool) -> None:
        try:
            await self.coordinator.api.async_set_open_window(self._room_id, active=on)
        except TadoXRequestError as err:
            if not on:
                raise
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="open_window_not_detected"
            ) from err
        self.coordinator.async_apply_optimistic(_open_window_patch(self._room_id, on))
        await self.coordinator.async_request_refresh()


class TadoXAutoAdaptationSwitch(_TadoXSwitch[DevicesCoordinator]):
    """Automatic adaptation of the flow temperature of an OpenTherm boiler."""

    def __init__(self, coordinator: DevicesCoordinator) -> None:
        """Initialize the switch of the home."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            AUTO_ADAPTATION_DESCRIPTION,
            unique_id=f"{home_id}_flow_temp_auto_adaptation",
            device=home_identifier(home_id),
        )

    @property
    def _flow(self) -> FlowTemperature | None:
        return self.coordinator.data.flow_temperature

    @override
    def _present(self) -> bool:
        return self._flow is not None

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True while tado adapts the flow temperature."""
        return None if (flow := self._flow) is None else flow.auto_adaptation

    @handle_tado_errors
    @override
    async def _async_set(self, on: bool) -> None:
        await self.coordinator.api.async_set_flow_temperature(auto_adaptation=on)
        self.coordinator.async_apply_optimistic(
            lambda snapshot: snapshot.with_flow_temperature(auto_adaptation=on)
        )
