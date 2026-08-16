"""Contract suite for the ``AffectTools`` port (AVID-214, SDS §6.6, §6.8).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from the
real thing (P6). ``AffectTools`` is the first *tools* port to get one — ``MemoryTools`` never did,
and its port behaviour is exercised through ``tests/services/test_memory.py`` instead.

The real implementation is ``AffectService``, which satisfies the Protocol **structurally**, with
no inheritance and no edit to that service. So unlike the device ports there is no "fake adapter"
in ``adapters/`` at all: the two legs here are a minimal recording double (proving the *shape* is
implementable by something that is not the service) and the real service itself. Both are
constructible without hardware, so both run everywhere — the first contract suite in this repo
where the real leg is not skipped for want of a Pi.

What the port promises is narrow and that is the point: one method, one Tier-2 overlay, no return
value. §6.6 classifies it *async, fire-and-forget*, and §6.8's argument for tolerating its ~400 ms
round trip is that the Tier-1 baseline is never wrong in the meantime.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import AffectTools
from avid.domain import SEMANTIC_AFFECTS, Affect, AffectChanged
from avid.services.affect import AffectService


class _RecordingAffectTools:
    """A minimal ``AffectTools`` implementation that is *not* ``AffectService``.

    Present so the shared block proves the contract is satisfiable by the shape alone. If the port
    ever grew a promise only the service could keep, this leg would be the first to fail — which is
    what a contract suite is for."""

    def __init__(self) -> None:
        self.applied: list[tuple[Affect, UUID]] = []

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        self.applied.append((affect, correlation_id))


@pytest.fixture(params=["fake", "real"])
async def tools(request: pytest.FixtureRequest) -> AsyncIterator[AffectTools]:
    """Both implementations, and neither needs hardware or a key.

    The real leg needs a **started** bus — ``AffectService.set_affect`` publishes, and the bus
    refuses to publish before ``start()``. That is not incidental to the contract: this port's
    whole purpose is to end in an event."""
    if request.param != "real":
        yield _RecordingAffectTools()
        return
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    await bus.start()
    try:
        yield AffectService(bus=bus, clock=clock)
    finally:
        await bus.stop()


def test_adapter_satisfies_the_affect_tools_port(tools: AffectTools) -> None:
    assert isinstance(tools, AffectTools)


@pytest.mark.parametrize("affect", SEMANTIC_AFFECTS)
async def test_every_semantic_affect_is_accepted(
    tools: AffectTools, affect: Affect
) -> None:
    """The three Tier-2 overlays are the port's whole vocabulary, and every one must be settable.

    Parametrised over the domain tuple rather than a hand-written list, so an overlay added to
    ``SEMANTIC_AFFECTS`` is covered here the moment it exists."""
    await tools.set_affect(affect, correlation_id=uuid4())


async def test_set_affect_returns_nothing(tools: AffectTools) -> None:
    """§6.6 classifies it *fire-and-forget*: it returns when the overlay is recorded, not when a
    face reaches glass. A port that returned a render result would be inviting a caller to await
    one, which is exactly the ~400 ms §6.8 argues must stay off the turn path."""
    assert await tools.set_affect(Affect.HAPPY, correlation_id=uuid4()) is None


async def test_correlation_id_is_required_and_keyword_only(tools: AffectTools) -> None:
    """The signature difference from ``MemoryTools``, asserted rather than left to a docstring.

    ``MemoryTools`` makes ``correlation_id`` optional; this port requires it. An overlay with no
    turn behind it is a face change nothing can be traced back to (§9.1.1 — ids are *propagated*,
    never minted downstream), and a Protocol that had copied the neighbour's shape would not have
    been satisfied by ``AffectService`` at all."""
    with pytest.raises(TypeError):
        await tools.set_affect(Affect.HAPPY, uuid4())  # type: ignore[misc]


# --- the real service's own promise -----------------------------------------


async def test_the_real_service_publishes_one_tier_two_event() -> None:
    """The half only ``AffectService`` can keep, and the reason the gate's second clause holds.

    *"`set_affect` arrives, one event publishes, the face changes and the servo nods, and
    `ConversationService` never learned that a servo exists"* (§6.8). The dispatcher's side of that
    is covered in ``tests/services/test_tools.py``; this is the far side."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    service = AffectService(bus=bus, clock=clock)
    seen: list[AffectChanged] = []
    arrived = asyncio.Event()

    async def collect(event: AffectChanged) -> None:
        seen.append(event)
        arrived.set()

    bus.subscribe(AffectChanged, collect, name="test.affect_changed")
    await bus.start()
    try:
        corr = uuid4()
        await service.set_affect(Affect.CONFUSED, correlation_id=corr)
        # Waited on an Event, never a sleep: the bus cancels its workers on stop() rather than
        # draining them, so "publish then sleep a bit" is a race that passes locally and fails on
        # a loaded CI box.
        await asyncio.wait_for(arrived.wait(), 2.0)
    finally:
        await bus.stop()

    assert [(e.affect, e.tier, e.correlation_id) for e in seen] == [
        (Affect.CONFUSED, 2, corr)
    ]
