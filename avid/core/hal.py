"""HAL value types — the vocabulary the ports traffic in (SDS §3.9.1, AVID-11).

These are the frames, chunks, and capability descriptors that cross a port
boundary. They are *what* the application exchanges with the physical world,
defined by the application's needs (SDS §3.9.1) — never by a device's SDK.

Pure and dependency-light on purpose: stdlib only, payloads are ``bytes``. No
``numpy`` here — ``core`` stays lean and imports cleanly on the Pi's system
Python (ADR-008), and pixel/PCM buffers do not need an array library to be
handed across a port. All types are frozen/slotted/kw-only, matching the
:class:`~avid.domain.Event` envelope: an adapter cannot mutate a caller's frame.

Minimal by design. Fields are the smallest set that types the ports and feeds
the AVID-12/13 fakes; a fake or real adapter may carry richer detail behind the
same port without changing this vocabulary.

Two members of that vocabulary — :class:`~avid.domain.vision.BBox` and
:class:`~avid.domain.motion.Axis` — are *defined* in ``avid/domain/`` and
re-exported below rather than declared here. Each is named by a domain event to
type its payload (``vision.face_detected``, ``motion.gesture_started``), and the
``layers`` contract puts ``core`` above ``domain``, so declaring them here would
make those events the first ``domain -> core`` imports in the project (P1).
``avid.core.hal.BBox`` and ``avid.core.hal.Axis`` stay the spelling every port
and adapter uses; only the declarations moved. Recorded in SDS §3.6.5 (ADR-013)
and §3.9.4 (ADR-009).
"""

from __future__ import annotations

from dataclasses import dataclass

from avid.domain.motion import Axis
from avid.domain.vision import BBox

# S16_LE, 2 bytes per sample per channel — the one PCM format every audio port
# exchanges (see adapters/microphone.py, adapters/speaker.py). ``AudioChunk``
# carries no bit-depth field, so sample *width* is implicit; sample *rate* and
# channel count are not — they are fields, and a consumer must honour them.
SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioChunk:
    """A slice of PCM audio crossing the mic/speaker ports (SDS §3.9.1).

    Exchanged by :meth:`~avid.core.ports.Microphone.stream` (captured) and
    :meth:`~avid.core.ports.Speaker.play` (played back).
    """

    pcm: bytes
    sample_rate: int
    channels: int


def pcm_duration_ms(pcm: bytes, *, sample_rate: int, channels: int) -> int:
    """Milliseconds of S16_LE *pcm* — its byte length over the bytes-per-ms of its format.

    Floors. Format is a parameter, not a constant: mic capture is 16 kHz (32 bytes/ms)
    and Realtime playback is 24 kHz (48 bytes/ms), so the same arithmetic serves both —
    which is exactly AC-3's "÷ 48 at 24 kHz mono 16-bit" for real audio and ÷ 32 for the
    M4 loopback echo of 16 kHz capture.

    Lives in ``core`` because three layers need it and none may reach for another's copy:
    ``AudioService`` sizes a turn with it, ``FakeSpeaker`` reports playback with it, and
    ``AlsaSpeaker`` converts the frames ALSA accepted back into the milliseconds the
    ``Speaker`` port returns (AVID-91). One formula, one test.

    **This is submitted duration, not played duration.** It answers "how long is this
    buffer", never "how much of it reached a DAC" — computing the latter from the former
    is precisely how the M4 gate passed while the robot was mute. The played figure comes
    from :meth:`~avid.core.ports.Speaker.play`'s return value.
    """
    denom = sample_rate * channels * SAMPLE_WIDTH_BYTES
    return len(pcm) * 1000 // denom if denom else 0


def frames_duration_ms(frames: int, *, sample_rate: int) -> int:
    """Milliseconds of *frames* PCM frames at *sample_rate*. Floors.

    The frame is the unit a sound device counts in — ALSA's ``write()`` returns frames
    accepted, not bytes — so this is the conversion an adapter needs to answer
    :meth:`~avid.core.ports.Speaker.play` in the milliseconds the port promises.
    Channel-independent by definition: a frame is one sample *per channel*.
    """
    return frames * 1000 // sample_rate if sample_rate else 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """A single camera frame (SDS §3.9.1).

    Returned by :meth:`~avid.core.ports.Camera.capture`. ``format`` names the
    pixel layout (e.g. ``"RGB888"``) so the consumer need not guess the SDK's.
    """

    data: bytes
    width: int
    height: int
    format: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Detection:
    """One face, seen once (SDS §3.9.1, ADR-013).

    What :meth:`~avid.core.ports.FaceDetector.detect` returns, per face, for the
    frame it was handed. Two fields and no more: everything a detection library
    additionally offers — landmarks, keypoints, tracking ids, identity embeddings
    — stays on the adapter's side of the port, because the application does not
    need it and a port shaped by the model's output would be the inversion
    (§3.9.1) running backwards.

    ``confidence`` is this **frame's** score for this face, in ``[0, 1]``. It is
    not a decision and carries no notion of presence: whether a person *is here*
    is a judgement over time, made by the pure filter in
    :mod:`avid.domain.vision`, and comparing this number against a threshold is
    that filter's business rather than the detector's.
    """

    confidence: float
    box: BBox


@dataclass(frozen=True, slots=True, kw_only=True)
class DisplayFrame:
    """A rendered face, ready to push to a screen (SDS §3.9.1).

    Passed to :meth:`~avid.core.ports.Display.render`. It is a *frame*, not a
    screen: the port never promised a framebuffer, only pixels — which is why a
    real adapter can render offscreen and push RGB565 over ``spidev`` while a
    fake writes a PNG, both behind the identical port (AVID-11).
    """

    pixels: bytes
    width: int
    height: int
    format: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CameraCaps:
    """What a camera can actually do — for capability negotiation (SDS §3.9.3).

    Read via :attr:`~avid.core.ports.Camera.capabilities` so services adapt to
    the rig they were given rather than assuming one.
    """

    width: int
    height: int
    fps: int


# Explicit because ``BBox`` and ``Axis`` are re-exports (see the module docstring):
# without it, ruff reads the imports as unused and the vocabulary loses two members
# to a lint fix.
__all__ = [
    "SAMPLE_WIDTH_BYTES",
    "AudioChunk",
    "Axis",
    "BBox",
    "CameraCaps",
    "Detection",
    "DisplayFrame",
    "Frame",
    "frames_duration_ms",
    "pcm_duration_ms",
]
