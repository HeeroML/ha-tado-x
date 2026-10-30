"""Base entities and error mapping of the tado X integration."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, NoReturn, override

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import TadoXError
from .coordinator import TadoXCoordinator
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
        raise NotImplementedError

    def _present(self) -> bool:
        """Return True while the object of this entity is in the data."""
        raise NotImplementedError

    @property
    @override
    def available(self) -> bool:
        """Return True if the coordinator succeeded and the object is present."""
        raise NotImplementedError


class TadoXStatusEntity(Entity):
    """API and status entity driven by the quota monitor, not by a coordinator.

    It stays available whenever the entry is loaded.
    """

    _attr_should_poll = False


def handle_tado_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Translate client errors raised by an entity action."""
    raise NotImplementedError


def raise_for_tado_error(err: TadoXError, entry: TadoXConfigEntry) -> NoReturn:
    """Raise the translated Home Assistant error for a client error.

    An auth error also starts a reauth flow.
    """
    raise NotImplementedError


@callback
def async_add_dynamic_entities(
    entry: TadoXConfigEntry,
    coordinator: TadoXCoordinator[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[], Iterable[TadoXEntity[Any]]],
) -> None:
    """Add the built entities now and after every update, skipping known unique ids."""
    raise NotImplementedError
