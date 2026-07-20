"""The drawer — one affect onto glass (AVID-72, SDS §3.6.1).

The other half of the split ``AffectService`` starts: that service *decides* which affect is
current and publishes ``affect.changed``; this one *draws* it. Deciding to be happy is domain
logic with a unit test; getting a happy face onto glass ends in an adapter carrying a device
dependency, and the seam between those two facts is what P1 exists to protect.

So this is the service that owns the :class:`~avid.core.ports.Display` port, and the only
thing in the system that calls ``render()`` (SDS §9.1.4). It **publishes nothing** — there is
no ``publish`` call in this module and a test asserts as much. Rendering is an *effect*, not a
fact: nobody needs to be notified that pixels moved, and P4 says an event reports what
happened rather than asking for something to happen. The render itself is a direct awaited
port call for the same reason — losing it would be a correctness bug, and the bus carries
notifications, not obligations.

**Why the faces are precomputed.** All eight are composed once, at construction, into frames
sized from the display. At 480x320 an ``RGB888`` frame is 460,800 bytes; composing one per
event would put ~100 ms of pure-Python work on the loop, blowing both O4's 150 ms end-to-end
budget and P8's 50 ms slow-callback gate — on the *event loop*, where it would stall the audio
path too. Precomputing turns the hot path into a dict lookup plus one ``await``, which makes
the latency criterion trivially true rather than marginal. The cost is ~3.7 MB of resident
memory, which the Pi will not notice.

**Why latency is sampled after the await.** ``Display.render`` hands the framebuffer write to
a thread (P8). Reading the clock before it returns would measure the dispatch and call it the
render, flattering the one number the project is graded on.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast

from avid.core.affect_map import baseline_affect
from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.faces import render_face
from avid.core.hal import DisplayFrame
from avid.core.ports import Clock, Display, EventBus
from avid.domain import Affect, AffectChanged, Event, StateTransitioned

_log = logging.getLogger(__name__)

# The component name, for logs. Unlike ``AffectService._SOURCE`` this never reaches an event
# envelope, because this service does not publish — it exists so a log line names its author.
_NAME = "ExpressionService"

_NS_PER_MS = 1_000_000


class ExpressionService:
    """Renders the current affect to the display, and says nothing about it.

    Shaped to SDS §9.2 (``name`` / ``start`` / ``stop`` / ``subscriptions``) — the same shape
    ``AffectService`` takes. No ``Service`` Protocol names that shape yet: AVID-73 wired both
    into the composition root and deliberately did **not** add one, because neither service
    owns a background task, so neither needs ``start``/``stop`` called at all. M4's
    ``AudioService`` will own a real stream loop, and that is the occasion to declare the
    Protocol and give ``lifecycle.run`` a ``services=`` parameter. Structural typing means
    both of these will satisfy it with no edit here.

    Depends on the :class:`~avid.core.ports.EventBus`, :class:`~avid.core.ports.Display` and
    :class:`~avid.core.ports.Clock` **Protocols**, never a concrete adapter (P2) — which is
    what lets the identical service drive a PNG-writing fake, an SPI panel and, if ADR-012's
    bet ever needs unwinding, an HDMI backend.
    """

    name = _NAME

    def __init__(self, *, bus: EventBus, display: Display, clock: Clock) -> None:
        self._bus = bus
        self._display = display
        self._clock = clock

        width, height = display.resolution
        # Iterating ``Affect`` rather than listing eight members means a ninth affect is
        # cached the day it is added — the alternative is a KeyError on the Pi at 1 a.m.
        self._faces: Mapping[Affect, DisplayFrame] = MappingProxyType(
            {
                affect: render_face(affect, width=width, height=height)
                for affect in Affect
            }
        )

        # AC-3's metrics. Plain attributes, not events: they are an operator's read of this
        # service's health, and publishing them would be exactly the "effect as fact" mistake
        # the module docstring rules out.
        self.last_latency_ms: float | None = None
        self.max_latency_ms: float = 0.0

        # The monotonic stamp of the newest event actually rendered — see ``_render`` for why
        # a service reading two queues needs one. Monotonic, never wall clock (SDS §9.1.1):
        # an NTP step would otherwise make every subsequent event look stale and freeze the
        # face permanently, which is a far worse failure than the one being prevented.
        self._last_rendered_ns: int | None = None
        # How many renders were skipped as superseded. Not an error — it is the guard working
        # — but a rising count means the two streams are racing more than expected.
        self.stale_skipped = 0

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """No owned tasks: purely reactive. The expensive work already happened in __init__.

        Deliberately *not* where the faces are composed. Precomputing in the constructor means
        a display whose resolution the renderer cannot satisfy fails while the composition root
        is still wiring, not once the robot is notionally live.
        """

    async def stop(self) -> None:
        """Idempotent and instant — there is nothing to unwind (§9.2's 5 s budget)."""

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare, do not register (SDS §9.2).

        Two subscriptions, and the second is the interesting one. ``affect.changed`` is the
        normal path. ``state.transitioned`` renders the Tier-1 baseline *directly*, so the
        face is right even if ``AffectService`` is absent, wedged, or has dropped an event —
        the operational state is a fact this service can read for itself, and a robot showing
        the wrong face is worse than one rendering the same face twice.

        Note both services subscribe to ``state.transitioned`` independently and neither knows
        the other exists. That is P5 working as intended, and the ``service-independence``
        contract (activated in AVID-73) is what proves it mechanically.

        DROP_OLDEST because the latest affect is the only one worth rendering (SDS §9.1.3): a
        backlog of stale faces is worse than no backlog.
        """
        return (
            Subscription(
                # Same bridge ``AsyncioEventBus.subscribe`` makes: a handler typed on its own
                # event is safe to store as the base ``Handler``, because dispatch is by exact
                # runtime type and only ever hands it that type.
                event_type=AffectChanged,
                handler=cast(Handler, self._on_affect_changed),
                name="ExpressionService.affect_changed",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=StateTransitioned,
                handler=cast(Handler, self._on_state_transitioned),
                name="ExpressionService.state_transitioned",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the cache -----------------------------------------------------------------------

    def face(self, affect: Affect) -> DisplayFrame:
        """The precomputed frame for *affect*. Read-only; the cache is fixed at construction."""
        return self._faces[affect]

    # --- rendering -----------------------------------------------------------------------

    async def _on_affect_changed(self, event: AffectChanged) -> None:
        await self._render(event.affect, event)

    async def _on_state_transitioned(self, event: StateTransitioned) -> None:
        await self._render(baseline_affect(event.to), event)

    async def _render(self, affect: Affect, event: Event) -> None:
        """Push the cached face and record how long the whole path took.

        There is deliberately **no** "same face as last time" guard here. It would look like a
        free optimisation and would quietly disable the independence above: on a state change
        this service renders the Tier-1 face, then ``AffectService``'s ``affect.changed``
        arrives moments later with the same face, and a suppression guard would make the
        second render a no-op — indistinguishable, in a test, from a service that never
        handled ``affect.changed`` at all. One redundant 460 KB memcpy per state change is a
        cheap price for a test that can still tell the two apart. The upstream no-op guard in
        ``AffectService._publish`` already removes the redundant events that actually matter.

        There **is** a staleness guard, which is a different thing. The bus dispatches
        subscribers concurrently and guarantees FIFO *per subscriber*, not across them (SDS
        §3.5). This service reads two queues, so their relative order is a race: an
        ``affect.changed`` minted before a ``state.transitioned`` can still be handled after
        it. Rendering last-writer-wins would then leave the robot wearing an affect the
        operational state has already moved past — and, because nothing further is published,
        wearing it until the *next* event, which may be a conversation away. Dropping an event
        older than the one already on glass is not an optimisation; it is the difference
        between a correct face and a stuck one.
        """
        if (
            self._last_rendered_ns is not None
            and event.monotonic_ns < self._last_rendered_ns
        ):
            self.stale_skipped += 1
            _log.debug(
                "skipped superseded %s frame [correlation_id=%s]",
                affect.name,
                event.correlation_id,
            )
            return

        await self._display.render(self._faces[affect])
        # Advanced only on a render that actually landed: a frame that raised was never shown,
        # so it must not suppress the retry that follows it.
        self._last_rendered_ns = event.monotonic_ns
        # Monotonic, never ``timestamp_ms`` (SDS §9.1.1). Wall clock steps — NTP corrects the
        # Pi's clock seconds after boot, which is exactly when the first faces render — and a
        # negative latency poisons the metric the project is graded on. Wall for humans,
        # monotonic for arithmetic.
        elapsed_ms = (self._clock.monotonic_ns() - event.monotonic_ns) / _NS_PER_MS
        self.last_latency_ms = elapsed_ms
        self.max_latency_ms = max(self.max_latency_ms, elapsed_ms)
        _log.debug(
            "rendered %s in %.1f ms [correlation_id=%s]",
            affect.name,
            elapsed_ms,
            event.correlation_id,
        )
