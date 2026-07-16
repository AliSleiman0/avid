"""The in-process async event bus — concurrent dispatch core (SDS §3.5, AVID-9).

A publisher announces that something *happened* and stops caring who reacts
(SDS §3.5.1). This module is that mechanism: :class:`AsyncioEventBus` routes each
published :class:`~avid.domain.Event` to every subscriber registered for its exact
type, dispatching handlers **concurrently** — one per-subscriber queue drained by
one per-subscriber worker task.

Deliberately *not* here yet, to keep this issue's blast radius small:

* **Failure isolation** — swallowing a raising handler and republishing it as
  ``system.handler_failed`` — is **AVID-10**. Until then, the worker below has no
  ``try``/``except``: a raising handler stops only its own subscriber; the
  publisher and every other subscriber are unaffected.
* **Backpressure** — a *bounded* queue (default 100) plus the ``DROP_OLDEST`` /
  ``DROP_NEWEST`` overflow policies (``BLOCK`` forbidden) — is also **AVID-10**.
  The queue here is unbounded, so ``put_nowait`` never overflows.
* The ``EventBus`` **Protocol** lives in ``core/ports.py`` as of **AVID-11**; this
  concrete class satisfies it structurally.

The bus never mints envelope fields: publishers construct their own events
(``event_id``/timestamps come from the ``Clock`` port later — AVID-11/AVID-14).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TypeVar, cast

from avid.domain import Event

E = TypeVar("E", bound=Event)

# A handler is an async function of one event. Stored on :class:`Subscription`
# in its base-`Event` form; `subscribe` bridges the generic call site to it.
Handler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Subscription:
    """A single subscriber's declaration: *this* handler wants *this* event type.

    Returned by :meth:`AsyncioEventBus.subscribe` and — once services exist
    (SDS §9.2) — what ``Service.subscriptions()`` declares for the composition
    root to register. ``name`` is mandatory so the §9.1.5 drift check can see the
    subscriber (an anonymous lambda would be invisible to it).

    AVID-10 will add ``policy`` and ``maxsize`` for backpressure.
    """

    event_type: type[Event]
    handler: Handler
    name: str


@dataclass(slots=True)
class _Runner:
    """Bus-internal runtime state for one :class:`Subscription`: its inbox queue
    and the worker task draining it. Not part of the public contract."""

    subscription: Subscription
    queue: asyncio.Queue[Event]
    task: asyncio.Task[None] | None = None


class AsyncioEventBus:
    """In-process, asyncio-based event bus (ADR-001).

    Lifecycle: register every subscriber with :meth:`subscribe` at composition
    time, then :meth:`start` (spawns the workers and freezes the subscriber
    graph), then :meth:`publish` freely, then :meth:`stop`. Also usable as an
    ``async with`` block. Subscription is **static** — subscribing after
    :meth:`start` raises, which is what keeps the subscriber graph knowable for
    the §9.1.5 drift check (SDS §3.5.2).
    """

    def __init__(self) -> None:
        # The declaration graph: event type -> its subscriptions. Walked by the
        # §9.1.5 drift generator (later); the source of truth for who listens.
        self._subs: dict[type[Event], list[Subscription]] = {}
        # Built at start(): event type -> its runners, for O(1) dispatch.
        self._dispatch: dict[type[Event], list[_Runner]] = {}
        self._runners: list[_Runner] = []
        self._started = False
        self._stopped = False

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
    ) -> Subscription:
        """Register *handler* for *event_type*. ``name`` is mandatory (SDS §9.1.5).

        Raises :class:`RuntimeError` if called after :meth:`start` (subscription
        is static, SDS §3.5.2) and :class:`ValueError` if ``name`` is empty.
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
        subscription = Subscription(
            event_type=event_type,
            # A `Callable[[E], ...]` handler is safe to invoke with an instance of
            # its own `event_type`; storing it as the base `Callable[[Event], ...]`
            # bridges that invariance. Dispatch only ever passes it matching events.
            handler=cast(Handler, handler),
            name=name,
        )
        self._subs.setdefault(event_type, []).append(subscription)
        return subscription

    async def start(self) -> None:
        """Freeze the subscriber graph and spawn one worker task per subscription.

        Must run inside the event loop that will carry the workers.
        """
        if self._started:
            raise RuntimeError("event bus already started")
        self._started = True
        for event_type, subscriptions in self._subs.items():
            runners: list[_Runner] = []
            for subscription in subscriptions:
                queue: asyncio.Queue[Event] = asyncio.Queue()
                runner = _Runner(subscription=subscription, queue=queue)
                runner.task = asyncio.create_task(
                    self._worker(runner), name=f"eventbus:{subscription.name}"
                )
                runners.append(runner)
                self._runners.append(runner)
            self._dispatch[event_type] = runners

    async def publish(self, event: Event) -> None:
        """Fire-and-forget: enqueue *event* for each matching subscriber, return.

        Returns once the event is **queued, not handled** (SDS §3.5.2): the
        display renderer must never be in the latency path of speech. Dispatch is
        by the event's *exact* runtime type — no subclass fan-out — so the
        subscriber graph stays explicit (SDS §9.1.5).
        """
        if not self._started:
            raise RuntimeError("cannot publish() before start()")
        runners = self._dispatch.get(type(event))
        if not runners:
            return
        for runner in runners:
            # Unbounded queue in AVID-9: put_nowait cannot overflow. AVID-10 bounds
            # it and applies the per-subscriber overflow policy here.
            runner.queue.put_nowait(event)

    async def _worker(self, runner: _Runner) -> None:
        """Drain one subscriber's queue in FIFO order, awaiting its handler.

        One worker per subscriber gives concurrency *across* subscribers and
        ordered-per-publisher delivery *within* one (SDS §3.5.4). No ``try`` here:
        failure isolation is AVID-10 (see module docstring).
        """
        queue = runner.queue
        handler = runner.subscription.handler
        while True:
            event = await queue.get()
            await handler(event)

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
