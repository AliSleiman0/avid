"""Face-detector adapters — the fake that *is* the simulator (#220), and later the real one.

Implementations of the :class:`~avid.core.ports.FaceDetector` port, both behind the one
contract suite (P6, SDS §14.4). The port makes a single promise —
:meth:`~avid.core.ports.FaceDetector.detect`, this frame's faces, best-first — and every
adapter keeps it identically (SDS §3.6.5, ADR-013):

* :class:`FakeFaceDetector` reads :class:`~avid.core.hal.Frame` bytes the way ``FakeCamera``
  writes them and returns a **scriptable confidence timeline**. It *is* the simulator
  detector, so the sim can never drift from the real one — it is the real port with no model
  behind it (SDS §3.9.2). Stdlib only, no ``numpy``, no ONNX, no model blob.
* ``OnnxFaceDetector`` (#221) runs YuNet via ``onnxruntime``, lazily imported inside its
  worker so this module still loads on CI and a laptop where the ``pi`` extra is absent.

``FakeCamera`` was built for this moment. Its ``person_present`` flag is *"reflected in the
frame's byte fill so a consumer can tell the two states apart"* — this is that consumer, and
the pairing is what lets #222's filter and #223's service be driven end to end in CI with no
camera, no model and no Pi.

Constructed only by the composition root or a test fixture (P3); everything else depends on
the port (P2).
"""

from __future__ import annotations

from collections.abc import Sequence

from avid.core.hal import BBox, Detection, Frame

# The pixel layout both camera adapters produce and label (``adapters/camera.py``). A frame
# in any other format is a frame this adapter cannot read, and it says so rather than
# reporting an empty room — see the module note in ``detect``.
_FORMAT = "RGB888"
_CHANNELS = 3

# The box a synthesized frame yields: a plausible desk-distance face, centred, a quarter of
# the frame's width. Deterministic — the whole point of a fake is that the same frame gives
# the same answer, which is the first thing the contract suite asserts.
_FACE_WIDTH_FRACTION = 0.25


class FakeFaceDetector:
    """The :class:`~avid.core.ports.FaceDetector` fake (P6): frame bytes and a script, no model.

    Two inputs decide what it reports, and the split is deliberate. **Whether** a face is seen
    comes from the frame — a ``FakeCamera`` frame synthesized with ``person_present = True`` is
    detectable and one without is not (AC-4), so a test can drive presence by flipping the
    camera's flag and never touch the detector. **How confident** it is comes from an injected
    ``script`` of scores, walked one call at a time and holding the last value once exhausted
    (or ``default`` if the script was empty), so #222's flapping cases — a slow fade across the
    threshold, a flicker at the edge — can be driven deterministically without any notion of
    lighting.

    ``faces`` sets how many detections a positive frame yields, which is what gives
    ``vision.face_detected`` a ``count`` to carry. The public :attr:`calls` counter is the
    off-port observation point the contract asserts on, exactly as ``FakeCamera.captures`` is
    off the ``Camera`` port. Stdlib only.
    """

    def __init__(
        self,
        script: Sequence[float] | None = None,
        *,
        default: float = 0.9,
        faces: int = 1,
    ) -> None:
        self._script = list(script) if script is not None else []
        self._default = default
        self._faces = faces
        # Public, assertable: how many frames were judged (off the port).
        self.calls = 0

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        """This frame's faces, best-first, at the next scripted confidence.

        Pure with respect to the frame in the sense the contract requires — the *same* frame
        yields the same boxes and the same count every time. Only the confidence advances,
        because that is the axis a timeline test needs to script and the one a real detector
        genuinely varies frame to frame.
        """
        _reject_unreadable(frame)
        confidence = self._next_confidence()
        if not _looks_occupied(frame):
            return ()
        return tuple(
            Detection(confidence=confidence, box=box)
            for box in _synthetic_boxes(frame, self._faces)
        )

    def _next_confidence(self) -> float:
        index = self.calls
        self.calls += 1
        if not self._script:
            return self._default
        if index < len(self._script):
            return self._script[index]
        return self._script[-1]


def _reject_unreadable(frame: Frame) -> None:
    """Raise on a frame this adapter cannot read, rather than reporting an empty room.

    A malformed frame is an **error**, never a silent empty result (the contract says so).
    The reason is the milestone's worst failure mode: a detector that quietly detects nothing
    is indistinguishable from nobody being there, every downstream test still passes, and the
    robot simply never notices anyone. The real adapter has the same rule for the same reason
    (#221 AC-8) — and the M4 speaker defect, an adapter that ignored ``AudioChunk.sample_rate``
    for three weeks, is this mistake in a different port.
    """
    if frame.format != _FORMAT:
        raise ValueError(
            f"FakeFaceDetector cannot read frame format {frame.format!r} — "
            f"expected {_FORMAT!r} (adapters/camera.py produces it)"
        )
    if frame.width <= 0 or frame.height <= 0:
        raise ValueError(
            f"FakeFaceDetector got a degenerate frame: {frame.width}x{frame.height}"
        )
    expected = frame.width * frame.height * _CHANNELS
    if len(frame.data) != expected:
        raise ValueError(
            f"FakeFaceDetector got {len(frame.data)} bytes for a "
            f"{frame.width}x{frame.height} {_FORMAT} frame — expected {expected}"
        )


def _looks_occupied(frame: Frame) -> bool:
    """Whether the frame carries ``FakeCamera``'s "person present" signal (AC-4).

    That fake fills every byte with ``0xFF`` when its flag is set and ``0x00`` when it is not.
    Reading *any* non-zero byte rather than testing for ``0xFF`` exactly means a scripted
    frame holding a real captured image also reads as occupied, which is what a replay test
    needs — and an all-black frame still reads as empty, which is what the contract asserts.
    """
    return any(frame.data)


def _synthetic_boxes(frame: Frame, faces: int) -> tuple[BBox, ...]:
    """``faces`` plausible boxes for *frame*, **largest first**, all inside its bounds.

    Descending in size so ``result[0]`` is the largest by :attr:`~avid.core.hal.BBox.area` and
    "best-first" and "largest-first" agree for the fake — a service picking the largest box
    and one taking the first must not disagree here, or a bug in either would hide.
    """
    side = max(1, int(frame.width * _FACE_WIDTH_FRACTION))
    boxes: list[BBox] = []
    for index in range(max(0, faces)):
        # Each successive face is a little smaller and a little further right, so the set is
        # strictly ordered by area and no two boxes are identical.
        extent = max(1, side - index * (side // 4 or 1))
        x = min(frame.width - extent, index * extent)
        y = max(0, (frame.height - extent) // 2)
        boxes.append(BBox(x=max(0, x), y=y, w=extent, h=extent))
    return tuple(boxes)


__all__ = ["FakeFaceDetector"]
