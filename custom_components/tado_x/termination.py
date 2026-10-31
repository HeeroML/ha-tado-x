"""Termination policy of manual changes made from Home Assistant (#94).

Pure module: no Home Assistant imports.
"""

from datetime import timedelta
from enum import StrEnum
import logging
from typing import Final

from .client.models import Termination

_LOGGER: Final = logging.getLogger(__package__)


class TerminationPolicy(StrEnum):
    """When a manual change made from Home Assistant ends."""

    FOLLOW_APP = "follow_app"
    MANUAL = "manual"
    NEXT_TIME_BLOCK = "next_time_block"
    TIMER = "timer"


def describe_termination(termination: Termination) -> str:
    """Return a termination as a log text, such as "TIMER 3600 s"."""
    if termination.duration is None:
        return termination.type.value
    return f"{termination.type.value} {int(termination.duration.total_seconds())} s"


def _resolve(
    policy: TerminationPolicy,
    timer: timedelta,
    room_default: Termination | None,
    override: Termination | None,
) -> tuple[Termination, str]:
    """Return the termination and the rule that chose it."""
    if override is not None:
        return override, "override"
    match policy:
        case TerminationPolicy.MANUAL:
            return Termination.manual(), policy.value
        case TerminationPolicy.NEXT_TIME_BLOCK:
            return Termination.next_time_block(), policy.value
        case TerminationPolicy.TIMER:
            return Termination.timer(timer), policy.value
    if room_default is None:
        return Termination.next_time_block(), f"{policy.value} without an app default"
    return room_default, policy.value


def resolve_termination(
    *,
    room_id: int,
    policy: TerminationPolicy,
    timer: timedelta,
    room_default: Termination | None,
    override: Termination | None = None,
) -> Termination:
    """Return the termination to send with a manual change of a room.

    A per-call override wins, then a fixed policy. FOLLOW_APP uses the room's
    default from the tado app, and NEXT_TIME_BLOCK when that is unknown. The
    decision is logged at debug level, as in "Termination of room 3:
    follow_app -> TIMER 3600 s".
    """
    termination, rule = _resolve(policy, timer, room_default, override)
    _LOGGER.debug(
        "Termination of room %s: %s -> %s",
        room_id,
        rule,
        describe_termination(termination),
    )
    return termination
