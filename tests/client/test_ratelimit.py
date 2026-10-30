"""Tests for the ratelimit header parser, the call counter and the quota gate."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
import logging

from freezegun.api import FrozenDateTimeFactory
from multidict import CIMultiDict
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tado_x.client import (
    CallCounter,
    MemoryTokenStore,
    Priority,
    QuotaTracker,
    RateLimitInfo,
    TadoXRateLimitError,
    TokenManager,
    TokenSet,
    Transport,
    parse_rate_limit,
)
from custom_components.tado_x.client.const import HOPS_API_URL, MY_API_URL
from tests.tado_mock import HOME_B, TadoCloudMock, entry_tokens, load_fixture

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
POLICY = '"perday";q=20000;w=86400'

pytestmark = pytest.mark.freeze_time(NOW)


def _info(
    remaining: int | None, reset_in: int | None = None, limit: int | None = 20000
) -> RateLimitInfo:
    return RateLimitInfo(
        limit=limit,
        window=timedelta(days=1) if limit is not None else None,
        remaining=remaining,
        reset_in=None if reset_in is None else timedelta(seconds=reset_in),
        observed_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (load_fixture("ratelimit_headers.json")["normal"], (20000, 86400, 17833, None)),
        (load_fixture("ratelimit_headers.json")["exhausted"], (20000, 86400, 0, 7082)),
        (
            {"RateLimit-Policy": POLICY, "RateLimit": '"perday";r=5'},
            (20000, 86400, 5, None),
        ),
        ({"ratelimit": "perday ; r = 12 ; t = 60"}, (None, None, 12, 60)),
        ({"ratelimit-policy": POLICY}, (20000, 86400, None, None)),
        (
            {
                "ratelimit": '"perhour";r=5;t=30, "perday";r=100',
                "ratelimit-policy": POLICY,
            },
            (20000, 86400, 100, None),
        ),
        (
            {
                "ratelimit": '"burst";r=50, "sustained";r=10;t=99',
                "ratelimit-policy": '"burst";q=60;w=60, "sustained";q=500;w=3600',
            },
            (500, 3600, 10, 99),
        ),
        (
            {"ratelimit": '"a";r=5', "ratelimit-policy": '"b";q=100, "c";q=50'},
            (50, None, 5, None),
        ),
        (
            {"ratelimit": '"perday";r=-1;t=5', "ratelimit-policy": POLICY},
            (20000, 86400, None, None),
        ),
        ({"ratelimit": '"perday";r=12345678901'}, None),
        ({"ratelimit": '"perday";r="12"'}, None),
        ({"ratelimit": "garbage"}, None),
        ({"ratelimit": '"perday";r=1_000'}, None),
        ({"content-type": "application/json"}, None),
    ],
)
def test_parse_rate_limit(
    headers: dict[str, str], expected: tuple[int | None, ...] | None
) -> None:
    """Header variants parse to limit, window, remaining and reset, or to None."""
    now = datetime.now(UTC)
    info = parse_rate_limit(headers, now)
    if expected is None:
        assert info is None
        return
    assert info is not None
    limit, window, remaining, reset_in = expected
    assert info.limit == limit
    assert info.window == (None if window is None else timedelta(seconds=window))
    assert info.remaining == remaining
    assert info.reset_in == (None if reset_in is None else timedelta(seconds=reset_in))
    assert info.observed_at == now


def test_parse_rate_limit_repeated_headers() -> None:
    """Every value of a repeated header counts, whatever the name's case."""
    headers = CIMultiDict(
        [("ratelimit", '"perhour";r=5'), ("RateLimit", '"perday";r=100')]
    )
    info = parse_rate_limit(headers, NOW)
    assert info is not None
    assert info.remaining == 100


def test_parse_rate_limit_logs_garbage(caplog: pytest.LogCaptureFixture) -> None:
    """Unreadable headers are logged at debug level only."""
    with caplog.at_level(logging.DEBUG):
        assert parse_rate_limit({"ratelimit": "garbage"}, NOW) is None
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]


def test_priority_order() -> None:
    """Commands rank above polls."""
    assert Priority.COMMAND > Priority.POLL


