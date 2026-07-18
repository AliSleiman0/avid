"""Process lifecycle — boot to IDLE, run, shut down cleanly (SDS §3.11.3, §9.1.3).

The composition root (``avid/main.py``) builds the adapters and the bus, then hands
them here. This module owns the run loop: it starts the bus, publishes
``system.started`` (which drives ``BOOTING -> IDLE``), waits for a shutdown signal,
publishes ``system.shutting_down``, and returns an exit code. It never constructs an
adapter (P3) and never imports ``avid.adapters`` (P1/P5) — it depends only on the bus,
the ``Clock`` port, and the domain.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Mapping
from typing import Protocol, TypedDict
from uuid import UUID, uuid4

from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock, ServiceNotifier
from avid.domain import (
    RobotState,
    SystemShuttingDown,
    SystemStarted,
    Trigger,
    next_state,
)

_log = logging.getLogger("avid.lifecycle")

# The component name stamped on the events this module publishes (SDS §9.1.3).
_SOURCE = "Lifecycle"


class ControlSurface(Protocol):
    """A startable/stoppable inbound adapter the run loop owns (SDS §9.5).

    The local control API (``/health``) is the M1 instance. Structural, so the loop
    starts and stops it without importing ``avid.adapters`` (P1/P5): the composition
    root injects the concrete server, the loop only needs ``start``/``stop``. ``stop``
    must return within systemd's 5 s budget (SDS §9.2)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _Envelope(TypedDict):
    """The five base :class:`~avid.domain.Event` fields, sampled per publish."""

    event_id: UUID
    correlation_id: UUID
    timestamp_ms: int
    monotonic_ns: int
    source: str


def _envelope(*, clock: Clock, correlation_id: UUID) -> _Envelope:
    """Stamp a fresh event envelope from *clock* (SDS §9.1.1).

    Wall clock for humans, monotonic for arithmetic — ``now()`` is epoch seconds, so
    milliseconds is ``* 1000``.
    """
    return _Envelope(
        event_id=uuid4(),
        correlation_id=correlation_id,
        timestamp_ms=clock.now() * 1000,
        monotonic_ns=clock.monotonic_ns(),
        source=_SOURCE,
    )


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Wire SIGTERM/SIGINT to set *shutdown* (SDS §3.11.3: systemd stops via SIGTERM).

    Uses the loop's signal handling on POSIX. Windows' Proactor loop has no
    ``add_signal_handler`` (raises ``NotImplementedError``), so we fall back to the
    C-level handler, which hops back onto the loop thread to set the event.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:  # pragma: no cover - Windows-only fallback

            def _handler(signum: int, frame: object) -> None:
                loop.call_soon_threadsafe(shutdown.set)

            signal.signal(sig, _handler)


async def _ping_watchdog(
    *, notifier: ServiceNotifier, clock: Clock, interval_s: float
) -> None:
    """Ping ``WATCHDOG=1`` every *interval_s* until cancelled (SDS §3.11.3).

    Sleeps on the injected *clock* — so a test drives the interval with ``FakeClock``
    instead of waiting real seconds. The interval must be shorter than the unit's
    ``WatchdogSec`` (config keeps it at half): a loop that stops pinging is a loop
    systemd restarts, which is the entire point of ``Type=notify``."""
    while True:
        await clock.sleep(interval_s)
        await notifier.watchdog()


async def run(
    *,
    bus: AsyncioEventBus,
    clock: Clock,
    adapter_health: Mapping[str, bool],
    notifier: ServiceNotifier,
    health: ControlSurface | None = None,
    watchdog_interval_s: float = 15.0,
    shutdown: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
) -> int:
    """Run the robot until a shutdown signal, then exit cleanly.

    Starts *bus* and the *health* control surface, publishes ``system.started`` (with
    the *adapter_health* snapshot), reaches ``IDLE`` via the domain transition,
    announces ``READY=1`` through *notifier*, then pings ``WATCHDOG=1`` every
    *watchdog_interval_s* while it waits for SIGTERM/SIGINT (or *shutdown* being set —
    the test seam). On shutdown it announces ``STOPPING=1``, publishes
    ``system.shutting_down``, stops the health surface, and returns ``0``.

    *shutdown* and *ready* are injectable for tests: a test sets *shutdown* to stop the
    loop deterministically and awaits *ready* to know IDLE was reached. In production
    both are created here and *shutdown* is driven by the signal handlers. A
    non-positive *watchdog_interval_s* disables the ping task (the notifier still gets
    ready/stopping) — the laptop profile, where nothing is watching.
    """
    shutdown = shutdown or asyncio.Event()
    _install_signal_handlers(shutdown)

    boot_id = uuid4()
    async with bus:
        if health is not None:
            await health.start()
        try:
            await bus.publish(
                SystemStarted(
                    **_envelope(clock=clock, correlation_id=boot_id),
                    adapters=dict(adapter_health),
                )
            )
            state = next_state(RobotState.BOOTING, Trigger.SYSTEM_STARTED)
            _log.info("reached %s [correlation_id=%s]", state.name, boot_id)
            await notifier.ready()
            if ready is not None:
                ready.set()

            watchdog_task: asyncio.Task[None] | None = None
            if watchdog_interval_s > 0:
                watchdog_task = asyncio.create_task(
                    _ping_watchdog(
                        notifier=notifier,
                        clock=clock,
                        interval_s=watchdog_interval_s,
                    ),
                    name="watchdog-pinger",
                )
            try:
                await shutdown.wait()
            finally:
                # Ordered teardown: stop pinging, tell the supervisor this is a
                # deliberate stop (not a crash), then announce the shutdown fact.
                if watchdog_task is not None:
                    watchdog_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watchdog_task
                await notifier.stopping()
                shutdown_id = uuid4()
                _log.info("shutting down [correlation_id=%s]", shutdown_id)
                await bus.publish(
                    SystemShuttingDown(
                        **_envelope(clock=clock, correlation_id=shutdown_id),
                        reason="signal",
                    )
                )
        finally:
            if health is not None:
                await health.stop()

    return 0
