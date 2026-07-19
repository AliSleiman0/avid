"""Display adapters — the fake that *is* the simulator, and the real panel (AVID-13/55).

Two implementations of the :class:`~avid.core.ports.Display` port, both behind the one
contract suite (P6, SDS §14.4):

* :class:`FakeDisplay` writes each rendered face to a numbered PNG under
  ``.artifacts/frames/``. That is the whole point of P6: the fake ships in ``adapters/``
  (not ``tests/``) and *is* the simulator, so the simulator can never drift from the real
  system — it is the real system with a different back end.
* :class:`FramebufferDisplay` pushes a frame to a Linux framebuffer device (AVID-55). The
  physical panel is an Elecrow 3.5″ SPI ILI9486 on the ``piscreen,drm`` overlay — a real
  DRM/KMS device that surfaces as ``/dev/fb0`` (bring-up verified 2026-07-19), **32bpp
  XRGB8888** (little-endian ``0x00RRGGBB``, stride = ``width*4``). The device path is
  *injected*, never hardcoded — on this headless Pi the SPI panel grabbed framebuffer
  index 0, but with HDMI attached it would not, so assuming ``fb1`` (or ``fb0``) is a bug.

Pixel-exact rendering is deliberately **not** unit-tested (SDS §14.8): a human eyeballs
the frames — the PNG artifact for the fake, the lit panel for the real one — which beats
an image-diff test that fails on every intentional face change. Tests assert the *count*
(:attr:`FakeDisplay.frames_rendered`) and that a well-formed PNG lands on disk, or that a
real ``render`` completes without blocking the loop — never what the face looks like.

Stdlib only: the PNG encoder is a ~two-dozen-line ``zlib`` + ``struct`` function and the
framebuffer path is plain file I/O plus a C-speed slice reorder, so neither adapter pulls
in an image library. ``core`` stays lean the same way (see :mod:`avid.core.hal`), and the
runtime dependency set stays at just ``pydantic``.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path
from typing import IO

from avid.core.hal import DisplayFrame

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Frame pixel format -> (bytes per pixel, PNG colour-type). Truecolour (2) and truecolour
# with alpha (6) are the two the system needs; nothing produces DisplayFrames yet, and
# "RGB888" is the natural baseline (see the Frame example in avid.core.hal).
_FORMATS: dict[str, tuple[int, int]] = {
    "RGB888": (3, 2),
    "RGBA8888": (4, 6),
}


def _chunk(tag: bytes, data: bytes) -> bytes:
    """One PNG chunk: length, type, data, CRC32 over type+data (PNG spec §5)."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _encode_png(frame: DisplayFrame) -> bytes:
    """Encode a :class:`DisplayFrame` as a PNG byte string, stdlib only.

    Raises :class:`ValueError` for an unrecognised ``frame.format`` — better a loud,
    early failure than a silently corrupt artifact a human later squints at.
    """
    try:
        channels, colour_type = _FORMATS[frame.format]
    except KeyError:
        raise ValueError(
            f"FakeDisplay cannot encode format {frame.format!r}; "
            f"known formats are {sorted(_FORMATS)}"
        ) from None

    stride = frame.width * channels
    # Each scanline is prefixed with filter-type byte 0x00 (no filtering) before deflate.
    raw = b"".join(
        b"\x00" + frame.pixels[y * stride : (y + 1) * stride]
        for y in range(frame.height)
    )
    ihdr = struct.pack(">IIBBBBB", frame.width, frame.height, 8, colour_type, 0, 0, 0)
    return b"".join(
        (
            _PNG_SIGNATURE,
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", zlib.compress(raw)),
            _chunk(b"IEND", b""),
        )
    )


class FakeDisplay:
    """The :class:`~avid.core.ports.Display` fake (P6): every ``render`` writes a PNG.

    Constructed only by the composition root or a test fixture (P3). ``out_dir`` defaults
    to ``.artifacts/frames/`` — already git-ignored and, once CI exists (AVID-4), uploaded
    as a build artifact. ``resolution`` is injected (the port exposes it as a device
    property); it describes the face LCD, independent of any one frame's size.
    """

    def __init__(
        self,
        *,
        out_dir: Path = Path(".artifacts/frames"),
        resolution: tuple[int, int] = (240, 240),
    ) -> None:
        self._out_dir = out_dir
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._resolution = resolution
        # Written paths, in render order — the assertable record of what the face did.
        self.frames: list[Path] = []

    @property
    def resolution(self) -> tuple[int, int]:
        """``(width, height)`` of the display surface (SDS §3.9.1)."""
        return self._resolution

    @property
    def frames_rendered(self) -> int:
        """How many frames have been rendered — the AC's assertable counter."""
        return len(self.frames)

    async def render(self, frame: DisplayFrame) -> None:
        """Write ``frame`` to the next numbered PNG.

        The sequence number is read synchronously (single event loop, so no interleave),
        then the encode-and-write runs on a worker thread: file I/O is blocking and must
        never touch the loop (P8, ADR-002) — which is also exactly what the real spidev
        adapter will do.
        """
        seq = len(self.frames)
        path = self._out_dir / f"frame_{seq:05d}.png"
        png = _encode_png(frame)
        await asyncio.to_thread(path.write_bytes, png)
        self.frames.append(path)