def test_call_counter_window(freezer: FrozenDateTimeFactory) -> None:
    """Calls count for 24 hours in one-minute buckets."""
    counter = CallCounter()
    assert counter.total(NOW) == 0
    counter.record(NOW)
    counter.record(NOW + timedelta(seconds=30))
    counter.record(NOW + timedelta(hours=12))
    assert counter.total(NOW + timedelta(hours=12)) == 3
    assert counter.total(NOW + timedelta(hours=23, minutes=59)) == 3
    assert counter.total(NOW + timedelta(hours=24)) == 1
    assert counter.total(NOW + timedelta(hours=36)) == 0


def test_call_counter_clock_going_backwards() -> None:
    """A call stamped before the newest bucket counts into the newest bucket."""
    counter = CallCounter()
    counter.record(NOW)
    counter.record(NOW - timedelta(minutes=5))
    assert counter.total(NOW) == 2
    assert counter.total(NOW + timedelta(hours=24)) == 0


def test_call_counter_dump_and_restore() -> None:
    """A restore after downtime drops the calls that left the window meanwhile."""
    assert CallCounter().dump() == []

    counter = CallCounter()
    counter.record(NOW - timedelta(hours=23))
    counter.record(NOW)
    data = counter.dump()
    assert len(data) == 1441

    restored = CallCounter()
    restored.restore(data, NOW + timedelta(minutes=10))
    assert restored.total(NOW + timedelta(minutes=10)) == 2

    restored = CallCounter()
    restored.restore(data, NOW + timedelta(hours=2))
    assert restored.total(NOW + timedelta(hours=2)) == 1

    restored = CallCounter()
    restored.restore(data, NOW + timedelta(days=3))
    assert restored.total(NOW + timedelta(days=3)) == 0


@pytest.mark.parametrize(
    "data",
    [[], [1] * 1440, [1] * 1442, [1, -1, *([0] * 1439)], [1, True, *([0] * 1439)]],
)
def test_call_counter_ignores_bad_dumps(data: list[int]) -> None:
    """Data that is not a dump leaves the counter as it was."""
    counter = CallCounter()
    counter.record(NOW)
    counter.restore(data, NOW)
    assert counter.total(NOW) == 1


def test_gate_closes_on_exhausted_quota(freezer: FrozenDateTimeFactory) -> None:
    """r=0 with t= refuses everything until t plus 30 seconds."""
    quota = QuotaTracker()
    quota.check(Priority.POLL)
    quota.observe(_info(0, reset_in=7082), status=200)
    assert quota.blocked_until == NOW + timedelta(seconds=7112)

    for priority in Priority:
        with pytest.raises(TadoXRateLimitError) as err:
            quota.check(priority)
        assert err.value.local
        assert err.value.reset_at == quota.blocked_until

    freezer.tick(timedelta(seconds=7112))
    quota.check(Priority.POLL)
    quota.check(Priority.COMMAND)


def test_429_with_reset_time(freezer: FrozenDateTimeFactory) -> None:
    """A 429 with t= closes the gate until t plus 30 seconds."""
    quota = QuotaTracker()
    quota.observe(_info(0, reset_in=600), status=429)
    assert quota.blocked_until == NOW + timedelta(seconds=630)
    assert quota.latest is not None
    assert quota.latest.remaining == 0


def test_429_backoff_without_reset_time(freezer: FrozenDateTimeFactory) -> None:
    """A 429 without t= backs off 15, 30, 60 and 120 minutes; a 2xx resets it."""
    quota = QuotaTracker()
    for minutes in (15, 30, 60, 120, 120):
        quota.observe(None, status=429)
        assert quota.blocked_until == datetime.now(UTC) + timedelta(minutes=minutes)
        with pytest.raises(TadoXRateLimitError):
            quota.check(Priority.COMMAND)
        freezer.tick(timedelta(minutes=minutes))
        quota.check(Priority.POLL)

    quota.observe(_info(500), status=200)
    assert quota.blocked_until is None
    quota.observe(None, status=429)
    assert quota.blocked_until == datetime.now(UTC) + timedelta(minutes=15)


def test_concurrent_429s_count_as_one_step(freezer: FrozenDateTimeFactory) -> None:
    """429s that arrive while the gate is already closed do not extend it."""
    quota = QuotaTracker()
    quota.observe(None, status=429)
    quota.observe(None, status=429)
    quota.observe(_info(0), status=429)
    assert quota.blocked_until == NOW + timedelta(minutes=15)


