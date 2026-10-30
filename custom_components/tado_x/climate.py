"""Climate platform: one entity per tado X room."""

import datetime as dt
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoomsCoordinator
from .entity import TadoXEntity
from .runtime import TadoXConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TadoXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room climate entities of a tado X home."""
    raise NotImplementedError


class TadoXClimate(TadoXEntity[RoomsCoordinator], ClimateEntity):
    """Climate entity of one room; presets set the home-wide presence."""

    async def async_set_climate_timer(
        self,
        temperature: float,
        duration: int | None = None,
        termination: str | None = None,
    ) -> None:
        """Set a temperature; duration in minutes means a timer (v1 compatible)."""
        raise NotImplementedError

    async def async_get_schedule(self) -> ServiceResponse:
        """Return the weekly schedule of the room."""
        raise NotImplementedError

    async def async_set_schedule(
        self, days: list[str], blocks: list[dict[str, Any]]
    ) -> None:
        """Replace the schedule of the given weekdays with the same blocks."""
        raise NotImplementedError

    async def async_get_day_report(
        self, date: dt.date | None = None
    ) -> ServiceResponse:
        """Return the summary and the raw day report; today in the home's zone by default."""
        raise NotImplementedError
