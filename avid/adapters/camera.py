"""Camera adapters — the fake that *is* the simulator, and the real Pi camera (AVID-51).

Two implementations of the :class:`~avid.core.ports.Camera` port, both behind the one
contract suite (P6, SDS §14.4):

* :class:`FakeCamera` emits synthetic RGB888 frames with a scriptable "person present"
  flag (SDS §14.8). It ships in ``adapters/`` (not ``tests/``) and *is* the simulator, so
  the sim can never drift from the real system — it is the real system with a different
  back end. Stdlib only: a synthetic frame is a ``bytes`` fill, no image library, no numpy.
* :class:`Picamera2Camera` wraps ``picamera2`` on the Pi. ``picamera2`` is an apt/optional
  dependency (ADR-008, SPK-5/#43) with no pip distribution, so it is imported **only**
  inside this module and **lazily**, inside the worker-thread helper — the module itself
  imports cleanly on CI and a laptop, where the fake path and mypy still need it to load.

Both capture on a worker thread where the work is blocking (``picamera2``'s is), never on
the event loop (P8, ADR-002). Constructed only by the composition root or a test fixture
(P3); everything else depends on the port, not these classes (P2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from avid.core.hal import CameraCaps, Frame

# Pixel layout the adapters produce and label. Three bytes per pixel; the fake fills them
# and the real adapter configures picamera2's main stream to match, so a consumer reads
# one format regardless of which camera it was handed.
_FORMAT = "RGB888"
_CHANNELS = 3


class FakeCamera:
    """The :class:`~avid.core.ports.Camera` fake (P6): synthetic frames, no hardware.

    ``width``/``height``/``fps`` are injected (P7) and define :attr:`capabilities`; a
    synthesized frame is exactly ``width * height * _CHANNELS`` bytes, so it is well-formed
    against those caps. ``person_present`` is a public, scriptable flag (SDS §14.8): the
    future vision service and behaviour tests toggle it, and it is reflected in the frame's
    byte fill so a consumer can tell the two states apart. Pass ``frames`` to replay a fixed
    script of :class:`~avid.core.hal.Frame` objects (cycled) instead of synthesizing.
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        frames: Sequence[Frame] | None = None,
    ) -> None:
        self._caps = CameraCaps(width=width, height=height, fps=fps)
        self._scripted: tuple[Frame, ...] = tuple(frames) if frames is not None else ()
        # Public, mutable: a test or the (later) vision service scripts presence directly.
        self.person_present = False
        self._started = False
        # The assertable record of how many frames the camera handed out.
        self.captures = 0

    @property
    def capabilities(self) -> CameraCaps:
        """What this camera can do, for negotiation (SDS §3.9.3)."""
        return self._caps

    async def start(self) -> None:
        """Idempotent-safe: starting an already-started fake is a no-op, never a raise."""
        self._started = True

    async def stop(self) -> None:
        """Idempotent-safe, mirroring :meth:`start`."""
        self._started = False

    async def capture(self) -> Frame:
        """Return the next frame. Pure and non-blocking (P8) — no I/O, so no thread hop."""
        frame = self._next_frame()
        self.captures += 1
        return frame

    def _next_frame(self) -> Frame:
        if self._scripted:
            return self._scripted[self.captures % len(self._scripted)]
        # 0xFF when a person is "present", 0x00 otherwise — enough for a consumer to
        # distinguish the scripted states without pulling in an image library.
        fill = 0xFF if self.person_present else 0x00
        width, height = self._caps.width, self._caps.height
        data = bytes([fill]) * (width * height * _CHANNELS)
        return Frame(data=data, width=width, height=height, format=_FORMAT)


class Picamera2Camera:
    """The real :class:`~avid.core.ports.Camera` on the Pi, wrapping ``picamera2``.

    ``picamera2`` is imported lazily inside :meth:`_start_blocking` (P5, ADR-008): the
    import is apt-only and absent on CI/a laptop, so keeping it out of module scope lets
    this module load everywhere — the fake path, mypy, and the composition-root import all
    work off-Pi. Configure and capture are blocking, so both run on a worker thread (P8).
    """

    def __init__(self, *, width: int, height: int, fps: int) -> None:
        self._caps = CameraCaps(width=width, height=height, fps=fps)
        # The picamera2 handle, once started. Untyped (Any) because picamera2 ships no
        # stubs (mypy resolves it via ignore_missing_imports) — so no numpy import is
        # needed here either, and none is referenced by name.
        self._picam: Any | None = None
        self._started = False

    @property
    def capabilities(self) -> CameraCaps:
        """What this camera can do, for negotiation (SDS §3.9.3)."""
        return self._caps

    async def start(self) -> None:
        """Open and start the camera on a worker thread. Idempotent-safe."""
        if self._started:
            return
        self._picam = await asyncio.to_thread(self._start_blocking)
        self._started = True

    def _start_blocking(self) -> Any:
        # Lazy, Pi/apt-only import — kept out of module scope so this file loads off-Pi
        # (P5, ADR-008, SPK-5). picamera2 has no stubs; mypy resolves it via override.
        import picamera2

        picam = picamera2.Picamera2()
        picam.configure(
            picam.create_still_configuration(
                main={"format": _FORMAT, "size": (self._caps.width, self._caps.height)}
            )
        )
        picam.start()
        return picam

    async def capture(self) -> Frame:
        """Grab the latest frame off the sensor on a worker thread (P8)."""
        if self._picam is None:
            raise RuntimeError("Picamera2Camera.capture() called before start()")
        array = await asyncio.to_thread(self._picam.capture_array)
        return Frame(
            data=bytes(array.tobytes()),
            width=self._caps.width,
            height=self._caps.height,
            format=_FORMAT,
        )

    async def stop(self) -> None:
        """Stop and close the camera on a worker thread. Idempotent-safe."""
        if self._picam is None:
            return
        picam, self._picam, self._started = self._picam, None, False
        await asyncio.to_thread(self._stop_blocking, picam)

    @staticmethod
    def _stop_blocking(picam: Any) -> None:
        picam.stop()
        picam.close()
