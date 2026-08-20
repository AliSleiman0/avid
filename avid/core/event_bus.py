"""The in-process async event bus — concurrent dispatch, failure isolation, and
backpressure (SDS §3.5, AVID-9 + AVID-10).

A publisher announces that something *happened* and stops caring who reacts
(SDS §3.5.1). This module is that mechanism: :class:`AsyncioEventBus` routes each
published :class:`~avid.domain.Event` to every subscriber registered for its exact
type, dispatching handlers **concurrently** — one per-subscriber bounded queue
drained by one per-subscriber worker task.

Two reliability properties live here (SDS §3.5.2, §3.5.5):

* **Failure isolation.** A handler that raises is logged, swallowed, and republished
  as ``system.handler_failed`` (§3.5.2). A crashing display renderer must not kill a
  conversation — the single most important reliability property in the system. The
  worker survives its handler and keeps draining.
* **Backpressure.** Each subscriber's queue is bounded (default 100). On overflow the
  per-subscriber :class:`OverflowPolicy` decides which event to drop; the drop is a
  *loud* ``system.handler_failed`` (``reason="queue_overflow"``) plus a counter, never
  a silent loss (§3.5.5). ``BLOCK`` is forbidden — it would push backpressure into the
  audio path — and is rejected at registration.

``system.handler_failed`` is the bus's only self-referential event. A handler *of* it
that fails must not spawn another — that is an infinite loop, guarded explicitly in
:meth:`AsyncioEventBus._emit_handler_failed`.

The ``EventBus`` **Protocol** and the real ``Clock`` port now live in ``core/ports.py``
(**AVID-11**), and the ``Clock`` adapters ``SystemClock``/``FakeClock`` in
``adapters/clock.py`` (**AVID-12**). The bus stamps the ``system.handler_failed`` events
it builds via an injected time source: :class:`_TimeSource`, a deliberately *minimal*
internal Protocol (``now`` + ``monotonic_ns`` only — the bus never sleeps), defaulting to
:class:`_SystemClock`. It stays minimal on purpose (interface segregation) and cannot be
replaced by the ``Clock`` port itself, because ``core`` may not import ``adapters`` (P1);
the real ``Clock`` adapters satisfy :class:`_TimeSource` structurally, so the composition
root injects one here unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Protocol, TypeVar, cast
from uuid import uuid4

from avid.domain import (
    REASON_HANDLER_RAISED,
    REASON_QUEUE_OVERFLOW,
    Event,
    SystemHandlerFailed,
)

_log = logging.getLogger(__name__)

E = TypeVar("E", bound=Event)

# A handler is an async function of one event. Stored on :class:`Subscription`
# in its base-`Event` form; `subscribe` bridges the generic call site to it.
Handler = Callable[[Event], Awaitable[None]]

# Sensible defaults for a subscriber that does not state otherwise (SDS §3.5.5).
DEFAULT_MAXSIZE = 100


class OverflowPolicy(Enum):
    """What a subscriber's bounded queue does when it is full (SDS §3.5.5).

    ``BLOCK`` is a member so it can be *named and rejected*: blocking would propagate
    backpressure into the audio path, the one place §2.8.1 has no slack, so
    :meth:`AsyncioEventBus.subscribe` raises on it.
    """

    DROP_OLDEST = "drop_oldest"  # latest wins; stale frames are worthless
    DROP_NEWEST = "drop_newest"  # preserve the beginning of an incident
    BLOCK = "block"  # forbidden — rejected at registration


class _TimeSource(Protocol):
    """The minimum the bus needs to stamp the ``system.handler_failed`` events it
    builds: wall time (for humans) and monotonic time (for latency math).

    Private and structural on purpose, and kept minimal by design: the ``Clock`` port
    (SDS §9.3) adds ``sleep``, but the bus never sleeps, so depending on the full port
    here would over-couple it (interface segregation). ``Clock``'s adapters —
    :class:`~avid.adapters.clock.SystemClock` and
    :class:`~avid.adapters.clock.FakeClock` (AVID-12) — expose ``now``/``monotonic_ns``,
    so each satisfies this protocol structurally and the composition root injects one via
    ``clock=`` unchanged.
    """

    def now(self) -> int: ...  # epoch seconds

    def monotonic_ns(self) -> int: ...


class _SystemClock:
    """Real-time default for :class:`_TimeSource`. Named ``_SystemClock`` (not
    ``Real*``) so it stays clear of the P3 composition-root grep; the composition root
    (AVID-14) will inject the real :class:`~avid.adapters.clock.SystemClock` here."""

    def now(self) -> int:
        return int(time.time())

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(frozen=True, slots=True, kw_only=True)
class Subscription:
    """A single subscriber's declaration: *this* handler wants *this* event type,
    with *this* backpressure behaviour.

    Returned by :meth:`AsyncioEventBus.subscribe` and — once services exist
    (SDS §9.2) — what ``Service.subscriptions()`` declares for the composition
    root to register. ``name`` is mandatory so the §9.1.5 drift check can see the
    subscriber (an anonymous lambda would be invisible to it).
    """

    event_type: type[Event]
    handler: Handler
    name: str
    policy: OverflowPolicy
    maxsize: int


@dataclass(slots=True)
class _Runner:
    """Bus-internal runtime state for one :class:`Subscription`: its inbox queue, the
    worker task draining it, and how many events its queue has dropped. Not part of the
    public contract."""

    subscription: Subscription
    queue: asyncio.Queue[Event]
    task: asyncio.Task[None] | None = None
    overflow_count: int = 0


class AsyncioEventBus:
    """In-process, asyncio-based event bus (ADR-001).

    Lifecycle: register every subscriber with :meth:`subscribe` at composition
    time, then :meth:`start` (spawns the workers and freezes the subscriber
    graph), then :meth:`publish` freely, then :meth:`stop`. Also usable as an
    ``async with`` block. Subscription is **static** — subscribing after
    :meth:`start` raises, which is what keeps the subscriber graph knowable for
    the §9.1.5 drift check (SDS §3.5.2).
    """

    def __init__(self, *, clock: _TimeSource | None = None) -> None:
        # The declaration graph: event type -> its subscriptions. Walked by the
        # §9.1.5 drift generator (later); the source of truth for who listens.
        self._subs: dict[type[Event], list[Subscription]] = {}
        # Built at start(): event type -> its runners, for O(1) dispatch.
        self._dispatch: dict[type[Event], list[_Runner]] = {}
        self._runners: list[_Runner] = []
        # Stamps the handler_failed events the bus itself builds. The composition root
        # (AVID-14) injects the real SystemClock here; the default reads the system clock.
        self._clock: _TimeSource = clock or _SystemClock()
        self._started = False
        self._stopped = False

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        maxsize: int = DEFAULT_MAXSIZE,
    ) -> Subscription:
        """Register *handler* for *event_type*. ``name`` is mandatory (SDS §9.1.5).

        *policy* and *maxsize* set this subscriber's backpressure (SDS §3.5.5).

        Raises :class:`RuntimeError` if called after :meth:`start` (subscription is
        static, SDS §3.5.2); :class:`ValueError` if ``name`` is empty, ``maxsize`` is
        not positive, or ``policy`` is :attr:`OverflowPolicy.BLOCK` (forbidden).
        """
        if self._started:
            raise RuntimeError(
                "subscription is static: subscribe() is not allowed after start() "
                "(SDS §3.5.2 — a runtime-mutable subscriber graph forfeits the "
                "§9.1.5 drift check)"
            )
        if not name:
            raise ValueError(
                "subscribe(name=...) must be a non-empty string: the drift check "
                "identifies subscribers by name (SDS §9.1.5)"
            )
        if policy is OverflowPolicy.BLOCK:
            raise ValueError(
                "OverflowPolicy.BLOCK is forbidden (SDS §3.5.5): blocking would "
                "propagate backpressure into the audio path. Use DROP_OLDEST or "
                "DROP_NEWEST."
            )
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        subscription = Subscription(
            event_type=event_type,
            # A `Callable[[E], ...]` handler is safe to invoke with an instance of
            # its own `event_type`; storing it as the base `Callable[[Event], ...]`
            # bridges that invariance. Dispatch only ever passes it matching events.
            handler=cast(Handler, handler),
            name=name,
            policy=policy,
            maxsize=maxsize,
        )
        self._subs.setdefault(event_type, []).append(subscription)
        return subscription

    async def start(self) -> None:
        """Freeze the subscriber graph and spawn one worker task per subscription.

        Each worker gets a **bounded** queue (``maxsize`` per subscriber, SDS §3.5.5).
        Must run inside the event loop that will carry the workers.
        """
        if self._started:
            raise RuntimeError("event bus already started")
        self._started = True
        for event_type, subscriptions in self._subs.items():
            runners: list[_Runner] = []
            for subscription in subscriptions:
                queue: asyncio.Queue[Event] = asyncio.Queue(
                    maxsize=subscription.maxsize
                )
                runner = _Runner(subscription=subscription, queue=queue)
                runner.task = asyncio.create_task(
                    self._worker(runner), name=f"eventbus:{subscription.name}"
                )
                runners.append(runner)
                self._runners.append(runner)
            self._dispatch[event_type] = runners

    async def publish(self, event: Event) -> None:
        """Fire-and-forget: enqueue *event* for each matching subscriber, return.

        Returns once the event is **queued, not handled** (SDS §3.5.2): the display
        renderer must never be in the latency path of speech. Dispatch is by the
        event's *exact* runtime type — no subclass fan-out — so the subscriber graph
        stays explicit (SDS §9.1.5).
        """
        if not self._started:
            raise RuntimeError("cannot publish() before start()")
        self._enqueue(event)

    def _enqueue(self, event: Event) -> None:
        """Put *event* onto each matching subscriber's bounded queue, applying that
        subscriber's overflow policy. Synchronous — runs atomically w.r.t. the workers
        in the single-threaded loop, so ``get_nowait``/``put_nowait`` never race.

        Shared by :meth:`publish` and :meth:`_emit_handler_failed`; the latter is why
        this is factored out of ``publish`` (it must not re-run the started-check).
        """
        for runner in self._dispatch.get(type(event), ()):
            try:
                runner.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._overflow(runner, event)

    def _overflow(self, runner: _Runner, event: Event) -> None:
        """Handle a full queue for *runner* per its policy, then report the drop
        loudly (SDS §3.5.5): bump the counter and republish ``system.handler_failed``
        with ``reason="queue_overflow"``. Silent drops are a debugging catastrophe."""
        if runner.subscription.policy is OverflowPolicy.DROP_OLDEST:
            # Evict the stale head to make room for the fresh event (latest wins).
            runner.queue.get_nowait()
            runner.queue.put_nowait(event)
        # DROP_NEWEST: the incoming event is simply discarded (queue unchanged).
        runner.overflow_count += 1
        _log.warning(
            "event bus: queue overflow for subscriber %r on %s (corr=%s), policy=%s; "
            "dropped one event (overflow_count=%d)",
            runner.subscription.name,
            type(event).__name__,
            event.correlation_id,
            runner.subscription.policy.name,
            runner.overflow_count,
        )
        self._emit_handler_failed(runner, event, REASON_QUEUE_OVERFLOW, exc=None)

    async def _worker(self, runner: _Runner) -> None:
        """Drain one subscriber's queue in FIFO order, awaiting its handler.

        One worker per subscriber gives concurrency *across* subscribers and
        ordered-per-publisher delivery *within* one (SDS §3.5.4). A raising handler is
        logged, swallowed, and republished as ``system.handler_failed`` (SDS §3.5.2);
        the worker keeps draining. ``CancelledError`` (from :meth:`stop`) is *not*
        caught by ``except Exception``, so it still ends the task cleanly.
        """
        queue = runner.queue
        handler = runner.subscription.handler
        while True:
            event = await queue.get()
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001 — isolation is the whole point (§3.5.2)
                _log.warning(
                    "event bus: subscriber %r failed handling %s (corr=%s): %r",
                    runner.subscription.name,
                    type(event).__name__,
                    event.correlation_id,
                    exc,
                    exc_info=exc,
                )
                self._emit_handler_failed(
                    runner, event, REASON_HANDLER_RAISED, exc=repr(exc)
                )

    def _emit_handler_failed(
        self, runner: _Runner, failed: Event, reason: str, *, exc: str | None
    ) -> None:
        """Republish a subscriber's failure as ``system.handler_failed`` (SDS §9.1.3).

        **The loop guard:** if *runner* is itself a ``system.handler_failed``
        subscriber, we log and return without emitting — publishing another would be an
        infinite loop (SDS §9.1.3). Otherwise the bus mints the event (its own
        ``event_id``/timestamps via the injected clock), propagating the failed event's
        ``correlation_id`` so the failure stays pinned to its turn.
        """
        if runner.subscription.event_type is SystemHandlerFailed:
            _log.error(
                "event bus: the system.handler_failed subscriber %r itself failed "
                "(reason=%s); not re-emitting to avoid an infinite loop",
                runner.subscription.name,
                reason,
            )
            return
        self._enqueue(
            SystemHandlerFailed(
                event_id=uuid4(),
                correlation_id=failed.correlation_id,
                timestamp_ms=self._clock.now() * 1000,
                monotonic_ns=self._clock.monotonic_ns(),
                source="EventBus",
                handler=runner.subscription.name,
                event_type=type(failed).__name__,
                reason=reason,
                exc=exc,
            )
        )

    def queue_stats(self) -> dict[str, dict[str, int]]:
        """Per-subscriber inbox depth, capacity and drop count (#380, SDS §3.12.2).

        The public read over what :class:`_Runner` already tracks. Exposed because **overflow is
        the failure a soak exists to surface**: §3.5's rule is that silent drops are a debugging
        catastrophe and loud drops are a tuning signal, and until now the "loud" half was one log
        line per drop — visible while someone was watching, invisible over thirty days.

        ``depth`` is a live reading and will usually be 0; the number that matters afterwards is
        ``dropped``, which is monotonic for the process's life. Keyed by subscriber ``name``,
        which :meth:`subscribe` makes mandatory precisely so every subscriber is nameable here.

        Cheap and synchronous — ``qsize()`` is an attribute read — so it is safe to call inline
        from the metrics snapshot while the robot is mid-turn (P8).

        ⚠️ Built from the **declaration graph**, not from the runners, so a bus that has not
        started yet reports its subscribers with zero depth rather than reporting nothing. The
        difference matters for the same reason ``MetricsRegistry`` separates absent from zero: an
        empty mapping would read as "this robot has no subscribers", which is a very different
        claim from "the workers are not up yet" and is the more alarming of the two to read at
        thirty days.
        """
        runners = {r.subscription.name: r for r in self._runners}
        stats: dict[str, dict[str, int]] = {}
        for subscriptions in self._subs.values():
            for subscription in subscriptions:
                runner = runners.get(subscription.name)
                stats[subscription.name] = {
                    "depth": runner.queue.qsize() if runner is not None else 0,
                    "maxsize": subscription.maxsize,
                    "dropped": runner.overflow_count if runner is not None else 0,
                }
        return stats

    async def stop(self) -> None:
        """Cancel every worker and await it. Idempotent."""
        if not self._started or self._stopped:
            return
        self._stopped = True
        tasks = [runner.task for runner in self._runners if runner.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> AsyncioEventBus:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()
