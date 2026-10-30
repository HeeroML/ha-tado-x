"""Quota tracking from tado's ratelimit headers, and the local quota gate."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from http import HTTPStatus
import logging
import re
from typing import Any, Final

from .errors import TadoXRateLimitError
from .models import RateLimitInfo, utcnow

_LOGGER = logging.getLogger(__package__)

_PER_DAY: Final = "perday"
_INTEGER: Final = re.compile(r"[0-9]{1,9}")
_GATE_GRACE: Final = timedelta(seconds=30)
_FLOOR_RECHECK: Final = timedelta(minutes=15)
_BUCKETS: Final = 1440

type _Members = dict[str, dict[str, int]]


def _int_params(params: Iterable[str]) -> Iterable[tuple[str, int]]:
    for param in params:
        key, _, value = param.partition("=")
        if _INTEGER.fullmatch(value := value.strip()):
            yield key.strip().lower(), int(value)


def _members(headers: Mapping[str, str], name: str) -> _Members:
    """Return the integer parameters of every member of a structured header."""
    members: _Members = {}
    for key, value in headers.items():
        if key.lower() != name:
            continue
        for member in value.split(","):
            label, *params = member.split(";")
            members[label.strip().strip('"')] = dict(_int_params(params))
    return members


def _pick(members: _Members, key: str) -> tuple[str, dict[str, int]] | None:
    """Return the "perday" member, else the member with the lowest value of key."""
    candidates = {name: params for name, params in members.items() if key in params}
    if not candidates:
        return None
    if _PER_DAY in candidates:
        return _PER_DAY, candidates[_PER_DAY]
    return min(candidates.items(), key=lambda item: item[1][key])


def _seconds(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)


def parse_rate_limit(headers: Mapping[str, str], now: datetime) -> RateLimitInfo | None:
    """Parse the ratelimit-policy and ratelimit headers of one response.

    Header names are case-insensitive and every value of a repeated header
    counts. Members are split on "," and parameters on ";". The "perday"
    member is preferred, otherwise the one with the lowest r. Only integers
    are accepted; garbage gives None.
    """
    states = _members(headers, "ratelimit")
    policies = _members(headers, "ratelimit-policy")
    state = _pick(states, "r")
    policy = policies.get(state[0], {}) if state else {}
    if "q" not in policy and (picked := _pick(policies, "q")) is not None:
        policy = picked[1]
    params = state[1] if state else {}
    if "r" not in params and "q" not in policy:
        if states or policies:
            _LOGGER.debug("Ignoring unreadable ratelimit headers")
        return None
    return RateLimitInfo(
        limit=policy.get("q"),
        window=_seconds(policy.get("w")),
        remaining=params.get("r"),
        reset_in=_seconds(params.get("t")),
        observed_at=now,
    )


class Priority(IntEnum):
    """Priority of a request at the local quota gate."""

    POLL = 0
    COMMAND = 1


def _opt_count(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


class CallCounter:
    """Own API calls over a rolling 24 hours in 1,440 one-minute buckets."""

    def __init__(self) -> None:
        """Initialize an empty counter."""
        self._buckets = [0] * _BUCKETS
        self._newest: int | None = None

    def record(self, now: datetime) -> None:
        """Count one call at the given time."""
        minute = self._advance(now)
        self._buckets[minute % _BUCKETS] += 1

    def total(self, now: datetime) -> int:
        """Return the calls of the 24 hours before now."""
        self._advance(now)
        return sum(self._buckets)

    def dump(self) -> list[int]:
        """Return the epoch minute of the newest bucket followed by the buckets.

        The list is empty before the first call. Keeping the minute lets a
        restore after downtime drop the calls that left the window meanwhile.
        """
        if self._newest is None:
            return []
        return [self._newest, *self._buckets]

    def restore(self, data: Sequence[int], now: datetime) -> None:
        """Restore a dump(); anything else is ignored."""
        if len(data) != _BUCKETS + 1 or any(_opt_count(item) is None for item in data):
            return
        self._newest, self._buckets = data[0], list(data[1:])
        self._advance(now)

    def _advance(self, now: datetime) -> int:
        """Clear the buckets that left the window and return the current minute."""
        minute = int(now.timestamp()) // 60
        newest = self._newest
        if newest is None or minute - newest >= _BUCKETS:
            self._buckets = [0] * _BUCKETS
        else:
            for expired in range(newest + 1, minute + 1):
                self._buckets[expired % _BUCKETS] = 0
            # A clock that went backwards counts into the newest bucket.
            minute = max(minute, newest)
        self._newest = minute
        return minute


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _info_from_dict(raw: object) -> RateLimitInfo | None:
    if not isinstance(raw, Mapping):
        return None
    if (observed_at := _parse_time(raw.get("observed_at"))) is None:
        return None
    return RateLimitInfo(
        limit=_opt_count(raw.get("limit")),
        window=_seconds(_opt_count(raw.get("window"))),
        remaining=_opt_count(raw.get("remaining")),
        reset_in=_seconds(_opt_count(raw.get("reset_in"))),
        observed_at=observed_at,
    )


def _total_seconds(value: timedelta | None) -> int | None:
    return None if value is None else int(value.total_seconds())


class QuotaTracker:
    """Account-wide quota state of one config entry and the local gate.

    The gate closes on r=0 with t=, or on a 429. While it is closed nothing is
    sent. Below POLL_FLOOR remaining calls, polls are refused and commands
    still pass; without a known reset one poll per 15 minutes re-reads the
    quota, so polling cannot stop for good.
    """

    POLL_FLOOR: Final = 20
    BACKOFF_429: Final = (900, 1800, 3600, 7200)

    latest: RateLimitInfo | None
    blocked_until: datetime | None
    calls: CallCounter

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        """Initialize an open gate with no quota information."""
        self._clock = clock
        self.latest = None
        self.blocked_until = None
        self.calls = CallCounter()
        self._backoff_step = 0
        self._listeners: list[Callable[[], None]] = []

    def check(self, priority: Priority) -> None:
        """Raise TadoXRateLimitError(local=True) when the gate refuses the request."""
        now = self._clock()
        if self.blocked_until is not None and now < self.blocked_until:
            raise TadoXRateLimitError(self.latest, self.blocked_until, local=True)
        info = self.latest
        if (
            priority is Priority.POLL
            and info is not None
            and self._below_floor(info, now)
        ):
            raise TadoXRateLimitError(info, info.reset_at, local=True)

    def _below_floor(self, info: RateLimitInfo, now: datetime) -> bool:
        if info.remaining is None or info.remaining > self.POLL_FLOOR:
            return False
        return now < (info.reset_at or info.observed_at + _FLOOR_RECHECK)

    def observe(self, info: RateLimitInfo | None, *, status: int) -> None:
        """Take the headers and the status of a response into account."""
        if info is not None:
            self.latest = self._merged(info)
        success = HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES
        if success:
            self._backoff_step = 0
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            self._close_after_429(info)
        elif info is not None and info.remaining == 0 and info.reset_at is not None:
            self.blocked_until = info.reset_at + _GATE_GRACE
        elif success:
            self.blocked_until = None
        self._notify()

    def _merged(self, info: RateLimitInfo) -> RateLimitInfo:
        """Keep the last known limit and window when a response lacks the policy."""
        previous = self.latest
        if previous is None or info.limit is not None:
            return info
        return replace(
            info, limit=previous.limit, window=info.window or previous.window
        )

    def _close_after_429(self, info: RateLimitInfo | None) -> None:
        if info is not None and info.reset_at is not None:
            self.blocked_until = info.reset_at + _GATE_GRACE
            return
        now = self._clock()
        if self.blocked_until is not None and now < self.blocked_until:
            return
        step = min(self._backoff_step, len(self.BACKOFF_429) - 1)
        self.blocked_until = now + timedelta(seconds=self.BACKOFF_429[step])
        self._backoff_step += 1

    def record_call(self) -> None:
        """Count one sent request; listeners run when its response is observed."""
        self.calls.record(self._clock())

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call cb after every observed response and restore; return a remover."""
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted state: latest info, gate and backoff."""
        info = self.latest
        return {
            "latest": None
            if info is None
            else {
                "limit": info.limit,
                "window": _total_seconds(info.window),
                "remaining": info.remaining,
                "reset_in": _total_seconds(info.reset_in),
                "observed_at": info.observed_at.isoformat(),
            },
            "blocked_until": None
            if self.blocked_until is None
            else self.blocked_until.isoformat(),
            "backoff_step": self._backoff_step,
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        """Restore a state written by as_dict(); unreadable parts are dropped."""
        self.latest = _info_from_dict(data.get("latest"))
        self.blocked_until = _parse_time(data.get("blocked_until"))
        self._backoff_step = _opt_count(data.get("backoff_step")) or 0
        self._notify()
