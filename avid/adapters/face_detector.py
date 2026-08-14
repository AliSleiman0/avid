"""Face-detector adapters — the fake that *is* the simulator (#220), and later the real one.

Implementations of the :class:`~avid.core.ports.FaceDetector` port, both behind the one
contract suite (P6, SDS §14.4). The port makes a single promise —
:meth:`~avid.core.ports.FaceDetector.detect`, this frame's faces, best-first — and every
adapter keeps it identically (SDS §3.6.5, ADR-013):

* :class:`FakeFaceDetector` reads :class:`~avid.core.hal.Frame` bytes the way ``FakeCamera``
  writes them and returns a **scriptable confidence timeline**. It *is* the simulator
  detector, so the sim can never drift from the real one — it is the real port with no model
  behind it (SDS §3.9.2). Stdlib only, no ``numpy``, no ONNX, no model blob.
* :class:`OnnxFaceDetector` runs YuNet via ``onnxruntime`` (#221, ADR-013). That library and
  ``numpy`` are pip-on-Pi (the ``pi`` extra) and absent off it (ADR-008, like ``picamera2`` /
  ``pyalsaaudio``), so they are imported **only** in this module and **lazily**, inside the
  session helper — the module itself imports cleanly on CI and a laptop, where the fake path
  and mypy still need it to load (P5).

``FakeCamera`` was built for this moment. Its ``person_present`` flag is *"reflected in the
frame's byte fill so a consumer can tell the two states apart"* — this is that consumer, and
the pairing is what lets #222's filter and #223's service be driven end to end in CI with no
camera, no model and no Pi.

Constructed only by the composition root or a test fixture (P3); everything else depends on
the port (P2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from concurrent.futures import Executor
from pathlib import Path
from typing import Any

from avid.core.hal import BBox, Detection, Frame

_log = logging.getLogger(__name__)

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


# --- the real detector (#221, ADR-013) ---------------------------------------

# Where a deploy/provisioning step puts the blob (tools/fetch_face_model.py). Kept out of git:
# the largest tracked file in this repo is 566 KB and there is no LFS. Overridable via the
# injected model_path (P7).
_DEFAULT_MODEL_PATH = Path("/var/lib/robot/models/face_detection_yunet_2026may.onnx")
_FETCH_SCRIPT = "tools/fetch_face_model.py"

# YuNet's feature strides. Both input dimensions must be divisible by the largest, and the
# decode below walks these in order — anchors are concatenated per stride, not interleaved.
_STRIDES: tuple[int, ...] = (8, 16, 32)
_STRIDE_ALIGN = 32

# The adapter's own floor, deliberately BELOW the service's [vision] confidence_threshold.
# Thresholding twice is not redundant: this one bounds how many boxes reach NMS (a cost), while
# the service's decides presence (a policy). Keeping the floor lower means the filter — which is
# pure, replayable and tunable against #225's trace — does the deciding, not the adapter.
_SCORE_FLOOR = 0.3
_NMS_IOU = 0.3

# Integer downscale applied before inference, and the single number that buys the ≤1-core
# budget. Measured on the Pi at 1 intra-op thread (tools/probe_face_detector.py, #221 AC-7):
#
#   input     median   cores   of a 200 ms frame period
#   640x480   152.1 ms  1.00   76%   <- no headroom; capture shares this thread
#   512x384    76.8 ms  1.00   38%
#   320x256    30.2 ms  1.00   15%   <- shipped
#   256x192    18.8 ms  1.00    9%
#
# 2x decimation of the rig's 640x480 lands on 320x240, which is padded to 320x256 below. A face
# at desk distance is ~120 px wide in the full frame and so ~60 px here, comfortably above what
# YuNet resolves. Injected rather than constant because #226 may trade it against accuracy on
# the real rig, where a smaller face at a greater distance is the thing that decides.
_DEFAULT_SCALE = 2


class OnnxFaceDetector:
    """The real :class:`~avid.core.ports.FaceDetector`: YuNet on ONNX Runtime (ADR-013).

    Two things about this adapter are load-bearing and neither is obvious from the outside.

    **The session is bound to one non-spinning thread** (SDS §3.8.2). ONNX Runtime defaults to
    one intra-op thread *per core* and those threads **spin-wait** between inferences. This
    project has paid for that default twice — Silero pegged 3 of 4 cores and the robot went
    deaf mid-M5, and ``LocalMiniLmEmbedder`` shipped with the same default for a whole
    milestone (#168). M8's gate is **≤1 core**, so inheriting it would fail the milestone's
    headline resource criterion on the first run *and look like "vision is expensive"* rather
    than like a two-line configuration mistake.

    ``vad.py``'s comment instructs any third ONNX adapter to copy the *reasoning* and measure
    its own number rather than paste the 1. Measured (``tools/probe_face_detector.py``, 320×256
    on the Pi): **1 thread → 30.2 ms at 1.00 core; 2 threads → 25.0 ms at 1.70 cores.** Two
    threads buy 17% latency for 70% more CPU, which is a bad trade against a one-core budget
    and a 200 ms frame period. So the answer here is 1 — for a different reason than Silero's,
    and against a measurement rather than by inheritance.

    **Inference runs on the executor it is handed, not on ``asyncio.to_thread``.** §3.8.2 gives
    capture and inference the same *dedicated single-thread pool*, and the default pool is
    sized to the CPU count — using it would quietly put this work on up to eight threads. The
    pool is injected by the composition root and owned by ``PresenceService`` (#223); passing
    none falls back to the default pool, which is correct for a probe or a one-shot script and
    wrong for the running robot.
    """

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        scale: int = _DEFAULT_SCALE,
        score_floor: float = _SCORE_FLOOR,
        nms_iou: float = _NMS_IOU,
        executor: Executor | None = None,
    ) -> None:
        self._model_path = model_path if model_path is not None else _DEFAULT_MODEL_PATH
        # **Fail loudly at construction, not at the first frame** (AC-5). A detector that
        # silently detects nothing is indistinguishable from an empty room, and that is the
        # worst possible failure for this milestone: every downstream test passes and the robot
        # simply never notices anyone. Stricter than LocalMiniLmEmbedder, which checks at first
        # use — by then the capture loop is running and the error is a log line nobody reads.
        if not self._model_path.is_file():
            raise FileNotFoundError(
                f"YuNet model not found at {self._model_path} — provision it on the device "
                f"with `python {_FETCH_SCRIPT}` (the ~230 KB blob is not committed; ADR-013)"
            )
        if scale < 1:
            raise ValueError(f"scale must be >= 1, got {scale}")
        self._scale = scale
        self._score_floor = score_floor
        self._nms_iou = nms_iou
        self._executor = executor
        # Built lazily on the Pi; untyped (Any) because onnxruntime/numpy ship no stubs and are
        # absent off-Pi (mypy resolves them via ignore_missing_imports).
        self._session: Any | None = None
        self._input_name: str = "input"

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        """This frame's faces, best-first. Offloaded — never on the event loop (P8)."""
        loop = asyncio.get_running_loop()
        if self._executor is None:
            return await asyncio.to_thread(self._detect_sync, frame)
        return await loop.run_in_executor(self._executor, self._detect_sync, frame)

    # --- the worker-thread body ----------------------------------------------

    def _detect_sync(self, frame: Frame) -> tuple[Detection, ...]:
        """Preprocess, infer and decode on the worker thread (never on the loop)."""
        np = self._ensure_session()
        assert self._session is not None
        started_ns = time.monotonic_ns()

        tensor, offsets = self._preprocess(frame, np)
        outputs = self._session.run(None, {self._input_name: tensor})
        named = dict(
            zip([o.name for o in self._session.get_outputs()], outputs, strict=True)
        )
        detections = self._decode(named, frame, offsets, np)

        elapsed_ms = (time.monotonic_ns() - started_ns) / 1_000_000
        _log.debug(
            "yunet: %d face(s) in %.1f ms (%dx%d -> %dx%d)",
            len(detections),
            elapsed_ms,
            frame.width,
            frame.height,
            offsets[0],
            offsets[1],
        )
        return detections

    def _ensure_session(self) -> Any:
        """Build the ONNX session on first use; return the ``numpy`` module.

        Lazy, Pi-only imports — kept out of module scope so this file loads off-Pi (P5,
        ADR-008). Neither library ships stubs; mypy resolves them via ignore_missing_imports."""
        import numpy as np

        if self._session is None:
            import onnxruntime

            # One non-spinning thread. See the class docstring for the measurement behind the
            # 1 and for why it is NOT pasted from vad.py: ONNX Runtime otherwise runs one
            # intra-op thread per core and spin-waits between inferences, which on four cores
            # starves the audio loop — a P8 violation by CPU monopoly, invisible to code review
            # and to `asyncio.to_thread`, and the reason M8's gate is stated in cores.
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            # Belt and braces: even at one thread the pool spins between calls unless told not
            # to, and this loop is idle 85% of every frame period — exactly the window M5's
            # lesson was about.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self._session = onnxruntime.InferenceSession(
                str(self._model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
        return np

    def _preprocess(self, frame: Frame, np: Any) -> tuple[Any, tuple[int, int]]:
        """``Frame`` bytes → the NCHW float32 tensor YuNet wants. Returns it and the model size.

        Format is **honoured, not assumed** (AC-8). ``Frame`` carries its own
        ``format``/``width``/``height`` precisely so a consumer can obey them, and an adapter
        that guessed would repeat the M4 speaker defect — three weeks of 16 kHz audio played
        through a 24 kHz handle because ``AudioChunk.sample_rate`` was ignored.

        Three steps, all numpy, no OpenCV (ADR-013 rejected that dependency):

        1. **Decimate** by an integer factor, by plain subsampling. Integer-only so the
           coordinate map back is an exact multiply — no rounding, no interpolation kernel, no
           half-pixel convention to get wrong.

           This started as a proper 2×2 box filter, which is the textbook answer, and the
           measurement retired it: on the Pi at 640×480 the box filter costs **49.6 ms** —
           *more than the inference it feeds* — against **1.3 ms** for subsampling, and the
           detector cannot tell the difference. Scored across five face sizes from 200 px down
           to 30 px, peak confidence differed by ≤0.005 and the above-threshold anchor counts
           by less than the run-to-run noise. Faces are low-frequency structure, so the
           aliasing a box filter suppresses is not aliasing this model was reading. Spending a
           quarter of the frame budget on it would have been invisible waste.

        2. **Pad** right/bottom to a multiple of 32 (YuNet's largest stride). Padding at the
           far edges only, never centred, so padded coordinates need **no offset** — the
           inverse map stays a single multiply. Grey rather than black, because a hard black
           border invents a high-contrast edge the detector has to reject.
        3. **BGR and NCHW.** YuNet was trained through OpenCV's ``blobFromImage`` with
           ``scalefactor=1.0`` and no mean subtraction, on BGR input — so the channel swap is
           the whole of the normalisation, and it is the single highest-risk line here: get it
           wrong and the detector degrades quietly rather than failing. **Measured, not
           assumed:** the same image fed with the channels reversed yields **8 detections
           against 57**, at plausible-looking confidences. That is what "degrades quietly"
           means, and it is why the order is pinned by a test rather than by a comment.
        """
        if frame.format != _FORMAT:
            raise ValueError(
                f"OnnxFaceDetector cannot read frame format {frame.format!r} — "
                f"expected {_FORMAT!r} (adapters/camera.py produces it)"
            )
        if frame.width <= 0 or frame.height <= 0:
            raise ValueError(
                f"OnnxFaceDetector got a degenerate frame: {frame.width}x{frame.height}"
            )
        expected = frame.width * frame.height * _CHANNELS
        if len(frame.data) != expected:
            raise ValueError(
                f"OnnxFaceDetector got {len(frame.data)} bytes for a "
                f"{frame.width}x{frame.height} {_FORMAT} frame — expected {expected}"
            )

        rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(
            frame.height, frame.width, _CHANNELS
        )
        small = (
            rgb[:: self._scale, :: self._scale].astype(np.float32)
            if self._scale > 1
            else rgb.astype(np.float32)
        )

        pad_h = (-small.shape[0]) % _STRIDE_ALIGN
        pad_w = (-small.shape[1]) % _STRIDE_ALIGN
        if pad_h or pad_w:
            small = np.pad(
                small,
                ((0, pad_h), (0, pad_w), (0, 0)),
                mode="constant",
                constant_values=114.0,  # the neutral grey letterbox fill, not black
            )

        bgr = small[:, :, ::-1]
        tensor = np.ascontiguousarray(bgr.transpose(2, 0, 1)[None], dtype=np.float32)
        return tensor, (int(small.shape[1]), int(small.shape[0]))

    def _decode(
        self,
        outputs: dict[str, Any],
        frame: Frame,
        model_size: tuple[int, int],
        np: Any,
    ) -> tuple[Detection, ...]:
        """YuNet's 12 tensors → our vocabulary, in the original frame's pixels.

        Per stride ``s``: anchors are laid out row-major over a ``(H/s, W/s)`` grid, the score
        is ``sqrt(cls * obj)`` (the geometric mean of the classification and objectness heads,
        which is what OpenCV's own post-processing uses), the box centre is the anchor's grid
        position plus a learned offset in stride units, and the extent is exponential. The
        ``kps_*`` tensors are discarded entirely: the port has no keypoints and M8 has no use
        for them, and a port shaped by what the model emits would be the inversion backwards.
        """
        model_w, model_h = model_size
        boxes: list[Any] = []
        scores: list[Any] = []
        for stride in _STRIDES:
            cls = outputs[f"cls_{stride}"][0].reshape(-1)
            obj = outputs[f"obj_{stride}"][0].reshape(-1)
            bbox = outputs[f"bbox_{stride}"][0].reshape(-1, 4)
            cols = model_w // stride
            index = np.arange(cls.shape[0])
            col = (index % cols).astype(np.float32)
            row = (index // cols).astype(np.float32)

            score = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
            cx = (col + bbox[:, 0]) * stride
            cy = (row + bbox[:, 1]) * stride
            width = np.exp(bbox[:, 2]) * stride
            height = np.exp(bbox[:, 3]) * stride
            boxes.append(
                np.stack([cx - width / 2, cy - height / 2, width, height], axis=1)
            )
            scores.append(score)

        all_boxes = np.concatenate(boxes, axis=0)
        all_scores = np.concatenate(scores, axis=0)
        keep = all_scores >= self._score_floor
        all_boxes, all_scores = all_boxes[keep], all_scores[keep]
        if all_boxes.shape[0] == 0:
            return ()

        order = np.argsort(-all_scores)
        all_boxes, all_scores = all_boxes[order], all_scores[order]
        kept = _nms(all_boxes, self._nms_iou, np)

        # Back to the caller's pixels: a single multiply, because decimation was integer and
        # padding was right/bottom only. Then clip — a box may legitimately overhang the real
        # image into the padded margin, and a box entirely inside it is an artefact to drop.
        results: list[Detection] = []
        for i in kept:
            x, y, w, h = (float(v) * self._scale for v in all_boxes[i])
            x0 = max(0, min(frame.width, int(round(x))))
            y0 = max(0, min(frame.height, int(round(y))))
            x1 = max(0, min(frame.width, int(round(x + w))))
            y1 = max(0, min(frame.height, int(round(y + h))))
            if x1 - x0 <= 0 or y1 - y0 <= 0:
                continue
            results.append(
                Detection(
                    confidence=float(all_scores[i]),
                    box=BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0),
                )
            )
        # Best-first is the port's promise; largest-first is what ``largest_bbox`` needs. They
        # are not the same order, so the service takes the max by area rather than [0] — the
        # contract asserts both properties separately for exactly this reason.
        return tuple(results)


def _nms(boxes: Any, iou_threshold: float, np: Any) -> list[int]:
    """Greedy non-maximum suppression over score-sorted ``(x, y, w, h)`` boxes.

    Twenty lines of numpy rather than a dependency: ADR-013 rejected OpenCV, and this is the
    only piece of it the adapter would have wanted.
    """
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 0] + boxes[:, 2]
    y2 = boxes[:, 1] + boxes[:, 3]
    areas = np.maximum(0.0, boxes[:, 2]) * np.maximum(0.0, boxes[:, 3])
    remaining = list(range(boxes.shape[0]))
    kept: list[int] = []
    while remaining:
        current = remaining.pop(0)
        kept.append(current)
        if not remaining:
            break
        others = np.array(remaining)
        xx1 = np.maximum(x1[current], x1[others])
        yy1 = np.maximum(y1[current], y1[others])
        xx2 = np.minimum(x2[current], x2[others])
        yy2 = np.minimum(y2[current], y2[others])
        overlap = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[others] - overlap
        iou = np.where(union > 0, overlap / np.maximum(union, 1e-9), 0.0)
        remaining = [
            int(i) for i, keep in zip(others, iou <= iou_threshold, strict=True) if keep
        ]
    return kept


__all__ = ["FakeFaceDetector", "OnnxFaceDetector"]
