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
import logging
import signal
from collections.abc import Mapping
from typing import TypedDict
from uuid import UUID, uuid4

from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock
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


async def run(
    *,
    bus: AsyncioEventBus,
    clock: Clock,
    adapter_health: Mapping[str, bool],
    shutdown: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
) -> int:
    """Run the robot until a shutdown signal, then exit cleanly.

    Starts *bus*, publishes ``system.started`` (with the *adapter_health* snapshot),
    reaches ``IDLE`` via the domain transition, waits for SIGTERM/SIGINT (or *shutdown*
    being set — the test seam), publishes ``system.shutting_down``, and returns ``0``.

    *shutdown* and *ready* are injectable for tests: a test sets *shutdown* to stop the
    loop deterministically and awaits *ready* to know IDLE was reached. In production
    both are created here and *shutdown* is driven by the signal handlers.
    """
    shutdown = shutdown or asyncio.Event()
    _install_signal_handlers(shutdown)

    boot_id = uuid4()
    async with bus:
        await bus.publish(
            SystemStarted(
                **_envelope(clock=clock, correlation_id=boot_id),
                adapters=dict(adapter_health),
            )
        )
        state = next_state(RobotState.BOOTING, Trigger.SYSTEM_STARTED)
        _log.info("reached %s [correlation_id=%s]", state.name, boot_id)
        if ready is not None:
            ready.set()

        await shutdown.wait()

        shutdown_id = uuid4()
        _log.info("shutting down [correlation_id=%s]", shutdown_id)
        await bus.publish(
            SystemShuttingDown(
                **_envelope(clock=clock, correlation_id=shutdown_id), reason="signal"
            )
        )

    return 0
