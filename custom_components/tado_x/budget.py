"""Planning of the daily API call budget.

Pure module: no Home Assistant imports.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .client.models import RateLimitInfo


@dataclass(frozen=True, slots=True)
class EndpointRate:
    """Polling period of one endpoint at throttle factor 1, and its upper bound."""

    key: str
    base: timedelta
    maximum: timedelta
    optional: bool


class PlanMode(StrEnum):
    """How the polling plan reacts to the account-wide quota."""

    OK = "ok"
    THROTTLED = "throttled"
    CONSERVING = "conserving"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PollPlan:
    """Polling periods that fit the daily call budget."""

    rooms: timedelta
    periods: Mapping[str, timedelta]
    calls_per_day: int
    budget: int

    def period(self, key: str, factor: float = 1.0) -> timedelta:
        """Return the period of an endpoint, stretched by factor within its bounds."""
        raise NotImplementedError


def plan_polling(
    *, budget: int, quota_limit: int | None, endpoints: Sequence[EndpointRate]
) -> PollPlan:
    """Plan the endpoint periods for a daily budget.

    The cap is min(budget, 80 % of the quota), 10 % of it is kept for writes,
    the slow endpoints get at most half of the rest, and the rooms endpoint
    takes what remains (30 s to 3,600 s).
    """
    raise NotImplementedError


def throttle_factor(
    *,
    plan: PollPlan,
    info: RateLimitInfo | None,
    reserve: int,
    entries_on_account: int,
    blocked: bool,
    now: datetime,
) -> tuple[float, PlanMode]:
    """Return the factor that stretches every period, and the plan mode."""
    raise NotImplementedError


def endpoints_due(
    *,
    plan: PollPlan,
    keys: Iterable[str],
    optional: frozenset[str],
    last: Mapping[str, datetime],
    now: datetime,
    factor: float,
    mode: PlanMode,
) -> frozenset[str]:
    """Return the endpoints whose effective period has passed."""
    raise NotImplementedError
