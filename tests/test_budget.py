"""Tests for the daily call budget planner."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.tado_x.budget import (
    ENDPOINT_RATES,
    MAX_FACTOR,
    ROOMS,
    EndpointRate,
    PlanMode,
    PollPlan,
    coordinator_interval,
    effective_period,
    endpoints_due,
    plan_polling,
    throttle_factor,
)
from custom_components.tado_x.client import RateLimitInfo

NOW = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
OPTIONAL = frozenset({"weather", "air_comfort", "running_times", "home_info"})


def _rate(key: str, base: int, maximum: int, *, optional: bool) -> EndpointRate:
    return EndpointRate(
        key, timedelta(seconds=base), timedelta(seconds=maximum), optional
    )


HOME_B = (
    _rate(ROOMS, 45, 3600, optional=False),
    _rate("home_state", 300, 21600, optional=False),
    _rate("weather", 900, 21600, optional=True),
    _rate("air_comfort", 900, 21600, optional=True),
    _rate("topology", 900, 86400, optional=False),
    _rate("running_times", 3600, 86400, optional=True),
    _rate("home_info", 86400, 86400, optional=True),
)
PHONES_AND_HOLIDAY = (
    _rate("mobile_devices", 300, 21600, optional=True),
    _rate("holiday_mode", 900, 86400, optional=True),
)


def _seconds(plan: PollPlan) -> dict[str, float]:
    return {key: period.total_seconds() for key, period in plan.periods.items()}


def _info(
    remaining: int | None, *, limit: int | None = 20000, reset_in: int | None = None
) -> RateLimitInfo:
    return RateLimitInfo(
        limit=limit,
        window=timedelta(days=1),
        remaining=remaining,
        reset_in=None if reset_in is None else timedelta(seconds=reset_in),
        observed_at=NOW,
    )


@pytest.mark.parametrize(
    ("budget", "limit", "extra", "rooms", "calls"),
    [
        (3000, None, (), 45, 2521),
        (3000, 20000, (), 45, 2521),
        (3000, None, PHONES_AND_HOLIDAY, 55, 2556),
        (6000, None, (), 30, 3481),
        (2000, None, (), 75, 1753),
        (3000, 100, (), 2620, 72),
    ],
    ids=["home_b", "auto_assist", "phones_holiday", "6000", "2000", "free_tier"],
)
def test_plan_examples(
    budget: int,
    limit: int | None,
    extra: tuple[EndpointRate, ...],
    rooms: int,
    calls: int,
) -> None:
    """The planner reproduces the worked examples of the design."""
    plan = plan_polling(budget=budget, quota_limit=limit, endpoints=(*HOME_B, *extra))

    assert plan.rooms == timedelta(seconds=rooms)
    assert plan.periods[ROOMS] == plan.rooms
    assert plan.calls_per_day == calls
    assert plan.budget == budget


def test_plan_home_b_periods() -> None:
    """With the default budget the slow endpoints keep their base periods."""
    plan = plan_polling(budget=3000, quota_limit=None, endpoints=HOME_B)

    assert _seconds(plan) == {
        ROOMS: 45,
        "home_state": 300,
        "weather": 900,
        "air_comfort": 900,
        "topology": 900,
        "running_times": 3600,
        "home_info": 86400,
    }


def test_plan_free_tier_stretches_slow_endpoints_within_their_maximum() -> None:
    """A free-tier quota stretches every slow endpoint by k = 601 / 36."""
    plan = plan_polling(budget=3000, quota_limit=100, endpoints=HOME_B)

    assert _seconds(plan) == {
        ROOMS: 2620,
        "home_state": 5009,
        "weather": 15025,
        "air_comfort": 15025,
        "topology": 15025,
        "running_times": 60100,
        "home_info": 86400,
    }


def test_plan_without_spare_calls_polls_rooms_at_the_maximum() -> None:
    """When the slow endpoints use everything, rooms poll once an hour."""
    plan = plan_polling(budget=100, quota_limit=2, endpoints=HOME_B)

    assert plan.rooms == timedelta(hours=1)


def test_plan_without_a_rooms_rate_uses_the_default_bounds() -> None:
    """The rooms tier is always planned, also when the caller leaves it out."""
    plan = plan_polling(budget=3000, quota_limit=None, endpoints=HOME_B[1:])

    assert plan.rooms == timedelta(seconds=45)
    assert plan.maximums[ROOMS] == timedelta(hours=1)


def test_period_is_clamped_between_base_and_maximum() -> None:
    """A factor stretches a period, never below the base or above the maximum."""
    plan = plan_polling(budget=3000, quota_limit=None, endpoints=HOME_B)

    assert plan.period("home_state") == timedelta(seconds=300)
    assert plan.period("home_state", 0.5) == timedelta(seconds=300)
    assert plan.period("home_state", 3.15125) == timedelta(seconds=946)
    assert plan.period(ROOMS, 3.15125) == timedelta(seconds=142)
    assert plan.period("home_state", MAX_FACTOR) == timedelta(seconds=9000)
    assert plan.period("weather", MAX_FACTOR) == timedelta(seconds=21600)
    assert plan.period("home_info", 2) == timedelta(days=1)


def test_period_without_a_maximum_is_only_stretched() -> None:
    """A plan built by hand without maximums still applies the factor."""
    plan = PollPlan(
        rooms=timedelta(seconds=60),
        periods={ROOMS: timedelta(seconds=60)},
        calls_per_day=1440,
        budget=1600,
    )

    assert plan.period(ROOMS, 2) == timedelta(seconds=120)


@pytest.fixture
def plan() -> PollPlan:
    """Return the plan of home B with the default budget."""
    return plan_polling(budget=3000, quota_limit=20000, endpoints=HOME_B)


@pytest.mark.parametrize(
    ("info", "factor", "mode"),
    [
        (None, 1.0, PlanMode.OK),
        (_info(None), 1.0, PlanMode.OK),
        (_info(15000), 1.0, PlanMode.OK),
        (_info(1800, reset_in=21600), 1.0, PlanMode.OK),
        (_info(1800), 2521 / 800, PlanMode.THROTTLED),
        (_info(1001), MAX_FACTOR, PlanMode.THROTTLED),
        (_info(1000), MAX_FACTOR, PlanMode.CONSERVING),
        (_info(1800, reset_in=0), 1.0, PlanMode.OK),
        (_info(900, reset_in=0), 1.0, PlanMode.OK),
    ],
    ids=[
        "no_headers",
        "no_remaining",
        "plenty",
        "example_with_reset",
        "example_without_reset",
        "capped",
        "reserve",
        "reset_passed",
        "reserve_until_a_passed_reset",
    ],
)
def test_throttle_factor(
    plan: PollPlan, info: RateLimitInfo | None, factor: float, mode: PlanMode
) -> None:
    """The factor follows the need until the reset against the allowance (§4.4)."""
    result, result_mode = throttle_factor(
        plan=plan,
        info=info,
        reserve=1000,
        entries_on_account=1,
        blocked=False,
        now=NOW,
    )

    assert result == pytest.approx(factor)
    assert result_mode is mode


def test_throttle_factor_when_the_gate_is_closed(plan: PollPlan) -> None:
    """A closed gate blocks polling whatever the headers say."""
    result = throttle_factor(
        plan=plan,
        info=_info(15000),
        reserve=1000,
        entries_on_account=1,
        blocked=True,
        now=NOW,
    )

    assert result == (1.0, PlanMode.BLOCKED)


def test_two_entries_split_the_allowance(plan: PollPlan) -> None:
    """Two entries on one account each get half of the quota above the reserve."""
    one, _ = throttle_factor(
        plan=plan,
        info=_info(5000, reset_in=43200),
        reserve=1000,
        entries_on_account=1,
        blocked=False,
        now=NOW,
    )
    two, mode = throttle_factor(
        plan=plan,
        info=_info(2000, reset_in=43200),
        reserve=1000,
        entries_on_account=2,
        blocked=False,
        now=NOW,
    )

    assert one == 1.0
    assert two == pytest.approx(1260.5 / 500)
    assert mode is PlanMode.THROTTLED


def test_free_tier_reserve_is_capped_at_ten_percent(plan: PollPlan) -> None:
    """With q=100 the reserve is 10 calls, so 50 left calls do not conserve (C21)."""
    free = plan_polling(budget=3000, quota_limit=100, endpoints=HOME_B)

    _, mode = throttle_factor(
        plan=free,
        info=_info(50, limit=100, reset_in=43200),
        reserve=1000,
        entries_on_account=1,
        blocked=False,
        now=NOW,
    )
    _, low = throttle_factor(
        plan=free,
        info=_info(10, limit=100, reset_in=43200),
        reserve=1000,
        entries_on_account=1,
        blocked=False,
        now=NOW,
    )

    assert mode is PlanMode.OK
    assert low is PlanMode.CONSERVING


def test_reserve_without_a_known_limit(plan: PollPlan) -> None:
    """Without q= the configured reserve applies as it is."""
    _, mode = throttle_factor(
        plan=plan,
        info=_info(900, limit=None),
        reserve=1000,
        entries_on_account=1,
        blocked=False,
        now=NOW,
    )

    assert mode is PlanMode.CONSERVING


def test_effective_period_per_mode(plan: PollPlan) -> None:
    """Conserving pauses optional endpoints and slows required ones to their maximum."""
    assert effective_period(
        plan=plan, key="weather", optional=True, factor=1.0, mode=PlanMode.OK
    ) == timedelta(seconds=900)
    assert (
        effective_period(
            plan=plan,
            key="weather",
            optional=True,
            factor=1.0,
            mode=PlanMode.CONSERVING,
        )
        is None
    )
    assert effective_period(
        plan=plan, key=ROOMS, optional=False, factor=1.0, mode=PlanMode.CONSERVING
    ) == timedelta(hours=1)
    assert effective_period(
        plan=plan, key="home_state", optional=False, factor=2, mode=PlanMode.BLOCKED
    ) == timedelta(seconds=600)


def _due(
    plan: PollPlan,
    last: dict[str, datetime],
    *,
    after: int,
    factor: float = 1.0,
    mode: PlanMode = PlanMode.OK,
) -> frozenset[str]:
    return endpoints_due(
        plan=plan,
        keys=list(plan.periods),
        optional=OPTIONAL,
        last=last,
        now=NOW + timedelta(seconds=after),
        factor=factor,
        mode=mode,
    )


def test_endpoints_due_per_tier(plan: PollPlan) -> None:
    """Endpoints never read are due; the others when their period has passed."""
    last = dict.fromkeys(plan.periods, NOW)

    assert _due(plan, {}, after=0) == frozenset(plan.periods)
    assert _due(plan, last, after=30) == frozenset()
    assert _due(plan, last, after=45) == {ROOMS}
    assert _due(plan, last, after=299) == {ROOMS, "home_state"}
    assert _due(plan, last, after=900) == {
        ROOMS,
        "home_state",
        "weather",
        "air_comfort",
        "topology",
    }
    assert _due(plan, last, after=3600) == frozenset(plan.periods) - {"home_info"}
    assert _due(plan, last, after=86400) == frozenset(plan.periods)


def test_endpoints_due_with_a_factor(plan: PollPlan) -> None:
    """A throttle factor delays every tier."""
    last = dict.fromkeys(plan.periods, NOW)

    assert _due(plan, last, after=100, factor=3) == frozenset()
    assert _due(plan, last, after=135, factor=3) == {ROOMS}


def test_endpoints_due_while_conserving(plan: PollPlan) -> None:
    """Conserving reads only the required endpoints, at their maximum period."""
    last = dict.fromkeys(plan.periods, NOW)

    assert _due(plan, {}, after=0, mode=PlanMode.CONSERVING) == {
        ROOMS,
        "home_state",
        "topology",
    }
    assert _due(plan, last, after=3600, mode=PlanMode.CONSERVING) == {ROOMS}
    assert _due(plan, last, after=86400, mode=PlanMode.CONSERVING) == {
        ROOMS,
        "home_state",
        "topology",
    }


@pytest.mark.parametrize(
    ("keys", "mode", "seconds"),
    [
        ((ROOMS,), PlanMode.OK, 45),
        (("home_state", "weather"), PlanMode.OK, 300),
        (("home_state", "weather"), PlanMode.CONSERVING, 21600),
        (("weather", "air_comfort"), PlanMode.CONSERVING, 3600),
    ],
    ids=["rooms", "home", "home_conserving", "all_paused"],
)
def test_coordinator_interval(
    plan: PollPlan, keys: tuple[str, ...], mode: PlanMode, seconds: int
) -> None:
    """A coordinator polls at its shortest active period, else at the rooms maximum."""
    interval = coordinator_interval(
        plan=plan, keys=keys, optional=OPTIONAL, factor=1.0, mode=mode
    )

    assert interval == timedelta(seconds=seconds)


def test_endpoint_table_matches_the_design() -> None:
    """The planner of home B with the endpoint table gives the documented plan."""
    keys = (
        ROOMS,
        "home_state",
        "weather",
        "air_comfort",
        "topology",
        "running_times",
        "home_info",
    )

    plan = plan_polling(
        budget=3000,
        quota_limit=None,
        endpoints=[ENDPOINT_RATES[key] for key in keys],
    )

    assert plan.calls_per_day == 2521
