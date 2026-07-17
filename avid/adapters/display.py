"""Display adapters — the fake that *is* the simulator (AVID-13, SDS §3.9.2, §14.8).

:class:`FakeDisplay` implements the :class:`~avid.core.ports.Display` port by writing
each rendered face to a numbered PNG under ``.artifacts/frames/``. That is the whole
point of P6: the fake ships in ``adapters/`` (not ``tests/``) and *is* the simulator, so
the simulator can never drift from the real system — it is the real system with a
different back end. A real display adapter (a later hardware issue) renders offscreen and
pushes RGB565 over ``spidev``; this one writes a PNG. Same port, both.

Pixel-exact rendering is deliberately **not** unit-tested (SDS §14.8): a human eyeballs
the frames in the CI artifact, which beats an image-diff test that fails on every
intentional face change. Tests here assert the *count* (:attr:`FakeDisplay.frames_rendered`)
and that well-formed PNGs land on disk — never what the face looks like.

Stdlib only: the PNG encoder is a ~two-dozen-line ``zlib`` + ``struct`` function, so the
fake pulls in no image library. ``core`` stays lean the same way (see :mod:`avid.core.hal`),
and the runtime dependency set stays at just ``pydantic``.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

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
