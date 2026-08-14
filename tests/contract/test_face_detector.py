"""Contract suite for the ``FaceDetector`` port (#220, SDS §3.6.5, §3.9.1, §14.4, ADR-013).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). The shared tier is parametrized over the hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case skips off
the Pi and, on the Pi, exercises the real YuNet adapter — the same seam ``test_camera.py`` and
``test_vad.py`` use.

**Assertions are made through the port, and never about what the model saw.** Whether a
particular frame really contains a face is a human's call (SDS §14.8); what the contract can
insist on is the port's promises — that detection is a function of the frame, that an empty
frame yields nothing without raising, that a malformed frame is an error rather than a silent
empty result, and that no vendor type leaks out. The last two matter most: a detector that
quietly reports nothing is indistinguishable from an empty room, which is this milestone's
worst failure mode because every downstream test still passes.

Frames are built here rather than pulled from ``FakeCamera`` so the suite states its own
inputs, but they are byte-identical to that camera's output — an all-``0xFF`` fill for
"someone is here", all-``0x00`` for an empty room (§14.8).
"""

from __future__ import annotations

import pytest

from avid.adapters.face_detector import FakeFaceDetector
from avid.core.hal import BBox, Detection, Frame
from avid.core.ports import FaceDetector

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# Small on the fake so a synthetic frame is cheap. The real detector gets the rig's negotiated
# geometry: YuNet's strides are 8/16/32 so its input must be divisible by 32, and 640x480
# already is — which is why the real adapter performs no resize at all (ADR-013).
_FAKE_WIDTH, _FAKE_HEIGHT = 64, 48
_REAL_WIDTH, _REAL_HEIGHT = 640, 480
_CHANNELS = 3
_FORMAT = "RGB888"


def _frame(width: int, height: int, *, fill: int) -> Frame:
    """A well-formed ``RGB888`` frame of one repeated byte — ``FakeCamera``'s own output shape."""
    return Frame(
        data=bytes([fill]) * (width * height * _CHANNELS),
        width=width,
        height=height,
        format=_FORMAT,
    )


# --- shared contract: every FaceDetector adapter must satisfy it --------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def detector(request: pytest.FixtureRequest) -> FaceDetector:
    """Every FaceDetector adapter, real and fake, must satisfy the tests below (P6, §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs the
    YuNet adapter, which needs its model blob provisioned (``tools/fetch_face_model.py``)."""
    if request.param == "fake":
        return FakeFaceDetector()
    skip_off_pi()
    # Imported here, not at module top: onnxruntime is Pi-only and absent off the Pi, so only
    # the on-Pi "real" branch ever touches it (P5, ADR-008).
    from avid.adapters.face_detector import OnnxFaceDetector

    return OnnxFaceDetector()


@pytest.fixture
def geometry(request: pytest.FixtureRequest) -> tuple[int, int]:
    """The frame size to drive the current adapter with — the fake's cheap one, or the rig's."""
    param = request.node.callspec.params.get("detector")
    if param == "fake":
        return _FAKE_WIDTH, _FAKE_HEIGHT
    return _REAL_WIDTH, _REAL_HEIGHT


def test_adapter_satisfies_the_face_detector_port(detector: FaceDetector) -> None:
    assert isinstance(detector, FaceDetector)


async def test_detect_returns_our_vocabulary_not_a_librarys(
    detector: FaceDetector, geometry: tuple[int, int]
) -> None:
    """Whatever comes back is a sequence of :class:`~avid.core.hal.Detection`, each carrying a
    plain ``float`` and a :class:`~avid.core.hal.BBox` — no tensor, no ndarray, no vendor
    result object. A detector that returned a raw tensor would have leaked its implementation
    through the port and made the fake impossible to write honestly (AC-2)."""
    width, height = geometry
    result = await detector.detect(_frame(width, height, fill=0xFF))

    assert isinstance(result, tuple | list)
    for detection in result:
        assert isinstance(detection, Detection)
        assert type(detection.confidence) is float
        assert isinstance(detection.box, BBox)
        assert 0.0 <= detection.confidence <= 1.0


async def test_detection_is_pure_with_respect_to_the_frame(
    detector: FaceDetector, geometry: tuple[int, int]
) -> None:
    """The same frame yields the same answer. Confidence is allowed to move — the fake walks a
    script and a real model is not bit-reproducible across sessions — but *what was found*
    must not: same count, same boxes. Without this the replay in #225 would be meaningless."""
    width, height = geometry
    frame = _frame(width, height, fill=0xFF)

    first = await detector.detect(frame)
    second = await detector.detect(frame)

    assert len(first) == len(second)
    assert [d.box for d in first] == [d.box for d in second]


async def test_an_empty_frame_yields_no_detections_and_does_not_raise(
    detector: FaceDetector, geometry: tuple[int, int]
) -> None:
    """An all-black frame is a legitimate picture of an empty room, not an error. Returning
    ``()`` is the correct answer and the loop must keep running on it."""
    width, height = geometry
    assert await detector.detect(_frame(width, height, fill=0x00)) == ()


