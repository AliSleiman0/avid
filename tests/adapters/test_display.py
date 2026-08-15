"""Adapter tests for :class:`~avid.adapters.display.FramebufferDisplay` (AVID-266).

The contract suite (``tests/contract/test_display.py``) skips the real adapter off the Pi, so
until now **the framebuffer write path was never executed anywhere but the rig** — and the one
contract test that touches it renders exactly once, which no amount of running would have caught
a concurrency defect.

That gap is what this file closes, and it closes it without hardware: ``_write_blocking`` is
plain file I/O with no ``ioctl``, so a temp file pre-sized to the panel's byte count behaves like
``/dev/fb0`` in the one respect that matters here — it is the *fixed size* of a framebuffer that
turns a raced write into ``ENOSPC`` rather than a silently longer file. A regular file will
happily grow instead, so these tests assert the **size** as the fixed-size stand-in for the
device's refusal.

⚠️ What this cannot prove: that a panel lit up. It proves what the adapter *wrote*, which is the
same limit the on-rig ``fbshot.py`` evidence carries.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from avid.adapters.display import FakeDisplay, FramebufferDisplay
from avid.core.hal import DisplayFrame

# The bring-up geometry (Elecrow 3.5" ILI9486, 32bpp XRGB8888): 480 x 320 x 4 = 614400 bytes.
_WIDTH = 480
_HEIGHT = 320
_PANEL_BYTES = _WIDTH * _HEIGHT * 4

# How long the seek-to-write window is held open in the deterministic race test. See
# _widen_the_seek_window for why this exists at all.
_SEEK_WINDOW_S = 0.02


def _frame(value: int = 0x40) -> DisplayFrame:
    return DisplayFrame(
        pixels=bytes([value]) * (_WIDTH * _HEIGHT * 3),
        width=_WIDTH,
        height=_HEIGHT,
        format="RGB888",
    )


# Filesystem reads live in sync helpers, not inline in the async tests: ruff's ASYNC240 rightly
# objects to blocking pathlib calls inside a coroutine, and a per-line suppression would be noise
# around the very lines that carry the meaning. (Spelling the directive out here would itself trip
# ruff's parser, which is a small lesson in why the suppressions are worth avoiding.)
def _size_of(path: Path) -> int:
    return path.stat().st_size


def _bytes_of(path: Path) -> bytes:
    return path.read_bytes()


def _png_count(directory: Path) -> int:
    return len(list(directory.glob("frame_*.png")))


def _widen_the_seek_window(
    monkeypatch: pytest.MonkeyPatch, display: FramebufferDisplay
) -> None:
    """Hold open the gap between ``lseek`` and the ``write`` that belongs to it.

    Scoped to *this* adapter's fd, so patching a module-global cannot slow anything else pytest
    happens to be doing on another thread. Sleeping in a worker thread is not a P8 concern — the
    event loop is not involved — and 20 ms is far wider than the real window while still being
    invisible in the suite's runtime."""
    real_lseek = os.lseek

    def slow_lseek(fd: int, position: int, whence: int) -> int:
        result = real_lseek(fd, position, whence)
        if fd == display._fd:
            time.sleep(_SEEK_WINDOW_S)
        return result

    monkeypatch.setattr(os, "lseek", slow_lseek)


@pytest.fixture
def panel(tmp_path: Path) -> Path:
    """A file pre-sized to the panel, standing in for the fixed-size ``/dev/fb0``."""
    device = tmp_path / "fb0"
    device.write_bytes(b"\x00" * _PANEL_BYTES)
    return device


async def test_a_single_render_fills_the_panel_exactly(panel: Path) -> None:
    """The baseline the concurrency test is measured against: one render writes the whole panel
    and not one byte more. If this drifts, the size assertions below stop meaning anything."""
    display = FramebufferDisplay(device=str(panel), width=_WIDTH, height=_HEIGHT)
    await display.render(_frame())

    assert _size_of(panel) == _PANEL_BYTES
    assert display.frames_rendered == 1


async def test_the_bytes_on_the_device_are_the_bytes_that_were_composed(
    panel: Path,
) -> None:
    """No byte is translated on the way to the device.

    Pixel value ``10`` is ``0x0A``, and a framebuffer is full of them. Writing through a handle
    opened in text mode expands every one to ``\\r\\n``, which on Windows is the default for
    ``os.open`` — so this asserts the panel image byte-for-byte rather than only its length.

    Not a hypothetical: it is how the ``O_BINARY`` flag came to be in ``_ensure_open_locked``.
    The concurrency test below overran the panel by exactly 460800 bytes, which is the ``0x0A``
    count of a single frame, and the first reading of that was another lost race."""
    display = FramebufferDisplay(device=str(panel), width=_WIDTH, height=_HEIGHT)
    frame = _frame(0x0A)

    await display.render(frame)

    written = _bytes_of(panel)
    assert len(written) == _PANEL_BYTES
    assert written == display._compose(frame), "the device received translated bytes"


