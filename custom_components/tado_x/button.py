"""Button platform: quick actions and identify."""

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


def _all_off(snapshot: RoomsSnapshot) -> RoomsSnapshot:
    """Show every room off until ended, as the allOff quick action leaves it."""
    for room_id, room in snapshot.rooms.items():
        manual = room.manual_control
        if room.setting != OFF or manual is None or manual.type is not MANUAL:
            snapshot = snapshot.with_room(
                room_id, setting=OFF, manual_control=ActiveOverride(type=MANUAL)
            )
    return snapshot


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
        if action is QuickAction.ALL_OFF:
            self.coordinator.async_apply_optimistic(_all_off)
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