def test_other_statuses_keep_the_gate(freezer: FrozenDateTimeFactory) -> None:
    """Only a 2xx re-opens the gate; an error status leaves it as it is."""
    quota = QuotaTracker()
    quota.observe(None, status=429)
    blocked_until = quota.blocked_until
    quota.observe(_info(10), status=500)
    assert quota.blocked_until == blocked_until
    quota.observe(None, status=204)
    assert quota.blocked_until is None


def test_poll_floor_with_known_reset(freezer: FrozenDateTimeFactory) -> None:
    """At the floor polls wait for the reset and commands still pass."""
    quota = QuotaTracker()
    quota.observe(_info(QuotaTracker.POLL_FLOOR + 1), status=200)
    quota.check(Priority.POLL)

    quota.observe(_info(QuotaTracker.POLL_FLOOR, reset_in=3600), status=200)
    quota.check(Priority.COMMAND)
    with pytest.raises(TadoXRateLimitError) as err:
        quota.check(Priority.POLL)
    assert err.value.local
    assert err.value.reset_at == NOW + timedelta(hours=1)

    freezer.tick(timedelta(hours=1))
    quota.check(Priority.POLL)


def test_poll_floor_without_reset_rechecks(freezer: FrozenDateTimeFactory) -> None:
    """Without t= one poll per 15 minutes may re-read the quota."""
    quota = QuotaTracker()
    quota.observe(_info(3), status=200)
    with pytest.raises(TadoXRateLimitError) as err:
        quota.check(Priority.POLL)
    assert err.value.reset_at is None

    freezer.tick(timedelta(minutes=15))
    quota.check(Priority.POLL)


def test_policy_is_kept_when_a_response_lacks_it() -> None:
    """A response without ratelimit-policy keeps the known limit."""
    quota = QuotaTracker()
    quota.observe(_info(100), status=200)
    quota.observe(_info(99, limit=None), status=200)
    assert quota.latest is not None
    assert (quota.latest.limit, quota.latest.window) == (20000, timedelta(days=1))
    assert quota.latest.remaining == 99


def test_record_call_counts(freezer: FrozenDateTimeFactory) -> None:
    """Every sent request counts in the rolling counter."""
    quota = QuotaTracker()
    quota.record_call()
    quota.record_call()
    assert quota.calls.total(NOW) == 2


def test_listeners() -> None:
    """Listeners run on every observed response and on restore until removed."""
    quota = QuotaTracker()
    seen: list[int | None] = []

    def listener() -> None:
        seen.append(None if quota.latest is None else quota.latest.remaining)

    remove = quota.add_listener(listener)
    quota.record_call()
    quota.observe(_info(7), status=200)
    quota.observe(None, status=500)
    quota.restore({})
    assert seen == [7, 7, None]

    remove()
    remove()
    quota.observe(_info(6), status=200)
    assert seen == [7, 7, None]


def test_as_dict_and_restore(freezer: FrozenDateTimeFactory) -> None:
    """The persisted state survives a restart, including the closed gate."""
    quota = QuotaTracker()
    quota.observe(None, status=429)
    quota.observe(_info(0, reset_in=600), status=500)
    data = quota.as_dict()
    assert data == {
        "latest": {
            "limit": 20000,
            "window": 86400,
            "remaining": 0,
            "reset_in": 600,
            "observed_at": NOW.isoformat(),
        },
        "blocked_until": (NOW + timedelta(seconds=630)).isoformat(),
        "backoff_step": 1,
    }

    restored = QuotaTracker()
    restored.restore(data)
    assert restored.as_dict() == data
    with pytest.raises(TadoXRateLimitError):
        restored.check(Priority.COMMAND)

    assert QuotaTracker().as_dict() == {
        "latest": None,
        "blocked_until": None,
        "backoff_step": 0,
    }


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"latest": "nope", "blocked_until": 5, "backoff_step": -1},
        {"latest": {"observed_at": "yesterday"}, "blocked_until": "soon"},
        {"latest": {"remaining": 3}, "backoff_step": True},
    ],
)
def test_restore_drops_unreadable_state(data: dict[str, object]) -> None:
    """A damaged state restores as an open gate without quota information."""
    quota = QuotaTracker()
    quota.restore(data)
    assert quota.as_dict() == QuotaTracker().as_dict()


