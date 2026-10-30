"""Quota tracking from tado's ratelimit headers, and the local quota gate."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import IntEnum
from typing import Any, Final

from .models import RateLimitInfo, utcnow


def parse_rate_limit(headers: Mapping[str, str], now: datetime) -> RateLimitInfo | None:
    """Parse the ratelimit-policy and ratelimit headers of one response.

    Header names are case-insensitive and getall() is used when available.
    Members are split on "," and parameters on ";". The "perday" member is
    preferred, otherwise the one with the lowest r. Only integers are
    accepted; garbage gives None.
    """
    raise NotImplementedError


class Priority(IntEnum):
    """Priority of a request at the local quota gate."""

    POLL = 0
    COMMAND = 1


class CallCounter:
    """Own API calls over a rolling 24 hours in 1,440 one-minute buckets."""

    def __init__(self) -> None:
        """Initialize an empty counter."""
        raise NotImplementedError

    def record(self, now: datetime) -> None:
        """Count one call at the given time."""
        raise NotImplementedError

    def total(self, now: datetime) -> int:
        """Return the calls of the 24 hours before now."""
        raise NotImplementedError

    def dump(self) -> list[int]:
        """Return the buckets for persistence."""
        raise NotImplementedError

    def restore(self, data: Sequence[int], now: datetime) -> None:
        """Restore persisted buckets."""
        raise NotImplementedError


class QuotaTracker:
    """Account-wide quota state of one config entry and the local gate.

    The gate closes on r=0 with t=, or on a 429. While it is closed nothing is
    sent. Below POLL_FLOOR remaining calls, polls are refused and commands
    still pass.
    """

    POLL_FLOOR: Final = 20
    BACKOFF_429: Final = (900, 1800, 3600, 7200)

    latest: RateLimitInfo | None
    blocked_until: datetime | None
    calls: CallCounter

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        """Initialize an open gate with no quota information."""
        raise NotImplementedError

    def check(self, priority: Priority) -> None:
        """Raise TadoXRateLimitError(local=True) when the gate refuses the request."""
        raise NotImplementedError

    def observe(self, info: RateLimitInfo | None, *, status: int) -> None:
        """Take the headers and the status of a response into account."""
        raise NotImplementedError

    def record_call(self) -> None:
        """Count one sent request."""
        raise NotImplementedError

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call cb after every change; return a function that removes it."""
        raise NotImplementedError

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted state: latest info, gate and backoff."""
        raise NotImplementedError

    def restore(self, data: Mapping[str, Any]) -> None:
        """Restore a state written by as_dict()."""
        raise NotImplementedError
