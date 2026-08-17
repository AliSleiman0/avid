"""``BehaviorTools`` contract — the model's one behaviour verb (#243, P6).

SDS §14.4: one suite per port, run against every implementation. There is one implementation,
``BehaviorService``, and it satisfies the Protocol **structurally** — no inheritance, no
registration — exactly as ``AffectService`` satisfies ``AffectTools``. The suite exists so that
stays true: a signature drifting on either side breaks here rather than at a tool call on the Pi.

Modelled on ``tests/contract/test_affect_tools.py``, which is the same shape for the same reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from avid.adapters import FakeTriggerStore
from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import BehaviorTools
from avid.core.state_manager import StateManager
from avid.domain import PolicyLimits
from avid.services.behavior import BehaviorService

_START = 1_781_438_400

_LIMITS = PolicyLimits(
    quiet_start_minutes=22 * 60,
    quiet_end_minutes=7 * 60 + 30,
    presence_window_s=300,
    ambient_speech_threshold_s=60,
    global_cooldown_s=900,
    daily_budget=5,
)


@pytest.fixture
async def tools() -> AsyncIterator[BehaviorService]:
    clock = FakeClock(start=_START)
    bus = AsyncioEventBus(clock=clock)
    store = FakeTriggerStore(clock=clock)
    service = BehaviorService(
        bus=bus,
        clock=clock,
        state=StateManager(bus=bus, clock=clock),
        triggers=store,
        proactive_log=store,
        limits=_LIMITS,
        timezone="UTC",
        default_cooldown_s=900,
        hold_open_s=30.0,
        ignore_backoff_multiplier=2,
        stale_grace_s=600,
        ignore_streak_limit=3,
    )
    try:
        yield service
    finally:
        await store.aclose()


async def test_the_service_satisfies_the_port(tools: BehaviorService) -> None:
    """Structural, like ``AffectService``/``AffectTools``: nothing declares the relationship, so
    this assertion is the only thing holding the two signatures together."""
    assert isinstance(tools, BehaviorTools)


async def test_set_quiet_returns_the_instant_it_lifts(tools: BehaviorService) -> None:
    """Not an echo of the duration: the model repeats this to the user, and an instant is
    checkable in a way that "for an hour" is not."""
    until = await tools.set_quiet(3600, correlation_id=uuid4())
    assert until == _START + 3600


async def test_a_second_request_extends_and_never_shortens(
    tools: BehaviorService,
) -> None:
    """⚠️ The property worth having a contract test for.

    Someone who says "leave me alone" twice is not asking for *less* quiet. Taking the later of the
    two instants means a careless second call — a shorter one, or the HTTP door firing while the
    tool door already has — cannot cut the first request short.
    """
    first = await tools.set_quiet(3600, correlation_id=uuid4())
    second = await tools.set_quiet(60, correlation_id=uuid4())
    assert second == first

    third = await tools.set_quiet(7200, correlation_id=uuid4())
    assert third == _START + 7200


@pytest.mark.parametrize("bad", [0, -1, -3600])
async def test_a_non_positive_duration_is_rejected(
    tools: BehaviorService, bad: int
) -> None:
    """Raises rather than silently doing nothing — the dispatcher turns it into a tool error, which
    is a thing the model can react to. A no-op would leave the user believing they had asked for
    quiet and the robot believing nothing had been asked."""
    with pytest.raises(ValueError, match="positive"):
        await tools.set_quiet(bad, correlation_id=uuid4())


async def test_the_override_actually_suppresses_a_proposal(
    tools: BehaviorService,
) -> None:
    """AC-5's end-to-end claim, at the seam where it is provable: the override reaches the same
    ``PolicyContext`` §10.4's gate reads, and rule 1 vetoes on it — outside the static window, at a
    time of day that otherwise delivers."""
    import math

    from avid.domain import Delivered, RobotState, Suppressed, evaluate_policy

    tools._robot_state = RobotState.IDLE  # noqa: SLF001 - assembling the world by hand
    tools._last_present_at = tools._monotonic_s()  # noqa: SLF001 - as above

    before = await tools._context(  # noqa: SLF001 - as above
        now=tools._clock.now(),  # noqa: SLF001
        trigger_last_fired_s=math.inf,
        trigger_cooldown_s=900.0,
    )
    assert isinstance(evaluate_policy(before, limits=_LIMITS), Delivered)

    await tools.set_quiet(3600, correlation_id=uuid4())
    after = await tools._context(  # noqa: SLF001 - as above
        now=tools._clock.now(),  # noqa: SLF001
        trigger_last_fired_s=math.inf,
        trigger_cooldown_s=900.0,
    )
    verdict = evaluate_policy(after, limits=_LIMITS)
    assert isinstance(verdict, Suppressed)
    assert verdict.rule == "quiet_hours"
