"""Base entities and error mapping of the tado X integration.

Errors are recorded in the telemetry of the entry once, by the innermost
layer that knows why the request was made. The client records failed writes
("command <endpoint>"), token refreshes and the closing of the quota gate;
the coordinators record polls ("poll <kind>"), setup records "setup", and
raise_for_tado_error() records entity actions ("command <key>") and services
("service <name>"). Telemetry ignores an error object it holds already, so
an outer layer that records the same error again changes nothing.
"""

from collections.abc import Callable, Coroutine, Iterable
from functools import wraps
from typing import Any, Concatenate, NoReturn, override

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import (
    TadoXAuthError,
    TadoXError,
    TadoXHomeNotXError,
    TadoXNotFoundError,
    TadoXParseError,
    TadoXRateLimitError,
    TadoXRequestError,
    TadoXTransientError,
    redact_text,
)
from .const import DOMAIN, LOGGER
from .coordinator import TadoXCoordinator
from .issues import format_time
from .runtime import TadoXConfigEntry


class TadoXEntity[C: TadoXCoordinator[Any]](CoordinatorEntity[C]):
    """Coordinator entity that is available only while its object is reported."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: C,
        description: EntityDescription,
        *,
        unique_id: str,
        device: tuple[str, str],
    ) -> None:
        """Initialize with DeviceInfo(identifiers={device}) only."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(identifiers={device})
        self._device_identifier = device

    @property
    def device_identifier(self) -> tuple[str, str]:
        """Return the identifier of the device this entity belongs to."""
        return self._device_identifier

    def _present(self) -> bool:
        """Return True while the object of this entity is in the data."""
        return True

    @property
    @override
    def available(self) -> bool:
        """Return True if the coordinator succeeded and the object is present."""
        return super().available and self._present()


class TadoXStatusEntity(Entity):
    """API and status entity driven by the quota monitor, not by a coordinator.

    It stays available whenever the entry is loaded, and writes its state
    whenever the monitor of its config entry reports a change or an error is
    recorded in its telemetry.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    @override
    async def async_added_to_hass(self) -> None:
        """Listen to the quota monitor and the telemetry of the entry."""
        await super().async_added_to_hass()
        entry: TadoXConfigEntry | None = self.platform.config_entry
        if entry is not None:
            data = entry.runtime_data
            self.async_on_remove(
                data.monitor.async_add_listener(self.async_write_ha_state)
            )
            self.async_on_remove(data.telemetry.add_listener(self.async_write_ha_state))


def raise_for_tado_error(
    err: TadoXError, entry: TadoXConfigEntry, *, context: str
) -> NoReturn:
    """Record a client error and raise the translated Home Assistant error.

    context names the action, such as "command child_lock" or "service
    set_schedule". An auth error also starts a reauth flow.
    """
    entry.runtime_data.telemetry.record_tado_error(err, context=context)
    if isinstance(err, TadoXAuthError):
        entry.async_start_reauth(entry.runtime_data.rooms.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    if isinstance(err, TadoXRateLimitError):
        if err.reset_at is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="quota_exhausted_unknown_reset",
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="quota_exhausted",
            translation_placeholders={"reset": format_time(err.reset_at)},
        ) from err
    if isinstance(err, TadoXTransientError):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err
    if isinstance(err, TadoXHomeNotXError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="not_tado_x_home",
            translation_placeholders={"home": entry.title},
        ) from err
    if isinstance(err, TadoXNotFoundError):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="not_found"
        ) from err
    if isinstance(err, TadoXRequestError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="request_rejected",
            translation_placeholders={
                "status": str(err.status),
                "title": redact_text(err.title) or "",
            },
        ) from err
    error = (
        f"{err.endpoint} {err.path}" if isinstance(err, TadoXParseError) else str(err)
    )
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="api_error",
        translation_placeholders={"error": error},
    ) from err


def handle_tado_errors[E: TadoXEntity[Any], **P, R](
    func: Callable[Concatenate[E, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[E, P], Coroutine[Any, Any, R]]:
    """Translate client errors raised by an entity action.

    The error is recorded as "command" plus the key of the entity description.
    """

    @wraps(func)
    async def wrapper(entity: E, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(entity, *args, **kwargs)
        except TadoXError as err:
            raise_for_tado_error(
                err,
                entity.coordinator.config_entry,
                context=f"command {entity.entity_description.key}",
            )

    return wrapper


@callback
def async_add_dynamic_entities(
    entry: TadoXConfigEntry,
    coordinator: TadoXCoordinator[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[], Iterable[TadoXEntity[Any]]],
) -> None:
    """Add the built entities now and after every update, skipping known unique ids.

    Removing a device removes its entities, so a unique id is forgotten once
    its device is gone; the entities of a room or device that tado reports
    again are then added again.
    """
    registry = dr.async_get(coordinator.hass)
    known: dict[str | None, tuple[str, str]] = {}

    @callback
    def _async_add_new() -> None:
        for unique_id, device in list(known.items()):
            if registry.async_get_device_by_identifier(device, entry.entry_id) is None:
                del known[unique_id]
        new = [entity for entity in build() if entity.unique_id not in known]
        if new:
            known.update((entity.unique_id, entity.device_identifier) for entity in new)
            LOGGER.debug("Adding %s new entities of %s", len(new), entry.title)
            async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
