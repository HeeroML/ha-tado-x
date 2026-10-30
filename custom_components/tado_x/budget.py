"""Planning of the daily API call budget.

Pure module: no Home Assistant imports.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import math
from typing import Final

from .client.models import RateLimitInfo

ROOMS: Final = "rooms"
ROOMS_MIN_PERIOD: Final = timedelta(seconds=30)
ROOMS_MAX_PERIOD: Final = timedelta(hours=1)
MAX_FACTOR: Final = 30.0

_DAY: Final = 86400
_ROOMS_STEP: Final = 5
# The coordinator schedules on whole loop seconds, so a tick can come up to a
# second early; without slack an endpoint would wait for the next tick.
_DUE_TOLERANCE: Final = timedelta(seconds=5)


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


def _rate(key: str, base: int, maximum: int, *, optional: bool) -> EndpointRate:
    return EndpointRate(
        key, timedelta(seconds=base), timedelta(seconds=maximum), optional
    )


# Base and longest period of every polled endpoint (design §4.3). Once a 404
# marked flow temperature absent, FLOW_REPROBE reads it once a day.
ENDPOINT_RATES: Final[Mapping[str, EndpointRate]] = {
    rate.key: rate
    for rate in (
        _rate(ROOMS, 45, 3600, optional=False),
        _rate("home_state", 300, 21600, optional=False),
        _rate("weather", 900, 21600, optional=True),
        _rate("air_comfort", 900, 21600, optional=True),
        _rate("mobile_devices", 300, 21600, optional=True),
        _rate("topology", 900, 86400, optional=False),
        _rate("home_info", 86400, 86400, optional=True),
        _rate("running_times", 3600, 86400, optional=True),
        _rate("flow_temperature", 900, 86400, optional=True),
        _rate("holiday_mode", 900, 86400, optional=True),
    )
}
FLOW_REPROBE: Final = _rate("flow_temperature", 86400, 86400, optional=True)
OPTIONAL_ENDPOINTS: Final = frozenset(
    key for key, rate in ENDPOINT_RATES.items() if rate.optional
)


@dataclass(frozen=True, slots=True)
class PollPlan:
    """Polling periods that fit the daily call budget."""

    rooms: timedelta
    periods: Mapping[str, timedelta]
    calls_per_day: int
    budget: int
    maximums: Mapping[str, timedelta] = field(default_factory=dict)

    def period(self, key: str, factor: float = 1.0) -> timedelta:
        """Return the period of an endpoint, stretched by factor within its bounds."""
        base = self.periods[key]
        stretched = _ceil_seconds(base * factor)
        return min(max(stretched, base), self.maximums.get(key, stretched))


def _ceil_seconds(value: timedelta) -> timedelta:
    return timedelta(seconds=math.ceil(value.total_seconds()))


def _calls_per_day(period: timedelta) -> int:
    return math.ceil(_DAY / period.total_seconds())


def _rooms_period(spare_calls: int, maximum: timedelta) -> timedelta:
    """Spread the calls left for the rooms over a day, in steps of 5 s."""
    if spare_calls <= 0:
        return maximum
    seconds = math.ceil(_DAY / spare_calls / _ROOMS_STEP) * _ROOMS_STEP
    return min(max(timedelta(seconds=seconds), ROOMS_MIN_PERIOD), maximum)


def plan_polling(
    *, budget: int, quota_limit: int | None, endpoints: Sequence[EndpointRate]
) -> PollPlan:
    """Plan the endpoint periods for a daily budget.

    The cap is min(budget, 80 % of the quota), 10 % of it is kept for writes,
    the slow endpoints get at most half of the rest, and the rooms endpoint
    takes what remains (30 s to 3,600 s).
    """
    cap = budget if quota_limit is None else min(budget, math.floor(0.8 * quota_limit))
    poll = math.floor(0.9 * cap)
    rates = {rate.key: rate for rate in endpoints}
    slow_periods = {key: rate.base for key, rate in rates.items() if key != ROOMS}
    slow = sum(_calls_per_day(period) for period in slow_periods.values())
    if slow > poll / 2:
        stretch = slow / max(poll / 2, 1)
        slow_periods = {
            key: min(_ceil_seconds(period * stretch), rates[key].maximum)
            for key, period in slow_periods.items()
        }
        slow = sum(_calls_per_day(period) for period in slow_periods.values())
    rooms_max = rates[ROOMS].maximum if ROOMS in rates else ROOMS_MAX_PERIOD
    rooms = _rooms_period(poll - slow, rooms_max)
    periods = {ROOMS: rooms, **slow_periods}
    return PollPlan(
        rooms=rooms,
        periods=periods,
        calls_per_day=sum(_calls_per_day(period) for period in periods.values()),
        budget=budget,
        maximums={key: rate.maximum for key, rate in rates.items()}
        | {ROOMS: rooms_max},
    )


def throttle_factor(
    *,
    plan: PollPlan,
    info: RateLimitInfo | None,
    reserve: int,
    entries_on_account: int,
    blocked: bool,
    now: datetime,
) -> tuple[float, PlanMode]:
    """Return the factor that stretches every period, and the plan mode.

    The plan's calls until the quota resets are compared with this entry's
    share of the quota left above the reserve. Without a known reset the
    whole day is assumed.
    """
    if blocked:
        return 1.0, PlanMode.BLOCKED
    if info is None or info.remaining is None:
        return 1.0, PlanMode.OK
    if info.limit is not None:
        reserve = min(reserve, math.floor(0.1 * info.limit))
    if info.remaining <= reserve:
        return MAX_FACTOR, PlanMode.CONSERVING
    reset_at = info.reset_at
    seconds = _DAY if reset_at is None else max((reset_at - now).total_seconds(), 0)
    need = plan.calls_per_day * seconds / _DAY
    allowed = (info.remaining - reserve) / max(entries_on_account, 1)
    factor = min(max(need / allowed, 1.0), MAX_FACTOR)
    return factor, PlanMode.OK if factor == 1.0 else PlanMode.THROTTLED


def effective_period(
    *, plan: PollPlan, key: str, optional: bool, factor: float, mode: PlanMode
) -> timedelta | None:
    """Return the period of an endpoint in a mode; None while it is paused.

    While conserving, optional endpoints pause and the others poll at their
    maximum period.
    """
    if mode is PlanMode.CONSERVING:
        return None if optional else plan.maximums.get(key, plan.periods[key])
    return plan.period(key, factor)


def coordinator_interval(
    *,
    plan: PollPlan,
    keys: Iterable[str],
    optional: frozenset[str],
    factor: float,
    mode: PlanMode,
) -> timedelta:
    """Return the shortest active period of the endpoints of one coordinator."""
    periods = [
        period
        for key in keys
        if (
            period := effective_period(
                plan=plan, key=key, optional=key in optional, factor=factor, mode=mode
            )
        )
        is not None
    ]
    return min(periods, default=plan.maximums.get(ROOMS, plan.rooms))


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
    due: set[str] = set()
    for key in keys:
        period = effective_period(
            plan=plan, key=key, optional=key in optional, factor=factor, mode=mode
        )
        if period is None:
            continue
        previous = last.get(key)
        if previous is None or now - previous >= period - _DUE_TOLERANCE:
            due.add(key)
    return frozenset(due)