def test_restore_reads_naive_times_as_utc() -> None:
    """Naive timestamps in a persisted state are UTC; bad numbers become None."""
    quota = QuotaTracker()
    quota.restore(
        {
            "latest": {
                "limit": "many",
                "remaining": 4,
                "window": None,
                "reset_in": 60,
                "observed_at": "2026-10-30T10:00:00",
            },
            "blocked_until": "2026-10-30T11:00:00",
        }
    )
    assert quota.latest == RateLimitInfo(
        limit=None,
        window=None,
        remaining=4,
        reset_in=timedelta(seconds=60),
        observed_at=NOW,
    )
    assert quota.blocked_until == NOW + timedelta(hours=1)


def test_late_success_keeps_a_closed_gate(freezer: FrozenDateTimeFactory) -> None:
    """A 2xx to a request sent before a 429 neither reopens the gate nor replaces its quota."""
    quota = QuotaTracker()
    earlier = quota.record_call()
    later = quota.record_call()
    quota.observe(_info(0, reset_in=7082), status=429, ticket=later)
    blocked_until = quota.blocked_until
    exhausted = quota.latest

    quota.observe(_info(1), status=200, ticket=earlier)

    assert quota.blocked_until == blocked_until
    assert quota.latest == exhausted
    with pytest.raises(TadoXRateLimitError):
        quota.check(Priority.COMMAND)

    freezer.tick(timedelta(seconds=7112))
    probe = quota.record_call()
    quota.observe(_info(20000), status=200, ticket=probe)
    assert quota.blocked_until is None
    quota.observe(_info(1), status=200, ticket=earlier)
    assert quota.latest is not None
    assert quota.latest.remaining == 1


@pytest.fixture
def quota() -> QuotaTracker:
    """Return the quota tracker of the transport."""
    return QuotaTracker()


@pytest.fixture
async def transport(
    aioclient_mock: AiohttpClientMocker, quota: QuotaTracker
) -> AsyncGenerator[Transport]:
    """Return a transport with a fresh token of home 1000002."""
    access, refresh = entry_tokens(HOME_B)
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    fresh = TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    yield Transport(session, TokenManager(session, MemoryTokenStore(fresh)), quota)
    await session.close()


async def test_late_success_over_http_keeps_a_closed_gate(
    mock_tado: TadoCloudMock, transport: Transport, quota: QuotaTracker
) -> None:
    """Through the transport, a slow 2xx sent before a 429 leaves the gate closed."""
    rooms = f"{HOPS_API_URL}/homes/{HOME_B}/rooms"
    state = f"{MY_API_URL}/homes/{HOME_B}/state"
    release = mock_tado.hold("GET", rooms)
    mock_tado.fail("GET", state, status=429)

    slow = asyncio.create_task(transport.request("GET", rooms))
    await asyncio.sleep(0)
    with pytest.raises(TadoXRateLimitError):
        await transport.request("GET", state)
    blocked_until = quota.blocked_until
    release.set()
    await slow

    assert quota.blocked_until == blocked_until
    assert quota.latest is not None
    assert quota.latest.remaining == 0
    with pytest.raises(TadoXRateLimitError) as err:
        await transport.request("GET", rooms, priority=Priority.COMMAND)
    assert err.value.local


def test_known_hold_end(freezer: FrozenDateTimeFactory) -> None:
    """A hold has a known end only when it waits for a reset tado sent."""
    quota = QuotaTracker()
    assert quota.known_hold_end(NOW) is None

    quota.observe(_info(500), status=200)
    assert quota.known_hold_end(NOW) is None

    quota.observe(_info(500, reset_in=60), status=200)
    assert quota.known_hold_end(NOW) is None

    quota.observe(_info(QuotaTracker.POLL_FLOOR, reset_in=600), status=200)
    assert quota.known_hold_end(NOW) == NOW + timedelta(seconds=600)

    quota.observe(_info(0, reset_in=7082), status=429)
    assert quota.known_hold_end(NOW) == NOW + timedelta(seconds=7112)

    freezer.tick(timedelta(seconds=7112))
    quota.observe(None, status=429)
    assert quota.blocked_until is not None
    assert quota.known_hold_end(datetime.now(UTC)) is None
