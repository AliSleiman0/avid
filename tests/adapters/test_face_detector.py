"""``OnnxFaceDetector``'s off-port behaviour (#221, ADR-013).

The contract suite (``tests/contract/test_face_detector.py``) covers the port's promises against
both adapters. This file covers the three things that are *not* port behaviour and that only
this adapter has: that a missing model blob fails **at construction**, that the preprocessing
maps coordinates back correctly, and that the channel order is the one that was measured rather
than the one that felt right.

None of it needs ONNX, numpy or a Pi — the pieces under test are the guards in front of the
session and the arithmetic around it. What genuinely needs the device (that the model loads,
that a real face is found, and what it costs) is the contract suite's real leg and #221 AC-7's
measurement, both run on the Pi.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avid.adapters.face_detector import OnnxFaceDetector
from avid.core.hal import Frame


def _model(tmp_path: Path) -> Path:
    path = tmp_path / "face_detection_yunet_2026may.onnx"
    path.write_bytes(
        b""
    )  # never read: every test here stops before the session is built
    return path


# --- AC-5: a missing model is loud, and loud at construction -----------------


def test_a_missing_model_raises_at_construction_not_at_the_first_frame(
    tmp_path: Path,
) -> None:
    """The milestone's worst failure mode, guarded at the earliest possible moment.

    A detector that silently detects nothing is **indistinguishable from an empty room**: every
    downstream test passes, the capture loop runs, the filter behaves, and the robot simply
    never notices anyone. Failing at first use — as ``LocalMiniLmEmbedder`` does — is not good
    enough here, because by then the service has started and the error is a log line inside a
    loop rather than a boot that stops.
    """
    with pytest.raises(FileNotFoundError) as excinfo:
        OnnxFaceDetector(model_path=tmp_path / "absent.onnx")

    message = str(excinfo.value)
    # The message has to be actionable, not just correct: the path that was looked at, and the
    # command that fixes it. An operator reading this at the bench should not have to grep.
    assert "absent.onnx" in message
    assert "tools/fetch_face_model.py" in message


def test_a_present_model_constructs_without_touching_onnx(tmp_path: Path) -> None:
    """Construction checks the file and stops. The session is built lazily on first detect, so
    ``main.py`` can build this adapter off-Pi — which is what keeps the composition root
    importable everywhere (P5, ADR-008)."""
    detector = OnnxFaceDetector(model_path=_model(tmp_path))
    assert detector._session is None


def test_a_nonsense_scale_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OnnxFaceDetector(model_path=_model(tmp_path), scale=0)


# --- frame validation: reject, never shrug ----------------------------------


@pytest.mark.parametrize(
    ("frame", "needle"),
    [
        pytest.param(
            Frame(data=b"\x00" * 12, width=2, height=2, format="BGR888"),
            "BGR888",
            id="wrong-format",
        ),
        pytest.param(
            Frame(data=b"\x00" * 5, width=2, height=2, format="RGB888"),
            "expected 12",
            id="truncated-payload",
        ),
        pytest.param(
            Frame(data=b"", width=0, height=4, format="RGB888"),
            "degenerate",
            id="degenerate-geometry",
        ),
    ],
)
def test_an_unreadable_frame_names_what_was_wrong(
    tmp_path: Path, frame: Frame, needle: str
) -> None:
    """Validation happens before numpy is touched, so these run anywhere — and the message says
    which of the three things was wrong. ``Frame`` carries its own format and geometry precisely
    so a consumer can honour them; the M4 speaker defect (three weeks of 16 kHz audio through a
    24 kHz handle) is what ignoring them looks like in a different port."""
    detector = OnnxFaceDetector(model_path=_model(tmp_path))
    with pytest.raises(ValueError, match=needle):
        detector._preprocess(frame, _numpy())


# --- the preprocessing arithmetic -------------------------------------------


def test_decimation_and_padding_produce_a_stride_aligned_bgr_tensor(
    tmp_path: Path,
) -> None:
    """The rig's 640×480 at scale 2 becomes 320×240, padded to 320×256 — and the padding goes on
    the **bottom**, never centred, which is what keeps the inverse coordinate map a single
    multiply with no offset to get wrong.

    The channel order is asserted here rather than described in a comment, because it is the
    highest-risk line in the adapter: measured on the Pi, feeding the same image with the
    channels reversed found **8 faces against 57**, at plausible-looking confidences. A wrong
    swap does not fail — it quietly halves the robot's eyesight.
    """
    np = _numpy()
    # A frame whose R, G and B planes are distinguishable, so a swap is visible.
    pixel = bytes((10, 20, 30))
    frame = Frame(data=pixel * (640 * 480), width=640, height=480, format="RGB888")

    detector = OnnxFaceDetector(model_path=_model(tmp_path), scale=2)
    tensor, size = detector._preprocess(frame, np)

    assert size == (320, 256)
    assert tensor.shape == (1, 3, 256, 320)
    assert tensor.dtype == np.float32
    # BGR: channel 0 must carry the blue value (30), channel 2 the red (10).
    assert tensor[0, 0, 0, 0] == pytest.approx(30.0)
    assert tensor[0, 1, 0, 0] == pytest.approx(20.0)
    assert tensor[0, 2, 0, 0] == pytest.approx(10.0)
    # The padded rows are the neutral grey fill, and they are at the bottom.
    assert tensor[0, 0, 250, 0] == pytest.approx(114.0)
    assert tensor[0, 0, 239, 0] == pytest.approx(30.0)


def test_scale_one_passes_the_frame_through_at_full_resolution(tmp_path: Path) -> None:
    """640×480 is already stride-aligned (both divisible by 32), so at scale 1 there is no
    resampling *and* no padding — the bytes go in as they came off the sensor. Worth pinning:
    ADR-013's original pin was a statically-shaped export that could not do this at all."""
    np = _numpy()
    frame = Frame(data=bytes(640 * 480 * 3), width=640, height=480, format="RGB888")
    detector = OnnxFaceDetector(model_path=_model(tmp_path), scale=1)
    tensor, size = detector._preprocess(frame, np)
    assert size == (640, 480)
    assert tensor.shape == (1, 3, 480, 640)


def test_a_geometry_that_is_not_stride_aligned_is_padded_not_rejected(
    tmp_path: Path,
) -> None:
    """The port takes whatever the negotiated camera produces (§3.9.3), not only the rig's
    geometry, so an odd size must still work — padded up to the next multiple of 32."""
    np = _numpy()
    frame = Frame(data=bytes(100 * 70 * 3), width=100, height=70, format="RGB888")
    detector = OnnxFaceDetector(model_path=_model(tmp_path), scale=1)
    _, size = detector._preprocess(frame, np)
    assert size == (128, 96)
    assert size[0] % 32 == 0 and size[1] % 32 == 0


def _numpy() -> object:
    numpy = pytest.importorskip(
        "numpy",
        reason="the preprocessing arithmetic needs numpy (the memory/pi extras)",
    )
    return numpy
