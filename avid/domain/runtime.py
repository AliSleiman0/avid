"""Runtime facts about a *process*, rather than about a conversation (#379, SDS §12.6).

Every other value in ``domain/`` describes what the robot experienced. This one describes what the
robot *was*: when it started, whether it is still running, and how it stopped. That is the
vocabulary O5 is written in — *"30-day soak, ≥99% uptime, zero manual restarts"* — and none of it
was representable before this module.

Pure, like the rest of the layer: no clock, no I/O, no storage opinion. The adapter behind
:class:`~avid.core.ports.BootLog` persists these; :func:`uptime_ratio` is what turns a sequence of
them into the number the M11 gate grades.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# How a run ended, from the *process's* point of view.
#
# ⚠️ There are exactly two values and the distinction is the one O5 turns on. A clean stop means
# the ordered teardown ran — SIGTERM reached the signal handler, services stopped, the record was
# closed. That only happens when a person (or a deploy) asked. Anything else — a crash, a watchdog
# kill, a power cut — never reaches that code, so the record simply stays open, and an *open*
# record on a process that is no longer running is exactly what "unplanned" means.
#
# ⚠️ The mechanism cannot separate a deploy from an operator `systemctl stop`: both are SIGTERM,
# both are deliberate, and nothing in the signal distinguishes them (SDS §12.6 states this limit
# rather than papering over it). #389 logs interventions independently.
StopReason = Literal["signal"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BootRecord:
    """One run of the process, from start to however it ended.

    ``started_at`` / ``last_seen_at`` / ``stopped_at`` are **epoch seconds, wall clock** — for
    humans and for spanning a restart, which is the one thing a monotonic clock cannot do
    (§9.1.1's rule is about *arithmetic within* a process; across a boot there is no shared
    monotonic origin at all).

    ``started_mono`` is this process's own monotonic reading at start, and it exists so that a
    **clock step is detectable after the fact**. The Pi has no RTC: an offline boot restores a
    stale wall clock and NTP steps it forward later, which is how AVID-345's scheduler slept
    ~34,700 s through its own booking. Holding both means the pair can be compared instead of
    trusted.
    """

    boot_id: str
    build: str
    started_at: int
    started_mono: int
    # Refreshed on a heartbeat. On a run that ended without warning this is the last moment the
    # process is *known* to have been alive — so downtime is bounded by the heartbeat interval
    # rather than guessed, which is why the interval is reported alongside any uptime figure.
    last_seen_at: int
    # ``None`` means the process never got to say goodbye: a crash, a watchdog kill, or a power
    # cut. Not an error condition in itself — but it is what makes a restart *unplanned*.
    stopped_at: int | None = None
    stop_reason: StopReason | None = None

    @property
    def was_clean(self) -> bool:
        """Did this run end through the ordered teardown? See :data:`StopReason`."""
        return self.stopped_at is not None

    @property
    def ended_at(self) -> int:
        """When this run is known to have still been alive — its stop, or its last heartbeat.

        The conservative reading, deliberately: crediting an unclean run with uptime up to the
        *next* boot would count the downtime as uptime, which is the direction an availability
        metric must never be wrong in.
        """
        return self.stopped_at if self.stopped_at is not None else self.last_seen_at


def uptime_ratio(
    records: Sequence[BootRecord], *, window_start: int, window_end: int
) -> float:
    """Fraction of ``[window_start, window_end)`` in which a run was alive (§12.6, O5).

    Sums each record's overlap with the window and divides by the window's length. Overlapping
    records cannot happen — one process at a time — but a record that starts before the window or
    ends after it is ordinary, so both ends are clipped rather than assumed.

    ⚠️ **A SLEEPING robot is UP.** SLEEPING is an operational state the design intends (§3.10,
    M8's nap), not an absence; this function counts process liveness and nothing else, so the nap
    cannot be miscounted as downtime.

    ⚠️ **The result is only as precise as the heartbeat.** An unclean run is credited to its last
    heartbeat, so up to one interval of genuine uptime is discarded. That bias is deliberate and
    one-directional: this number may understate availability, never overstate it.

    Returns 0.0 for an empty or non-positive window rather than raising — a gate that crashes
    while reporting is worse than one that reports a floor.
    """
    span = window_end - window_start
    if span <= 0:
        return 0.0
    alive = 0
    for record in records:
        start = max(record.started_at, window_start)
        end = min(record.ended_at, window_end)
        if end > start:
            alive += end - start
    return alive / span


__all__ = ["BootRecord", "StopReason", "uptime_ratio"]
