"""Button platform: quick actions and identify."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import (
    ActiveOverride,
    DeviceType,
    Power,
    QuickAction,
    Setting,
    TerminationType,
)
from .climate import RoomPatch, control_target, resume_patch
from .coordinator import DevicesCoordinator, RoomsCoordinator
from .devices import device_identifier, home_identifier
from .entity import TadoXEntity, async_add_dynamic_entities, handle_tado_errors
from .runtime import RoomsSnapshot, TadoXConfigEntry

PARALLEL_UPDATES = 1

IDENTIFIABLE: Final = frozenset(
    {DeviceType.VA04, DeviceType.SU04, DeviceType.TR04, DeviceType.RU04}
)
OFF: Final = Setting(power=Power.OFF)
MANUAL: Final = TerminationType.MANUAL


@dataclass(frozen=True, kw_only=True)
class TadoXQuickActionDescription(ButtonEntityDescription):
    """Description of a home-wide quick action."""

    action: QuickAction


QUICK_ACTIONS: Final = (
    TadoXQuickActionDescription(
        key="boost_all", translation_key="boost_all", action=QuickAction.BOOST
    ),
    TadoXQuickActionDescription(
        key="disable_all", translation_key="disable_all", action=QuickAction.ALL_OFF
    ),
    TadoXQuickActionDescription(
        key="resume_schedules",
        translation_key="resume_schedules",
        action=QuickAction.RESUME_SCHEDULE,
    ),
)
IDENTIFY_DESCRIPTION = ButtonEntityDescription(
    key="identify",
    translation_key="identify",
    device_class=ButtonDeviceClass.IDENTIFY,
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=False,
)


def _off_patch(room_id: int) -> RoomPatch:
    """Return a patch that shows a room off until ended, as allOff leaves it."""

    def patch(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        if (room := snapshot.rooms.get(room_id)) is None:
            return snapshot
        manual = room.manual_control
        if room.setting == OFF and manual is not None and manual.type is MANUAL:
            return snapshot
        return snapshot.with_room(
            room_id, setting=OFF, manual_control=ActiveOverride(type=MANUAL)
        )

    return patch


def _boost_patch(room_id: int) -> RoomPatch:
    """Return a patch that shows a boost until the server reports one.

    tado alone knows how a boost sets the room, so only the boost is shown.
    """

    def patch(snapshot: RoomsSnapshot) -> RoomsSnapshot:
        if (room := snapshot.rooms.get(room_id)) is None or room.boost is not None:
            return snapshot
        return snapshot.with_room(room_id, boost=ActiveOverride())

    return patch


# Every quick action changes the control of each room, so each one needs a
# patch that replaces the pending write of every room.
ROOM_PATCHES: Final[Mapping[QuickAction, Callable[[int], RoomPatch]]] = {
    QuickAction.BOOST: _boost_patch,
    QuickAction.ALL_OFF: _off_patch,
    QuickAction.RESUME_SCHEDULE: resume_patch,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button entities of a tado X home."""
    data = entry.runtime_data
    async_add_entities(
        TadoXQuickActionButton(data.rooms, description) for description in QUICK_ACTIONS
    )
    async_add_dynamic_entities(
        entry,
        data.devices,
        async_add_entities,
        lambda: [
            TadoXIdentifyButton(data.devices, serial)
            for serial, device in data.devices.data.topology.devices.items()
            if device.type in IDENTIFIABLE
        ],
    )


class TadoXQuickActionButton(TadoXEntity[RoomsCoordinator], ButtonEntity):
    """Quick action for every room of the home."""

    entity_description: TadoXQuickActionDescription

    def __init__(
        self, coordinator: RoomsCoordinator, description: TadoXQuickActionDescription
    ) -> None:
        """Initialize the button of the home."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            description,
            unique_id=f"{home_id}_{description.key}",
            device=home_identifier(home_id),
        )

    @override
    async def async_press(self) -> None:
        """Run the quick action and read the rooms back."""
        await self._async_run(self.entity_description.action)

    @handle_tado_errors
    async def _async_run(self, action: QuickAction) -> None:
        await self.coordinator.api.async_quick_action(action)
        room_patch = ROOM_PATCHES[action]
        self.coordinator.async_apply_optimistic(
            {
                control_target(room_id): room_patch(room_id)
                for room_id in self.coordinator.data.rooms
            }
        )
        await self.coordinator.async_request_refresh()


class TadoXIdentifyButton(TadoXEntity[DevicesCoordinator], ButtonEntity):
    """Let a device flash its display; experimental, disabled by default."""

    def __init__(self, coordinator: DevicesCoordinator, serial: str) -> None:
        """Initialize the button of a device."""
        super().__init__(
            coordinator,
            IDENTIFY_DESCRIPTION,
            unique_id=f"{serial}_identify",
            device=device_identifier(serial),
        )
        self._serial = serial

    @override
    def _present(self) -> bool:
        return self._serial in self.coordinator.data.topology.devices

    @override
    async def async_press(self) -> None:
        """Identify the device."""
        await self._async_identify()

    @handle_tado_errors
    async def _async_identify(self) -> None:
        await self.coordinator.api.async_identify_device(self._serial)