async def test_concurrent_renders_do_not_write_past_the_panel(
    panel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AVID-266: two renders in flight at once must not run off the end of the framebuffer.

    This is not a hypothetical pairing. ``ExpressionService`` declares two subscriptions —
    ``affect.changed`` and ``state.transitioned`` — that both end in ``render``, and the bus gives
    each subscriber its own worker task by design, so one state change routinely produces exactly
    this. On the rig it surfaced as::

        OSError: [Errno 28] No space left on device

    with 51G free, because a framebuffer device has no room past ``stride * height`` and the old
    ``seek(0); write(); flush()`` let one thread advance the shared offset to EOF between another
    thread's seek and its write.

    ⚠️ **The window is widened on purpose, and that is what makes this a test rather than a
    lottery.** Measured while writing it: with the lock removed, two concurrent renders never
    lost the race on this machine, eight never did either, and it took *thirty-two* before the
    overrun appeared — which would have made this a test that quietly stops proving anything on a
    slower box or a smaller CI runner. Sleeping inside ``lseek`` holds open the exact gap the
    defect lives in (between the seek and the write it belongs to), so two renders are enough and
    the outcome does not depend on the scheduler. The window is real; only its width is ours.

    ⚠️ A regular file cannot return ``ENOSPC`` — it grows instead. So the assertion is on the
    resulting **size**: a file longer than one panel means a write started somewhere other than
    offset 0, which is the same defect the device reports by refusing."""
    display = FramebufferDisplay(device=str(panel), width=_WIDTH, height=_HEIGHT)
    # One render first, so the fd is cached and the concurrent pair below SHARES it. Without this
    # the two renders race in _ensure_open instead and each opens its own fd — which has its own
    # file offset, so the panel comes out the right size and this test would quietly prove
    # nothing. That is also the real sequence on the rig: the face had been rendering for minutes
    # before the first ENOSPC.
    await display.render(_frame())
    _widen_the_seek_window(monkeypatch, display)

    await asyncio.gather(*(display.render(_frame(v)) for v in (0x10, 0x80)))

    assert _size_of(panel) == _PANEL_BYTES, (
        "a render wrote past the end of the panel — on /dev/fb0 this is the AVID-266 ENOSPC"
    )
    assert display.frames_rendered == 3


async def test_many_concurrent_renders_stay_within_the_panel(panel: Path) -> None:
    """The same property without the widened window, at the concurrency that first exposed it.

    Kept alongside the deterministic test rather than instead of it: this one runs the adapter
    exactly as it ships, with nothing patched, so it also covers the ordinary path the widened
    test necessarily perturbs."""
    display = FramebufferDisplay(device=str(panel), width=_WIDTH, height=_HEIGHT)

    await asyncio.gather(*(display.render(_frame(v % 256)) for v in range(32)))

    assert _size_of(panel) == _PANEL_BYTES
    assert display.frames_rendered == 32


async def test_the_device_is_opened_once_under_concurrent_renders(
    panel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The *second* race in the same method, and the one that hides the first.

    ``_ensure_open`` is a check-then-act: two worker threads could both find ``None`` and both
    open the device, leaking every fd but the last. Worth its own test because of what it does to
    the ENOSPC one — two separate fds have two separate file offsets, so a first-ever pair of
    concurrent renders comes out *correct*, and the overrun only begins once one fd is cached and
    shared. The fd leak is why AVID-266 looked intermittent rather than immediate.

    Counting opens rather than comparing the cached handle: after a double-open the adapter still
    holds exactly one fd (the last writer wins), so the leak is invisible from ``_fd``."""
    display = FramebufferDisplay(device=str(panel), width=_WIDTH, height=_HEIGHT)
    real_open = os.open
    opens: list[str] = []

    def counting_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == str(panel):
            opens.append(str(path))
            time.sleep(_SEEK_WINDOW_S)  # widen the check-then-act, as above
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", counting_open)

    await asyncio.gather(*(display.render(_frame()) for _ in range(4)))

    assert opens == [str(panel)], (
        f"the framebuffer was opened {len(opens)} times — fds leaked"
    )
    assert display.frames_rendered == 4


async def test_concurrent_fake_renders_do_not_collide_on_a_sequence_number(
    tmp_path: Path,
) -> None:
    """The fake had the same defect with a quieter symptom (AVID-266).

    ``seq = len(self.frames)`` was read *before* the await and appended *after* it, so two
    concurrent renders both claimed the same number: the second overwrote the first's PNG and
    ``frames`` recorded a duplicate path. Nothing raised.

    This matters more than it looks. P6 makes the fake the simulator, and nearly every test in
    the suite asserts against ``FakeDisplay.frames`` — a fake still carrying the bug the real
    adapter just lost is drift in the direction that hides regressions."""
    display = FakeDisplay(out_dir=tmp_path, resolution=(_WIDTH, _HEIGHT))

    await asyncio.gather(*(display.render(_frame(v)) for v in range(8)))

    assert len(set(display.frames)) == 8, "two renders claimed the same sequence number"
    assert display.frames_rendered == 8
    assert _png_count(tmp_path) == 8