async def test_boxes_fall_inside_the_frame_and_are_ordered_largest_first(
    detector: FaceDetector, geometry: tuple[int, int]
) -> None:
    """Coordinates are pixels of *this* frame (§3.9.1), so a box outside its bounds is a
    convention bug — the silent kind that only shows up as a servo aiming wrong in M9.
    Ordering is asserted too: ``vision.face_detected`` carries ``largest_bbox``, and a service
    that took ``result[0]`` must agree with one that took the max by area."""
    width, height = geometry
    result = await detector.detect(_frame(width, height, fill=0xFF))

    for detection in result:
        box = detection.box
        assert 0 <= box.x and 0 <= box.y
        assert box.w > 0 and box.h > 0
        assert box.x + box.w <= width
        assert box.y + box.h <= height
    areas = [d.box.area for d in result]
    assert areas == sorted(areas, reverse=True)


@pytest.mark.parametrize(
    ("frame", "why"),
    [
        pytest.param(
            Frame(data=b"\x00" * 12, width=2, height=2, format="BGR888"),
            "unknown pixel format",
            id="wrong-format",
        ),
        pytest.param(
            Frame(data=b"\x00" * 5, width=2, height=2, format=_FORMAT),
            "payload shorter than the geometry implies",
            id="truncated-payload",
        ),
        pytest.param(
            Frame(data=b"", width=0, height=0, format=_FORMAT),
            "degenerate geometry",
            id="empty-frame",
        ),
    ],
)
async def test_a_malformed_frame_is_an_error_not_a_silent_empty_result(
    detector: FaceDetector, frame: Frame, why: str
) -> None:
    """The contract's sharpest promise. ``Frame`` carries its own ``format``/``width``/
    ``height`` precisely so a consumer can honour them, and an adapter that shrugged and
    returned ``()`` would report an empty room for a frame it simply could not read — with
    every downstream test still green. The M4 speaker defect (an adapter that ignored
    ``AudioChunk.sample_rate`` for three weeks) is the same mistake in a different port."""
    with pytest.raises((ValueError, TypeError)):
        await detector.detect(frame)


# --- FakeFaceDetector-specific: the simulator's scriptable behaviour (§14.8) --


async def test_it_honours_fake_cameras_person_present_signal() -> None:
    """AC-4, and the reason this fake can be paired with that one: a frame synthesized with
    ``person_present = True`` is detectable and one without is not. Driven through the real
    ``FakeCamera`` rather than a hand-built frame, because the point is that the two adapters
    compose into a working simulator with no hardware — which is what makes #223 testable."""
    from avid.adapters.camera import FakeCamera

    camera = FakeCamera(width=_FAKE_WIDTH, height=_FAKE_HEIGHT, fps=5)
    detector = FakeFaceDetector()

    camera.person_present = False
    assert await detector.detect(await camera.capture()) == ()

    camera.person_present = True
    assert len(await detector.detect(await camera.capture())) == 1


async def test_the_confidence_script_is_walked_then_held() -> None:
    """#222's flapping cases need a deterministic confidence timeline — a slow fade across the
    threshold, a flicker at the edge. The script is walked one call at a time and **holds its
    last value** once exhausted, so a test can say "0.9, 0.7, then 0.4 forever" without
    listing 18,000 frames."""
    detector = FakeFaceDetector([0.9, 0.7, 0.4])
    frame = _frame(_FAKE_WIDTH, _FAKE_HEIGHT, fill=0xFF)

    scores = [(await detector.detect(frame))[0].confidence for _ in range(5)]
    assert scores == [0.9, 0.7, 0.4, 0.4, 0.4]


async def test_an_empty_script_reports_the_default_forever() -> None:
    detector = FakeFaceDetector(default=0.55)
    frame = _frame(_FAKE_WIDTH, _FAKE_HEIGHT, fill=0xFF)
    assert (await detector.detect(frame))[0].confidence == 0.55
    assert (await detector.detect(frame))[0].confidence == 0.55


async def test_the_calls_counter_counts_every_frame_judged() -> None:
    """The off-port observation point the contract asserts on, exactly as
    ``FakeCamera.captures`` is off the ``Camera`` port. It counts frames *judged*, including
    the empty ones — a frame with nobody in it was still work."""
    detector = FakeFaceDetector()
    assert detector.calls == 0
    await detector.detect(_frame(_FAKE_WIDTH, _FAKE_HEIGHT, fill=0xFF))
    await detector.detect(_frame(_FAKE_WIDTH, _FAKE_HEIGHT, fill=0x00))
    assert detector.calls == 2


async def test_the_face_count_is_scriptable_and_boxes_stay_distinct() -> None:
    """``vision.face_detected`` carries a ``count``, so the fake has to be able to produce more
    than one — and the boxes must differ, or "largest" would be a coin toss."""
    detector = FakeFaceDetector(faces=3)
    result = await detector.detect(_frame(_FAKE_WIDTH, _FAKE_HEIGHT, fill=0xFF))
    assert len(result) == 3
    assert len({d.box for d in result}) == 3
