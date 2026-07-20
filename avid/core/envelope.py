"""Event-envelope stamping — the one place the five base fields are sampled (SDS §9.1.1).

Every publisher needs an :class:`~avid.domain.Event` envelope, and every publisher needs it
stamped the *same* way. Keeping this in one function is what stops a second copy drifting —
specifically on the rule that costs the project its headline metric if it's got wrong: **wall
clock for humans, monotonic for arithmetic** (SDS §9.1.1). ``timestamp_ms`` is for logs and
never subtracted; latency math uses ``monotonic_ns``, because an NTP step or the Pi's boot-time
clock correction can walk the wall clock backwards and yield negative latencies.

*correlation_id* is **propagated, not minted**, by everything downstream of a turn's origin
(SDS §3.12.2): pass the id of whatever caused this event so one grep reconstructs the turn.
"""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID, uuid4

from avid.core.ports import Clock


class Envelope(TypedDict):
    """The five base :class:`~avid.domain.Event` fields, sampled per publish."""

    event_id: UUID
    correlation_id: UUID
    timestamp_ms: int
    monotonic_ns: int
    source: str


def envelope(*, clock: Clock, correlation_id: UUID, source: str) -> Envelope:
    """Stamp a fresh event envelope from *clock* (SDS §9.1.1).

    *source* is the publishing component's name as the §9.1.3 catalog spells it
    (``"Lifecycle"``, ``"StateManager"``, …) — it is the field an operator reads to know who
    published a fact. ``now()`` is epoch seconds, so milliseconds is ``* 1000``.
    """
    return Envelope(
        event_id=uuid4(),
        correlation_id=correlation_id,
        timestamp_ms=clock.now() * 1000,
        monotonic_ns=clock.monotonic_ns(),
        source=source,
    )