def _to_xrgb8888(frame: DisplayFrame) -> bytes:
    """Convert a frame's pixels to little-endian XRGB8888 (``0x00RRGGBB``) bytes.

    The panel is 32bpp XRGB8888, so each pixel is four bytes ``B G R 0x00`` in memory. The
    channel reorder is a C-speed extended-slice assignment on a ``bytearray`` — no per-pixel
    Python loop, no numpy — so a whole 480×320 frame converts in one shot. Raises
    :class:`ValueError` for an unknown ``frame.format`` (a loud early failure beats a garbled
    panel), reusing :data:`_FORMATS`' channel counts so the fake and the real one agree on
    what a format means.
    """
    try:
        channels = _FORMATS[frame.format][0]
    except KeyError:
        raise ValueError(
            f"FramebufferDisplay cannot convert format {frame.format!r}; "
            f"known formats are {sorted(_FORMATS)}"
        ) from None
    px = frame.pixels
    out = bytearray(frame.width * frame.height * 4)  # X (high) byte left 0x00
    out[0::4] = px[2::channels]  # B
    out[1::4] = px[1::channels]  # G
    out[2::4] = px[0::channels]  # R
    return bytes(out)


class FramebufferDisplay:
    """The real :class:`~avid.core.ports.Display` on the Pi: writes to a framebuffer (AVID-55).

    ``device`` (a framebuffer path such as ``/dev/fb0``) and the panel ``resolution`` are
    injected (P7): the adapter never reaches for a path or assumes an index — a headless SPI
    panel grabs ``fb0``, but with HDMI attached it would not, so hardcoding either is a bug.
    Each :meth:`render` converts the frame to XRGB8888, blits it centered into a black
    full-panel buffer, and writes the whole buffer to the device — the blocking write on a
    worker thread, because framebuffer I/O must never touch the loop (P8, ADR-002), exactly as
    :meth:`FakeDisplay.render` threads its PNG write. The device handle is opened lazily on the
    worker thread on first render and cached: constructing the adapter touches no hardware (so
    the module imports off-Pi), a display is always open, and the port has no ``stop`` to close
    it.
    """

    def __init__(self, *, device: str, width: int, height: int) -> None:
        self._device = device
        self._resolution = (width, height)
        self._width = width
        self._height = height
        self._stride = width * 4  # 32bpp XRGB8888
        self._fb: IO[bytes] | None = None
        # The assertable record of how many frames the panel was handed.
        self.frames_rendered = 0

    @property
    def resolution(self) -> tuple[int, int]:
        """``(width, height)`` of the panel surface (SDS §3.9.1)."""
        return self._resolution

    async def render(self, frame: DisplayFrame) -> None:
        """Blit ``frame`` onto the panel.

        The XRGB8888 conversion and centered blit build a full-panel buffer synchronously
        (pure CPU, no I/O); the blocking write to the framebuffer device then runs on a worker
        thread (P8), so the DRM/framebuffer syscall never stalls the event loop.
        """
        buf = self._compose(frame)
        await asyncio.to_thread(self._write_blocking, buf)
        self.frames_rendered += 1

    def _compose(self, frame: DisplayFrame) -> bytes:
        """Center ``frame`` (converted to XRGB8888) into a black full-panel buffer, clipped.

        Clipping keeps an oversized or off-panel frame from overflowing the buffer; when the
        frame matches the panel the offsets are zero and it is a straight full-frame copy.
        """
        pixels = _to_xrgb8888(frame)
        panel = bytearray(self._stride * self._height)  # black (all 0x00)
        src_stride = frame.width * 4
        x0 = (self._width - frame.width) // 2
        y0 = (self._height - frame.height) // 2
        for row in range(frame.height):
            dy = y0 + row
            if dy < 0 or dy >= self._height:
                continue
            sx = max(0, -x0)  # first source column that lands on-panel
            dx = x0 + sx
            span = min(frame.width - sx, self._width - dx)
            if span <= 0:
                continue
            src_off = row * src_stride + sx * 4
            dst_off = dy * self._stride + dx * 4
            panel[dst_off : dst_off + span * 4] = pixels[src_off : src_off + span * 4]
        return bytes(panel)

    def _write_blocking(self, buf: bytes) -> None:
        """Write a full-panel XRGB8888 buffer to the framebuffer (blocking; worker thread)."""
        fb = self._ensure_open()
        fb.seek(0)
        fb.write(buf)
        fb.flush()

    def _ensure_open(self) -> IO[bytes]:
        # Opened lazily on the worker thread (mirrors AlsaSpeaker/Picamera2Camera): "r+b"
        # overwrites the existing device in place, never truncating it as "wb" would.
        if self._fb is None:
            self._fb = open(self._device, "r+b")
        return self._fb
