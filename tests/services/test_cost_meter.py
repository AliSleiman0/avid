"""The cost meter — §6.10.6's mandatory instrumentation (#105).

Real :class:`AsyncioEventBus`, real :class:`FakeClock`, no mocks (SDS §14.3): the meter is a
real subscriber on a real bus, fed real ``conversation.turn_ended`` facts, so what it accumulates
is what it would accumulate in the running robot. It publishes nothing (a pure consumer), so the
assertions read its public metrics surface — the same numbers a future ``/metrics`` endpoint
reports (SDS §2180) — after the bus has delivered each fact.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import NamedTuple
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock
from avid.domain import ConversationTurnEnded, TokenUsage
from avid.services.cost_meter import CostMeterService

# The pinned default model (config [ai] model) → the SDS §6.10.1 mini rates.
_MODEL = "gpt-realtime-mini-2025-12-15"

# A publish reaches its subscriber in a couple of scheduler turns; a whole second is a generous
# ceiling that still fails fast if the bus ever wedges.
_ARRIVAL_TIMEOUT_S = 1.0


class _SignallingMeter(CostMeterService):
    """``CostMeterService`` plus an awaitable "a turn was metered" signal.

    Instrumentation, not a mock: it runs the real meter and only *notices* when a turn has been
    folded in — needed because the meter publishes nothing, so a test cannot otherwise know the
    bus has delivered a fact (asserting after a fixed sleep would be a race that fails on CI).
    """

    def __init__(self, *, bus: AsyncioEventBus, clock: Clock, model: str) -> None:
        super().__init__(bus=bus, clock=clock, model=model)
        self._metered = asyncio.Event()

    async def _on_turn_ended(self, event: ConversationTurnEnded) -> None:
        await super()._on_turn_ended(event)
        self._metered.set()

    async def wait_for(self, count: int) -> None:
        """Block until at least *count* turns have been metered, or fail the test."""
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while self.turns < count:
                self._metered.clear()
                if self.turns >= count:
                    return
                await self._metered.wait()


class Rig(NamedTuple):
    meter: _SignallingMeter
    bus: AsyncioEventBus
    clock: FakeClock


async def _rig(model: str = _MODEL) -> AsyncIterator[Rig]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    meter = _SignallingMeter(bus=bus, clock=FakeClock(), model=model)
    for sub in meter.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    await bus.start()
    await meter.start()
    try:
        yield Rig(meter, bus, clock)
    finally:
        await meter.stop()
        await bus.stop()


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async for r in _rig():
        yield r


def _turn_ended(*, usage: TokenUsage, clock: FakeClock) -> ConversationTurnEnded:
    """A ``conversation.turn_ended`` as ``ConversationService`` would publish it."""
    return ConversationTurnEnded(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=clock.now() * 1000,
        monotonic_ns=clock.monotonic_ns(),
        source="ConversationService",
        duration_ms=1000,
        usage=usage,
    )


# --- AC-5: the meter accumulates usage and projects monthly spend --------------------------


async def test_meter_accumulates_usage_and_costs_the_turn(rig: Rig) -> None:
    """One turn: the counts fold into the running total and the dollar cost matches §6.10.1.

    mini rates per 1M: cached $0.30, uncached $10.00, output $20.00. For 256 cached + 64 uncached
    input + 48 output: (256·0.30 + 64·10 + 48·20) / 1e6 = $0.0016768."""
    usage = TokenUsage(input_tokens=320, cached_input_tokens=256, output_tokens=48)
    await rig.bus.publish(_turn_ended(usage=usage, clock=rig.clock))
    await rig.meter.wait_for(1)

    assert rig.meter.total_usage == usage
    assert rig.meter.total_cost_usd == pytest.approx(0.0016768)
    # cached / total input = 256 / 320.
    assert rig.meter.cached_ratio == pytest.approx(0.8)


async def test_two_turns_sum_and_project_monthly(rig: Rig) -> None:
    """Two turns accumulate field-wise (TokenUsage.__add__) and the monthly projection is the
    per-turn average × the §6.10.3 model (20 turns/day × 30 days)."""
    u1 = TokenUsage(input_tokens=320, cached_input_tokens=256, output_tokens=48)
    u2 = TokenUsage(input_tokens=410, cached_input_tokens=384, output_tokens=40)
    await rig.bus.publish(_turn_ended(usage=u1, clock=rig.clock))
    await rig.bus.publish(_turn_ended(usage=u2, clock=rig.clock))
    await rig.meter.wait_for(2)

    assert rig.meter.turns == 2
    assert rig.meter.total_usage == u1 + u2
    # cost(u1)=0.0016768; cost(u2)=(384·0.30 + 26·10 + 40·20)/1e6 = (115.2+260+800)/1e6=0.0011752.
    expected_total = pytest.approx(0.0016768 + 0.0011752)
    assert rig.meter.total_cost_usd == expected_total
    per_turn = (0.0016768 + 0.0011752) / 2
    assert rig.meter.projected_monthly_usd == pytest.approx(per_turn * 20 * 30)


async def test_unknown_model_meters_tokens_but_skips_dollars() -> None:
    """A model with no rate row logs a warning and disables dollars, but still counts tokens —
    it never crashes the turn (the plan's log-and-skip)."""
    async for rig in _rig(model="some-future-model-not-in-the-table"):
        usage = TokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=20)
        await rig.bus.publish(_turn_ended(usage=usage, clock=rig.clock))
        await rig.meter.wait_for(1)

        assert rig.meter.total_usage == usage  # tokens still accrue
        assert rig.meter.total_cost_usd == 0.0  # dollars disabled
        assert rig.meter.projected_monthly_usd == pytest.approx(0.0)


