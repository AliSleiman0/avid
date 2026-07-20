"""Contract suite for the ``Display`` port (AVID-13, SDS §3.9.2, §14.8).

A port's contract test runs against *every* adapter, so a fake can never quietly drift
from the real thing (P6). Only :class:`FakeDisplay` exists today — the real spidev
adapter is a later hardware issue — so the shared tier is parametrized over the fake
alone, shaped so the real one drops in later.

Per SDS §14.8 these tests assert what the face *did* (a well-formed PNG landed, the count
is right), never what it *looks like*: pixel-exactness is eyeballed in the CI artifact,
not diffed here. Every display writes to ``tmp_path`` so no test litters ``.artifacts/``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from avid.adapters.display import FakeDisplay
from avid.core.hal import DisplayFrame
from avid.core.ports import Display

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def make_frame(*, width: int = 4, height: int = 3, fmt: str = "RGB888") -> DisplayFrame:
    channels = {"RGB888": 3, "RGBA8888": 4}[fmt]
    return DisplayFrame(
        pixels=b"\x00" * (width * height * channels),
        width=width,
        height=height,
        format=fmt,
    )


def ihdr_dimensions(png: bytes) -> tuple[int, int]:
    """Read ``(width, height)`` out of the PNG's IHDR — the first chunk after the
    8-byte signature: 4-byte length, 4-byte type, then width/height as big-endian u32."""
    assert png[:8] == _PNG_SIGNATURE
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


# --- shared contract: every Display adapter must satisfy it -----------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def display(request: pytest.FixtureRequest, tmp_path: Path) -> Display:
    """Every Display adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs
    :class:`~avid.adapters.display.FramebufferDisplay`, which writes XRGB8888 to the panel's
    framebuffer device (AVID-55). ``/dev/fb0`` is the bring-up rig's SPI panel (headless, so
    it grabbed index 0); the class is imported here, not at module scope, so this suite still
    collects off-Pi where the fake alone runs.
    """
    if request.param == "fake":
        return FakeDisplay(out_dir=tmp_path)
    skip_off_pi()
    from avid.adapters.display import FramebufferDisplay

    return FramebufferDisplay(device="/dev/fb0", width=480, height=320)


def test_adapter_satisfies_the_display_port(display: Display) -> None:
    assert isinstance(display, Display)


def test_resolution_is_a_pair_of_ints(display: Display) -> None:
    width, height = display.resolution
    assert isinstance(width, int)
    assert isinstance(height, int)


async def test_render_completes_without_blocking_the_loop(display: Display) -> None:
    """Smoke test for P8: render awaits its threaded write and returns — it must not hang
    or raise on a valid frame."""
    await display.render(make_frame())


# --- FakeDisplay-specific: the artifact-writing contract --------------------


async def test_frames_rendered_counts_up(tmp_path: Path) -> None:
    display = FakeDisplay(out_dir=tmp_path)
    assert display.frames_rendered == 0
    await display.render(make_frame())
    await display.render(make_frame())
    assert display.frames_rendered == 2


async def test_each_render_writes_a_new_numbered_file(tmp_path: Path) -> None:
    display = FakeDisplay(out_dir=tmp_path)
    await display.render(make_frame())
    await display.render(make_frame())

    assert display.frames == [
        tmp_path / "frame_00000.png",
        tmp_path / "frame_00001.png",
    ]
    for path in display.frames:
        assert path.exists()


async def test_rendered_records_the_frame_objects_in_order(tmp_path: Path) -> None:
    """``rendered`` answers "which frame", ``frames`` answers "which file" (AVID-72).

    Identity, not equality: a caller that precomputes and caches its frames needs to prove the
    *cached* object reached the port, which decoded pixels could never distinguish from an
    identical-looking rebuild.
    """
    display = FakeDisplay(out_dir=tmp_path)
    first, second = make_frame(width=2, height=2), make_frame(width=3, height=1)
    await display.render(first)
    await display.render(second)

    assert display.rendered[0] is first
    assert display.rendered[1] is second
    assert len(display.rendered) == len(display.frames)


async def test_a_rejected_frame_is_recorded_nowhere(tmp_path: Path) -> None:
    """The two records stay in lockstep: a frame that failed to encode was never shown, so it
    belongs in neither list."""
    display = FakeDisplay(out_dir=tmp_path)
    with pytest.raises(ValueError, match="cannot encode format"):
        await display.render(
            DisplayFrame(pixels=b"\x00\x00", width=1, height=1, format="RGB565")
        )

    assert display.rendered == []
    assert display.frames == []


async def test_written_file_is_a_wellformed_png(tmp_path: Path) -> None:
    """Structural, not pixel-exact (§14.8): it is a valid PNG whose IHDR matches the
    frame. What the face looks like is a human's job, not an assertion's."""
    display = FakeDisplay(out_dir=tmp_path)
    frame = make_frame(width=8, height=5)
    await display.render(frame)

    png = display.frames[0].read_bytes()
    assert png[:8] == _PNG_SIGNATURE
    assert ihdr_dimensions(png) == (frame.width, frame.height)


async def test_renders_rgba_frames(tmp_path: Path) -> None:
    """The 4-channel / colour-type-6 path encodes just as well as RGB888."""
    display = FakeDisplay(out_dir=tmp_path)
    frame = make_frame(width=6, height=2, fmt="RGBA8888")
    await display.render(frame)

    png = display.frames[0].read_bytes()
    assert ihdr_dimensions(png) == (6, 2)


async def test_unknown_format_raises(tmp_path: Path) -> None:
    display = FakeDisplay(out_dir=tmp_path)
    bad = DisplayFrame(pixels=b"\x00\x00", width=1, height=1, format="RGB565")
    with pytest.raises(ValueError, match="cannot encode format"):
        await display.render(bad)


def test_out_dir_is_created_on_construction(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "frames"
    assert not target.exists()
    FakeDisplay(out_dir=target)
    assert target.is_dir()
