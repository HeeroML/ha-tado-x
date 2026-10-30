"""Coordinators of the tado X integration, one per polling cadence."""

from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import ClassVar, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .cache import CacheSeed
from .client import TadoXHome
from .const import MAX_STALE_DEVICES, MAX_STALE_HOME, MAX_STALE_ROOMS
from .quota import QuotaMonitor
from .runtime import DevicesSnapshot, HomeSnapshot, RoomsSnapshot, TadoXConfigEntry


class TadoXCoordinator[S](DataUpdateCoordinator[S]):
    """Coordinator with per-endpoint periods, stale serving and optimistic patches.

    During a quota hold the last snapshot is served for up to MAX_STALE;
    network, server and parse errors make the entities unavailable as usual.
    """

    config_entry: TadoXConfigEntry

    ENDPOINTS: ClassVar[tuple[str, ...]]
    REQUIRED: ClassVar[frozenset[str]]
    MAX_STALE: ClassVar[timedelta]

    last_fetched: dict[str, datetime]
    last_live_update: datetime | None
    stale_since: datetime | None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TadoXConfigEntry,
        api: TadoXHome,
        monitor: QuotaMonitor,
        seed: CacheSeed | None,
    ) -> None:
        """Initialize with always_update=False and a 5 s refresh debouncer."""
        raise NotImplementedError

    @abstractmethod
    async def _async_fetch(self, previous: S | None, due: frozenset[str]) -> S:
        """Fetch the due endpoints and merge them into the previous snapshot."""

    @override
    async def _async_update_data(self) -> S:
        """Fetch the due endpoints and map client errors to coordinator errors."""
        raise NotImplementedError

    @callback
    def async_apply_optimistic(
        self, patch: Callable[[S], S], *, ttl: timedelta = timedelta(seconds=60)
    ) -> None:
        """Apply a patch now and re-apply it to fetched data until it agrees or ttl passes."""
        raise NotImplementedError


class RoomsCoordinator(TadoXCoordinator[RoomsSnapshot]):
    """Live room state, polled at the planned rooms period."""

    ENDPOINTS = ("rooms",)
    REQUIRED = frozenset({"rooms"})
    MAX_STALE = MAX_STALE_ROOMS

    @override
    async def _async_fetch(
        self, previous: RoomsSnapshot | None, due: frozenset[str]
    ) -> RoomsSnapshot:
        """Fetch the rooms."""
        raise NotImplementedError


class HomeCoordinator(TadoXCoordinator[HomeSnapshot]):
    """Presence, weather, air comfort and phones."""

    ENDPOINTS = ("home_state", "weather", "air_comfort", "mobile_devices")
    REQUIRED = frozenset({"home_state"})
    MAX_STALE = MAX_STALE_HOME

    @override
    async def _async_fetch(
        self, previous: HomeSnapshot | None, due: frozenset[str]
    ) -> HomeSnapshot:
        """Fetch the due home endpoints."""
        raise NotImplementedError


class DevicesCoordinator(TadoXCoordinator[DevicesSnapshot]):
    """Topology, home details, running times, flow temperature and holiday mode."""

    ENDPOINTS = (
        "topology",
        "home_info",
        "running_times",
        "flow_temperature",
        "holiday_mode",
    )
    REQUIRED = frozenset({"topology"})
    MAX_STALE = MAX_STALE_DEVICES

    @override
    async def _async_fetch(
        self, previous: DevicesSnapshot | None, due: frozenset[str]
    ) -> DevicesSnapshot:
        """Fetch the due device endpoints."""
        raise NotImplementedError
