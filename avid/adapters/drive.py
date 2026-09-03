"""Drive adapters — the fake that *is* the simulator, and (in the next PR) the real L9110S (#400).

Implementations of the :class:`~avid.core.ports.Drive` port, behind one contract suite (P6,
SDS §14.4). The port makes three promises — **signed speed in the robot frame, milliseconds
actually driven, and an instant idempotent stop** — and every adapter keeps them identically
(SDS §3.9.5):

* :class:`FakeDrive` records an assertable trace and integrates an **odometer** from the
  commanded legs, so a behaviour test asserts *"the robot ended up where it started"* rather
  than mocking a call. It *is* the simulator: stdlib only, no motors, same contract.

``odometer_mm`` and ``is_running`` are adapter-level introspection, deliberately **off** the
``Drive`` port — ``DriveService`` keeps its own odometer from the milliseconds :meth:`run`
returns and never reads the adapter's back, so they are not an application need (they are the
contract suite's observation points, exactly as ``FakeServo.position`` is). The two odometers
agreeing is what a service test asserts; the fake's is the one that would catch a service that
lied about what it commanded. Constructed only by the composition root or a test fixture (P3).
"""

from __future__ import annotations

import asyncio

from avid.core.hal import DriveCapabilities

# Update cadence for a run. ~20 ms/slice matches the servo adapters and the edge sensor's
# default poll: fine enough that a stop lands within one poll of the sensor that asked for
# it, and coarse enough that a 300 ms leg is ~15 awaited slices, each a chance to observe a
# cancel or a stop.
_STEP_MS = 20


def _clamp(speed: float) -> float:
    """Clamp a signed wheel speed to ``[-1, 1]`` — the adapter's job, like a servo's reach."""
    return max(-1.0, min(1.0, speed))


def _slices(duration_ms: int) -> int:
    """Number of awaited slices for a run of *duration_ms* (at least one)."""
    return max(1, duration_ms // _STEP_MS)


class FakeDrive:
    """The :class:`~avid.core.ports.Drive` fake (P6): a trace, an odometer, no motors.

    ``capabilities`` is injected (P7); ``None`` models a rig with no wheels — the port still
    answers, the planner plans nothing, and :meth:`run` records the call it should never
    receive. The public :attr:`runs` list is the assertable trace — one ``(left, right,
    driven_ms)`` entry per completed, stopped or cancelled run — and :attr:`odometer_mm`
    integrates ``mean(left, right) × mm_per_s_at_full`` over the slices that actually ran, so
    a stopped run advances it only as far as it got. :meth:`run` sleeps between slices, so a
    preempting cancel genuinely stops it partway and the trace is left partial.
    """

    def __init__(self, *, capabilities: DriveCapabilities | None) -> None:
        self._capabilities = capabilities
        self._running = False
        self._stop_requested = False
        # The assertable record of every run, in order: (left, right, ms actually driven).
        self.runs: list[tuple[float, float, int]] = []
        # Position along the robot's forward axis, mm, integrated from what was driven.
        self.odometer_mm = 0.0
        self.stops = 0

    @property
    def capabilities(self) -> DriveCapabilities | None:
        """What these wheels can do, for negotiation (SDS §3.9.3 / ADR-015)."""
        return self._capabilities

    async def run(self, left: float, right: float, *, duration_ms: int) -> int:
        """Drive at the clamped speeds for *duration_ms*, in awaited slices.

        Returns the milliseconds actually driven: the whole of *duration_ms* if the run
        completes, less if :meth:`stop` was called or the task was cancelled mid-run. The
        odometer advances slice by slice, so it reflects exactly what was driven. The trace
        entry is appended in ``finally`` so a cancelled run is recorded as partial rather than
        vanishing.
        """
        left, right = _clamp(left), _clamp(right)
        n = _slices(duration_ms)
        slice_ms = duration_ms / n
        driven_ms = 0.0
        self._running = True
        self._stop_requested = False
        try:
            for _ in range(n):
                if self._stop_requested:
                    break
                await asyncio.sleep(slice_ms / 1000)
                driven_ms += slice_ms
                if self._capabilities is not None:
                    self.odometer_mm += (
                        (left + right)
                        / 2
                        * self._capabilities.mm_per_s_at_full
                        * slice_ms
                        / 1000
                    )
        finally:
            self._running = False
            self.runs.append((left, right, round(driven_ms)))
        return round(driven_ms)

    async def stop(self) -> None:
        """Cut the motors now (SDS §9.1.4): a run in flight ends at its next slice and
        returns what it drove. Idempotent — counting is the only side effect when idle."""
        self.stops += 1
        self._stop_requested = True

    @property
    def is_running(self) -> bool:
        """Whether a run is in flight (introspection, off the port)."""
        return self._running
