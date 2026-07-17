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
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioChunk:
    """A slice of PCM audio crossing the mic/speaker ports (SDS §3.9.1).

    Exchanged by :meth:`~avid.core.ports.Microphone.stream` (captured) and
    :meth:`~avid.core.ports.Speaker.play` (played back).
    """

    pcm: bytes
    sample_rate: int
    channels: int


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


@dataclass(frozen=True, slots=True, kw_only=True)
class Axis:
    """One servo axis a rig exposes — for capability negotiation (SDS §3.9.3).

    Read via :attr:`~avid.core.ports.Servo.axes` so the gesture engine stays
    axis-agnostic and the same code runs on a 1-servo rig, a 2-servo rig, or a
    simulator with six (ADR-009). ``min_deg``/``max_deg`` describe the rig's
    reach; enforcing them is the *adapter's* job (see :meth:`Servo.move_to`).
    """

    name: str
    channel: int
    min_deg: float
    max_deg: float
