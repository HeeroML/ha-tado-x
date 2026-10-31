"""Select platform: presence and room termination defaults."""

from datetime import timedelta
from http import HTTPStatus
from typing import override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import (
    PresenceSetting,
    TadoXError,
    TadoXRequestError,
    Termination,
    TerminationType,
)
from .const import DEFAULT_OPTIONS, DOMAIN, OPT_TIMER_MINUTES
from .coordinator import DevicesCoordinator, HomeCoordinator
from .devices import home_identifier, room_identifier
from .entity import TadoXEntity, async_add_dynamic_entities, raise_for_tado_error
from .runtime import TadoXConfigEntry

PARALLEL_UPDATES = 1

PRESENCE_DESCRIPTION = SelectEntityDescription(
    key="presence_mode",
    translation_key="presence_mode",
    options=[setting.lower() for setting in PresenceSetting],
)
TERMINATION_DESCRIPTION = SelectEntityDescription(
    key="manual_termination",
    translation_key="manual_termination",
    entity_category=EntityCategory.CONFIG,
    options=[kind.lower() for kind in TerminationType],
)


async def async_set_presence(entry: TadoXConfigEntry, setting: PresenceSetting) -> None:
    """Lock the presence of the home, or hand it back to geofencing with AUTO."""
    data = entry.runtime_data
    try:
        await data.api.async_set_presence(setting)
    except TadoXRequestError as err:
        if (
            setting is PresenceSetting.AUTO
            and err.status == HTTPStatus.UNPROCESSABLE_ENTITY
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="presence_auto_requires_auto_assist",
            ) from err
        raise_for_tado_error(err, entry, context="command presence")
    except TadoXError as err:
        raise_for_tado_error(err, entry, context="command presence")
    data.home.async_apply_optimistic(
        {"presence": lambda snapshot: snapshot.with_presence(setting)}
    )


async def async_set_room_default(
    entry: TadoXConfigEntry, room_id: int, termination: Termination
) -> None:
    """Write the app's manual control default of a room; tado keeps the only copy."""
    data = entry.runtime_data
    try:
        await data.api.async_set_room_termination_default(room_id, termination)
    except TadoXError as err:
        raise_for_tado_error(err, entry, context="command manual_termination")
    data.devices.async_apply_optimistic(
        {
            ("room_default", room_id): lambda snapshot: snapshot.with_room_default(
                room_id, termination
            )
        }
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the select entities of a tado X home."""
    data = entry.runtime_data
    async_add_entities([TadoXPresenceSelect(data.home)])
    async_add_dynamic_entities(
        entry,
        data.devices,
        async_add_entities,
        lambda: [
            TadoXTerminationSelect(data.devices, room_id)
            for room_id in data.devices.data.topology.rooms
        ],
    )


class TadoXPresenceSelect(TadoXEntity[HomeCoordinator], SelectEntity):
    """Presence of the home: locked at home or away, or decided by geofencing."""

    def __init__(self, coordinator: HomeCoordinator) -> None:
        """Initialize the select of the home."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            PRESENCE_DESCRIPTION,
            unique_id=f"{home_id}_presence_mode",
            device=home_identifier(home_id),
        )

    @property
    @override
    def current_option(self) -> str:
        """Return the presence setting."""
        return self.coordinator.data.state.setting.lower()

    @override
    async def async_select_option(self, option: str) -> None:
        """Set the presence of the home."""
        await async_set_presence(
            self.coordinator.config_entry, PresenceSetting(option.upper())
        )


class TadoXTerminationSelect(TadoXEntity[DevicesCoordinator], SelectEntity):
    """When a manual change of a room ends, as set in the tado app (#94)."""

    def __init__(self, coordinator: DevicesCoordinator, room_id: int) -> None:
        """Initialize the select of a room."""
        home_id = coordinator.api.home_id
        super().__init__(
            coordinator,
            TERMINATION_DESCRIPTION,
            unique_id=f"{home_id}_{room_id}_manual_termination",
            device=room_identifier(home_id, room_id),
        )
        self._room_id = room_id

    @property
    def _default(self) -> Termination | None:
        room = self.coordinator.data.topology.rooms.get(self._room_id)
        return None if room is None else room.manual_termination_default

    @override
    def _present(self) -> bool:
        return self._room_id in self.coordinator.data.topology.rooms

    @property
    @override
    def current_option(self) -> str | None:
        """Return the termination type of the app setting."""
        default = self._default
        return None if default is None else default.type.lower()

    @override
    async def async_select_option(self, option: str) -> None:
        """Write the app setting; a new timer takes the timer option of the entry."""
        kind = TerminationType(option.upper())
        current = self._default
        if kind is not TerminationType.TIMER:
            termination = Termination(type=kind)
        elif current is not None and current.type is TerminationType.TIMER:
            termination = current
        else:
            options = {**DEFAULT_OPTIONS, **self.coordinator.config_entry.options}
            termination = Termination.timer(
                timedelta(minutes=int(options[OPT_TIMER_MINUTES]))
            )
        await async_set_room_default(
            self.coordinator.config_entry, self._room_id, termination
        )
