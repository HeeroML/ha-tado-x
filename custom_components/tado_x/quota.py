"""Quota monitoring: polling plan, throttle, API status and quota issues."""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .budget import PlanMode, PollPlan
from .cache import SnapshotCache
from .client import QuotaTracker, RateLimitInfo
from .runtime import TadoXConfigEntry

if TYPE_CHECKING:
    from .coordinator import TadoXCoordinator


class ApiStatus(StrEnum):
    """State of the api_status sensor."""

    OK = "ok"
    THROTTLED = "throttled"
    CONSERVING = "conserving"
    RATE_LIMITED = "rate_limited"


class QuotaMonitor:
    """Plan and throttle the coordinators of one entry from the shared quota."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TadoXConfigEntry,
        quota: QuotaTracker,
        cache: SnapshotCache,
    ) -> None:
        """Initialize from the entry options and the quota tracker."""
        raise NotImplementedError

    @property
    def status(self) -> ApiStatus:
        """Return the API status."""
        raise NotImplementedError

    @property
    def plan(self) -> PollPlan:
        """Return the current polling plan."""
        raise NotImplementedError

    @property
    def info(self) -> RateLimitInfo | None:
        """Return the latest rate limit information."""
        raise NotImplementedError

    @property
    def factor(self) -> float:
        """Return the current throttle factor."""
        raise NotImplementedError

    @property
    def mode(self) -> PlanMode:
        """Return the current plan mode."""
        raise NotImplementedError

    def due(self, coordinator: TadoXCoordinator[Any], now: datetime) -> frozenset[str]:
        """Return the endpoints of a coordinator that are due now."""
        raise NotImplementedError

    @callback
    def async_start(self, coordinators: Iterable[TadoXCoordinator[Any]]) -> None:
        """Plan, apply the intervals and start listening to the quota tracker."""
        raise NotImplementedError

    @callback
    def async_on_poll(self, coordinator: TadoXCoordinator[Any]) -> None:
        """Re-plan, update the status listeners and the quota issues after a poll."""
        raise NotImplementedError

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for status changes; return a function that removes the listener."""
        raise NotImplementedError
