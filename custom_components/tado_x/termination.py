"""Termination policy of manual changes made from Home Assistant (#94).

Pure module: no Home Assistant imports.
"""

from datetime import timedelta
from enum import StrEnum

from .client.models import Termination


class TerminationPolicy(StrEnum):
    """When a manual change made from Home Assistant ends."""

    FOLLOW_APP = "follow_app"
    MANUAL = "manual"
    NEXT_TIME_BLOCK = "next_time_block"
    TIMER = "timer"


def resolve_termination(
    *,
    policy: TerminationPolicy,
    timer: timedelta,
    room_default: Termination | None,
    override: Termination | None = None,
) -> Termination:
    """Return the termination to send with a manual change.

    A per-call override wins, then a fixed policy. FOLLOW_APP uses the room's
    default from the tado app, and NEXT_TIME_BLOCK when that is unknown.
    """
    if override is not None:
        return override
    match policy:
        case TerminationPolicy.MANUAL:
            return Termination.manual()
        case TerminationPolicy.NEXT_TIME_BLOCK:
            return Termination.next_time_block()
        case TerminationPolicy.TIMER:
            return Termination.timer(timer)
    return room_default or Termination.next_time_block()