async def test_projection_over_the_o7_budget_is_flagged(rig: Rig) -> None:
    """A pathological turn drives the projection past the O7 $25 budget — the §6.10.6 tripwire.

    100k uncached input + 100k output at mini rates = $3.00/turn → ~$1,800/mo projected, far over
    budget. The meter must surface that (a warning) rather than swallow it; here we assert the
    projection itself crossed the line."""
    usage = TokenUsage(
        input_tokens=100_000, cached_input_tokens=0, output_tokens=100_000
    )
    await rig.bus.publish(_turn_ended(usage=usage, clock=rig.clock))
    await rig.meter.wait_for(1)
    assert rig.meter.projected_monthly_usd > 25.0


async def test_no_turns_projects_zero(rig: Rig) -> None:
    """Before any turn is metered the projection is 0.0, not a divide-by-zero."""
    assert rig.meter.turns == 0
    assert rig.meter.projected_monthly_usd == 0.0
    assert rig.meter.cached_ratio == 0.0


def test_the_shipped_model_is_priced_from_an_exact_row_not_the_family_guess() -> None:
    """Whatever ``config/pi.toml`` actually runs must have its own rate row (SDS §6.10.5).

    The family fallback below exists so an unlisted snapshot *roll* still meters something rather
    than going silent on the number O7 is graded by. It is a safety net, not the resting state for
    the model we ship: a guess that happens to be right is indistinguishable from one that is not,
    and O7 is a milestone criterion. Reading the shipped name out of the config file is the point —
    a future model swap that forgets its rate row fails here rather than on an invoice."""
    import tomllib
    from pathlib import Path

    from avid.services.cost_meter import _RATES

    pi_toml = Path(__file__).resolve().parents[2] / "config" / "pi.toml"
    shipped = tomllib.loads(pi_toml.read_text(encoding="utf-8"))["ai"]["model"]
    assert shipped in _RATES, (
        f"config/pi.toml ships {shipped!r} with no exact rate row — O7 would be metered by the "
        f"mini-vs-flagship family guess. Add it to _RATES with its §6.10.1 published rates."
    )


def test_family_fallback_prices_an_unlisted_mini_snapshot() -> None:
    """An unlisted ``mini`` snapshot still meters via the family fallback, not the flagship."""
    bus = AsyncioEventBus()
    mini = CostMeterService(
        bus=bus, clock=FakeClock(), model="gpt-realtime-mini-2099-01-01"
    )
    flagship = CostMeterService(
        bus=bus, clock=FakeClock(), model="gpt-realtime-2099-01-01"
    )
    usage = TokenUsage(input_tokens=100, cached_input_tokens=0, output_tokens=0)
    # 100 uncached input tokens: mini $10/1M = $0.001; flagship $32/1M = $0.0032.
    assert mini._turn_cost_usd(usage) == pytest.approx(0.001)
    assert flagship._turn_cost_usd(usage) == pytest.approx(0.0032)
