"""Contract suite for the ``Camera`` port (AVID-51, SDS §3.9.1, §3.9.3, §14.4, §14.8).

A port's contract test runs against *every* adapter, so a fake can never quietly drift
from the real thing (P6). The shared tier is parametrized over the M2.0 hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case
skips off the Pi and, on the Pi, exercises the real :class:`Picamera2Camera` — the same
seam ``test_display.py`` uses.

Assertions are made *through the port*: a captured :class:`~avid.core.hal.Frame` is
well-formed against the adapter's own :attr:`~avid.core.ports.Camera.capabilities`
(dimensions, byte length, format), never pixel-exact — what the sensor saw is a human's
call, not an assertion's (SDS §14.8). The FakeCamera-specific tail covers the parts of
the fake that only it can prove: the scriptable presence flag and frame replay.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from avid.adapters.camera import FakeCamera
from avid.core.hal import Frame
from avid.core.ports import Camera

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# Small on the fake so a synthetic frame is cheap; the real sensor uses its own geometry.
_FAKE_WIDTH, _FAKE_HEIGHT, _FAKE_FPS = 64, 48, 5
_REAL_WIDTH, _REAL_HEIGHT, _REAL_FPS = 640, 480, 5
_CHANNELS = 3


# --- shared contract: every Camera adapter must satisfy it -------------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
async def camera(request: pytest.FixtureRequest) -> AsyncIterator[Camera]:
    """Every Camera adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The camera is started before the test and stopped after, uniformly for both adapters
    — the real one must be started to capture. The ``"real"`` case skips off the Pi via
    :func:`skip_off_pi`; on the Pi it constructs :class:`Picamera2Camera`, the seam
    AVID-50 laid (this replaces the placeholder skip other suites still carry).
    """
    cam: Camera
    if request.param == "fake":
        cam = FakeCamera(width=_FAKE_WIDTH, height=_FAKE_HEIGHT, fps=_FAKE_FPS)
    else:
        skip_off_pi()
        # Imported here, not at module top: picamera2 is apt-only and absent off the Pi,
        # so only the on-Pi "real" branch ever touches it (P5, ADR-008).
        from avid.adapters.camera import Picamera2Camera

        cam = Picamera2Camera(width=_REAL_WIDTH, height=_REAL_HEIGHT, fps=_REAL_FPS)
    await cam.start()
    try:
        yield cam
    finally:
        await cam.stop()


def test_adapter_satisfies_the_camera_port(camera: Camera) -> None:
    assert isinstance(camera, Camera)


async def test_capture_returns_a_frame_matching_capabilities(camera: Camera) -> None:
    """The captured frame is well-formed against the adapter's own caps (SDS §3.9.3):
    right dimensions, a non-empty ``RGB888`` payload of exactly the implied byte length."""
    caps = camera.capabilities
    frame = await camera.capture()

    assert isinstance(frame, Frame)
    assert (frame.width, frame.height) == (caps.width, caps.height)
    assert frame.format == "RGB888"
    assert frame.data  # non-empty
    assert len(frame.data) == caps.width * caps.height * _CHANNELS


async def test_start_and_stop_are_idempotent_safe(camera: Camera) -> None:
    """Double start/stop must not raise — the fixture already started it once (SDS §3.9.1).

    Capture still works after a redundant start, and a second stop is a clean no-op."""
    await camera.start()
    frame = await camera.capture()
    assert len(frame.data) > 0
    await camera.stop()
    await camera.stop()


async def test_capture_does_not_block_the_loop(camera: Camera) -> None:
    """Smoke test for P8: capture awaits (a thread hop for the real one) and returns —
    it must not hang or raise on a started camera."""
    await camera.capture()


# --- FakeCamera-specific: the simulator's scriptable behaviour (SDS §14.8) ---


async def test_person_present_flag_changes_the_frame() -> None:
    """The scriptable presence flag is observable in the frame, so behaviour tests can
    drive "someone is here" vs "no one is here" deterministically (SDS §14.8)."""
    cam = FakeCamera(width=_FAKE_WIDTH, height=_FAKE_HEIGHT, fps=_FAKE_FPS)
    cam.person_present = False
    absent = await cam.capture()
    cam.person_present = True
    present = await cam.capture()
    assert absent.data != present.data


async def test_captures_counter_counts_up() -> None:
    cam = FakeCamera(width=_FAKE_WIDTH, height=_FAKE_HEIGHT, fps=_FAKE_FPS)
    assert cam.captures == 0
    await cam.capture()
    await cam.capture()
    assert cam.captures == 2


async def test_scripted_frames_replay_in_order_and_cycle() -> None:
    """Given a fixed script of frames, the fake hands them out in order and wraps around
    — enough to replay a canned clip through the port without a video decoder."""
    scripted = [
        Frame(data=b"\x01", width=1, height=1, format="RGB888"),
        Frame(data=b"\x02", width=1, height=1, format="RGB888"),
    ]
    cam = FakeCamera(
        width=_FAKE_WIDTH, height=_FAKE_HEIGHT, fps=_FAKE_FPS, frames=scripted
    )
    assert (await cam.capture()).data == b"\x01"
    assert (await cam.capture()).data == b"\x02"
    assert (await cam.capture()).data == b"\x01"  # cycles
