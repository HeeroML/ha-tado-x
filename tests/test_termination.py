"""Tests for the termination policy of manual changes (#94)."""

from datetime import timedelta

import pytest

from custom_components.tado_x.client import Termination
from custom_components.tado_x.termination import TerminationPolicy, resolve_termination

TIMER = timedelta(minutes=45)
ROOM_TIMER = Termination.timer(timedelta(hours=1))
OVERRIDE = Termination.timer(timedelta(minutes=10))


@pytest.mark.parametrize("policy", list(TerminationPolicy))
@pytest.mark.parametrize("room_default", [None, ROOM_TIMER, Termination.manual()])
def test_override_wins(
    policy: TerminationPolicy, room_default: Termination | None
) -> None:
    """A per-call termination beats every policy and the room default."""
    assert (
        resolve_termination(
            policy=policy, timer=TIMER, room_default=room_default, override=OVERRIDE
        )
        == OVERRIDE
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (TerminationPolicy.MANUAL, Termination.manual()),
        (TerminationPolicy.NEXT_TIME_BLOCK, Termination.next_time_block()),
        (TerminationPolicy.TIMER, Termination.timer(TIMER)),
    ],
)
def test_fixed_policy_beats_room_default(
    policy: TerminationPolicy, expected: Termination
) -> None:
    """A fixed policy ignores the tado app setting of the room."""
    assert (
        resolve_termination(policy=policy, timer=TIMER, room_default=ROOM_TIMER)
        == expected
    )


@pytest.mark.parametrize(
    "room_default",
    [Termination.manual(), Termination.next_time_block(), ROOM_TIMER],
)
def test_follow_app_uses_room_default(room_default: Termination) -> None:
    """follow_app sends the tado app setting of the room."""
    assert (
        resolve_termination(
            policy=TerminationPolicy.FOLLOW_APP, timer=TIMER, room_default=room_default
        )
        == room_default
    )


def test_follow_app_without_room_default() -> None:
    """An unknown app setting falls back to the next time block, never a timer."""
    assert (
        resolve_termination(
            policy=TerminationPolicy.FOLLOW_APP, timer=TIMER, room_default=None
        )
        == Termination.next_time_block()
    )


def test_timer_option_body() -> None:
    """The timer option with 45 minutes sends 2,700 seconds."""
    termination = resolve_termination(
        policy=TerminationPolicy.TIMER,
        timer=timedelta(minutes=45),
        room_default=None,
    )
    assert termination.to_api() == {"type": "TIMER", "durationInSeconds": 2700}


def test_policy_values_are_option_values() -> None:
    """The policy values are the stored option and selector values."""
    assert [policy.value for policy in TerminationPolicy] == [
        "follow_app",
        "manual",
        "next_time_block",
        "timer",
    ]
