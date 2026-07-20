"""Face composition — an ``Affect`` becomes ``RGB888`` pixels (AVID-70, ADR-012/SDS §3.6.4).

The missing middle of the display path. Both :class:`~avid.core.ports.Display` adapters
have existed since AVID-13/55 and neither had anything to render: nothing in the repo
produced a :class:`~avid.core.hal.DisplayFrame`. This module does, and it is the whole of
"the face" — a pure function, no I/O, no async, no ports, no clock.

**Stdlib only** (ADR-012). Faces are sprites, not vector art (SDS §2.4 — "design for the
pixel grid"), so composition is array arithmetic and needs no drawing library. Runtime
dependencies stay ``pydantic`` alone. The technique is the one AVID-55's ``_to_xrgb8888``
already established: write whole **scanline spans** as contiguous ``bytearray`` slice
assignments and let C do the copying. A per-pixel Python loop over a 480×320 RGB888 buffer
(460,800 bytes) costs ~100 ms — it would blow both the O4 150 ms budget and P8's 50 ms
slow-callback gate. Span fills cost well under a millisecond.

It lives in ``core`` rather than ``services`` deliberately: AVID-73 activated the P5
``service-independence`` contract, and a shared drawing module sitting among the services
would entangle it for no benefit. Here it sits beside :class:`~avid.core.hal.DisplayFrame`,
which is what it returns, and ``core -> domain`` is the direction the layers contract wants.

**Faces are data, not branches.** A face is an ordered tuple of paint ops over a background,
rendered back to front, and there are exactly two ops: ``rect`` and ``ellipse``. Adding or
retuning an affect is an edit to :data:`_FACES` — never a new ``if``/``match`` arm. The
painter's-algorithm consequence worth knowing: **an op painted in the background colour
erases**, which is how curved mouths happen. A smile is an ellipse with a slightly higher
ellipse in the background colour laid over it, leaving an upturned crescent; a frown is the
same pair with the eraser nudged *down* instead. No arc maths, no third primitive.

Pixel-exact rendering is deliberately not asserted (SDS §14.8) — a human eyeballs the PNGs
``FakeDisplay`` writes. The tests check structure, determinism, distinctness, and speed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil, floor, sqrt
from typing import Literal, TypeAlias

from avid.core.hal import DisplayFrame
from avid.domain import Affect

RGB: TypeAlias = tuple[int, int, int]

_CHANNELS = 3  # RGB888 — one of the two formats the display adapters accept (_FORMATS)
_FORMAT = "RGB888"


# --- palette ----------------------------------------------------------------------------
# Dark backgrounds on purpose: the panel is a small emissive LCD on a desk, so a lit face on
# near-black reads far better than dark-on-light, and it draws less power.

_NIGHT: RGB = (8, 10, 18)  # the default backdrop
_DUSK: RGB = (10, 16, 38)  # cooler, for SAD
_DIM: RGB = (5, 5, 9)  # darker still, for SLEEPING
_CYAN: RGB = (90, 220, 255)  # the resting eye colour
_BRIGHT: RGB = (150, 245, 255)  # attentive — LISTENING
_WARM: RGB = (255, 205, 115)  # HAPPY
_COOL: RGB = (120, 170, 235)  # SAD
_MUTED: RGB = (85, 125, 165)  # SLEEPING — dim, but still legible against _DIM


@dataclass(frozen=True, slots=True, kw_only=True)
class _Shape:
    """One paint op, in **fractions of the surface** — never pixels.

    ``cx``/``w`` are fractions of the width, ``cy``/``h`` fractions of the height, so the
    same table renders correctly centred at 240×240 and at 480×320 (AC-5). There is no
    hardcoded resolution anywhere in this module, which matters because ``FakeDisplay``'s
    bare default is 240×240 while the real panel is 480×320.

    A shape whose ``colour`` is its face's background is an **eraser** — see the module
    docstring. Erasers are drawn deliberately oversized (``w`` > 1.0) so they clip against
    the surface edge rather than leaving a seam a fraction of a pixel wide.
    """

    kind: Literal["rect", "ellipse"]
    cx: float
    cy: float
    w: float
    h: float
    colour: RGB


@dataclass(frozen=True, slots=True, kw_only=True)
class _Face:
    """A background plus the ops painted over it, back to front."""

    background: RGB
    shapes: tuple[_Shape, ...]


_EYE_LEFT = 0.32
_EYE_RIGHT = 0.68
_EYE_LINE = 0.38


def _eyes(
    *,
    cy: float,
    w: float,
    h: float,
    colour: RGB,
    kind: Literal["rect", "ellipse"] = "ellipse",
) -> tuple[_Shape, ...]:
    """A symmetric pair of eyes at the standard eye positions. Most faces want exactly this.

    Also used to build the *erasers* that arch or half-lid a pair of eyes, which is why it
    takes a colour rather than assuming one.
    """
    return tuple(
        _Shape(kind=kind, cx=cx, cy=cy, w=w, h=h, colour=colour)
        for cx in (_EYE_LEFT, _EYE_RIGHT)
    )


# The eight faces. Every ``Affect`` member must appear: :func:`render_face` looks the affect
# up directly and a missing one raises ``KeyError`` **on purpose**. There is deliberately no
# default face — a silently-substituted fallback would make a forgotten affect invisible,
# which is the failure mode worth being loud about. The exhaustive test over ``Affect`` is
# what keeps this table complete, exactly as ``test_no_undocumented_transitions`` keeps the
# transition table complete.
_FACES: Mapping[Affect, _Face] = {
    Affect.IDLE: _Face(
        background=_NIGHT,
        shapes=(
            *_eyes(cy=_EYE_LINE, w=0.14, h=0.20, colour=_CYAN),
            _Shape(kind="rect", cx=0.5, cy=0.68, w=0.18, h=0.035, colour=_CYAN),
        ),
    ),
    Affect.LISTENING: _Face(
        background=_NIGHT,
        shapes=(
            # Wide open: attention reads as eye area more than anything else.
            *_eyes(cy=_EYE_LINE, w=0.17, h=0.29, colour=_BRIGHT),
            _Shape(kind="rect", cx=0.5, cy=0.70, w=0.11, h=0.03, colour=_BRIGHT),
        ),
    ),
    Affect.THINKING: _Face(
        background=_NIGHT,
        shapes=(
            # Asymmetric eye line — one brow up. The cheapest legible "considering".
            # Kept near full size: shrink these and the offset stops reading as a raised
            # brow and starts reading as two eyes that failed to line up.
            _Shape(kind="ellipse", cx=_EYE_LEFT, cy=0.34, w=0.14, h=0.16, colour=_CYAN),
            _Shape(
                kind="ellipse", cx=_EYE_RIGHT, cy=0.43, w=0.14, h=0.16, colour=_CYAN
            ),
            _Shape(kind="rect", cx=0.60, cy=0.70, w=0.10, h=0.03, colour=_CYAN),
        ),
    ),
    Affect.SPEAKING: _Face(
        background=_NIGHT,
        shapes=(
            *_eyes(cy=_EYE_LINE, w=0.14, h=0.20, colour=_CYAN),
            # An open mouth: a full ellipse, no eraser over it.
            _Shape(kind="ellipse", cx=0.5, cy=0.70, w=0.21, h=0.17, colour=_CYAN),
        ),
    ),
    Affect.HAPPY: _Face(
        background=_NIGHT,
        shapes=(
            # Eyes as upward arches — ellipse, then an eraser nudged down over it.
            *_eyes(cy=_EYE_LINE, w=0.15, h=0.19, colour=_WARM),
            *_eyes(cy=_EYE_LINE + 0.035, w=0.15, h=0.19, colour=_NIGHT),
            # Smile: eraser sits *above* centre, leaving the upturned crescent.
            _Shape(kind="ellipse", cx=0.5, cy=0.62, w=0.36, h=0.28, colour=_WARM),
            _Shape(kind="ellipse", cx=0.5, cy=0.575, w=0.32, h=0.25, colour=_NIGHT),
        ),
    ),
    Affect.SAD: _Face(
        background=_DUSK,
        shapes=(
            # Lowered, half-lidded eyes: an eraser clipped across the top.
            *_eyes(cy=0.42, w=0.14, h=0.19, colour=_COOL),
            _Shape(kind="rect", cx=0.5, cy=0.35, w=1.4, h=0.10, colour=_DUSK),
            # Frown: same pair as the smile, eraser nudged *down* instead of up.
            _Shape(kind="ellipse", cx=0.5, cy=0.74, w=0.34, h=0.26, colour=_COOL),
            _Shape(kind="ellipse", cx=0.5, cy=0.785, w=0.30, h=0.23, colour=_DUSK),
        ),
    ),
    Affect.CONFUSED: _Face(
        background=_NIGHT,
        shapes=(
            # One eye wide, one squinting. The squint is a *wide, short* ellipse, not a
            # small round one: shrunk in both axes it stops reading as a squint and starts
            # reading as a rendering glitch — which is exactly how the first cut looked on
            # the contact sheet.
            _Shape(
                kind="ellipse", cx=_EYE_LEFT, cy=_EYE_LINE, w=0.17, h=0.22, colour=_CYAN
            ),
            _Shape(
                kind="ellipse", cx=_EYE_RIGHT, cy=0.37, w=0.16, h=0.075, colour=_CYAN
            ),
            # A tilted mouth, faked as two offset rects — with only axis-aligned primitives
            # a stair-step is how a slant gets drawn, and at this size it reads as one.
            # The two bars must overlap horizontally and sit one bar-height apart, or the
            # stair separates into two floating dashes instead of one slanted mouth.
            _Shape(kind="rect", cx=0.44, cy=0.715, w=0.13, h=0.035, colour=_CYAN),
            _Shape(kind="rect", cx=0.55, cy=0.682, w=0.13, h=0.035, colour=_CYAN),
            # A floating dot standing in for a question mark.
            _Shape(kind="rect", cx=0.78, cy=0.20, w=0.045, h=0.07, colour=_CYAN),
        ),
    ),
    Affect.SLEEPING: _Face(
        background=_DIM,
        shapes=(
            # Closed: flat slits sitting lower than the open eye line. Dim, but not *too*
            # dim — the first cut used a darker muted tone and the whole face vanished into
            # the background. Asleep should read as asleep, not as a dead panel.
            *_eyes(cy=0.45, w=0.16, h=0.035, colour=_MUTED, kind="rect"),
            _Shape(kind="rect", cx=0.5, cy=0.68, w=0.08, h=0.03, colour=_MUTED),
        ),
    ),
}


# --- the two primitives -------------------------------------------------------------------


def _span(buf: bytearray, width: int, y: int, x0: int, x1: int, colour: bytes) -> None:
    """Paint one horizontal run ``[x0, x1)`` on scanline *y*, clipped to the surface.

    The single place pixels are written. One contiguous slice assignment per call, which is
    a C-speed memcpy — this is the whole performance story (AC-2).

    The empty-run guard is load-bearing, not defensive noise: a ``bytearray`` slice
    assignment whose stop precedes its start does **not** write nothing, it *inserts* and
    resizes the buffer. Without the guard a shape that clips away to nothing would silently
    lengthen the frame instead of drawing none of it.
    """
    if x0 < 0:
        x0 = 0
    if x1 > width:
        x1 = width
    if x1 <= x0:
        return
    off = (y * width + x0) * _CHANNELS
    buf[off : off + (x1 - x0) * _CHANNELS] = colour * (x1 - x0)


def _fill_rect(
    buf: bytearray, width: int, height: int, shape: _Shape, colour: bytes
) -> None:
    """Paint an axis-aligned rectangle: one span per scanline."""
    w = round(shape.w * width)
    h = round(shape.h * height)
    x0 = round(shape.cx * width) - w // 2
    y0 = round(shape.cy * height) - h // 2
    for y in range(max(0, y0), min(height, y0 + h)):
        _span(buf, width, y, x0, x0 + w, colour)


def _fill_ellipse(
    buf: bytearray, width: int, height: int, shape: _Shape, colour: bytes
) -> None:
    """Paint a filled axis-aligned ellipse: one span per scanline, one ``sqrt`` each.

    Solving the ellipse for its half-width at each *y* is what keeps this O(height) instead
    of O(width x height) — the difference between sub-millisecond and ~100 ms at 480x320.
    """
    cx = shape.cx * width
    cy = shape.cy * height
    rx = shape.w * width / 2
    ry = shape.h * height / 2
    if rx <= 0 or ry <= 0:
        return  # degenerate at tiny surfaces; also stops a divide-by-zero on ry below
    for y in range(max(0, floor(cy - ry)), min(height, ceil(cy + ry))):
        dy = (y + 0.5 - cy) / ry
        if dy * dy >= 1.0:
            continue  # this scanline grazes past the ellipse
        half = rx * sqrt(1.0 - dy * dy)
        _span(buf, width, y, round(cx - half), round(cx + half), colour)


# --- the public function ------------------------------------------------------------------


def render_face(affect: Affect, *, width: int, height: int) -> DisplayFrame:
    """Compose *affect* into an ``RGB888`` :class:`~avid.core.hal.DisplayFrame`.

    Pure and deterministic: the same arguments always produce byte-identical pixels. Raises
    :class:`KeyError` for an affect missing from :data:`_FACES` — see that table for why
    there is no default face.

    Synchronous by design. It is CPU-only with no I/O, and fast enough (well under a
    millisecond at 480x320) that running it on the event loop does not risk P8's 50 ms
    slow-callback gate; handing it to a thread would cost more in overhead than it saves.
    """
    face = _FACES[affect]
    buf = bytearray(bytes(face.background) * (width * height))
    for shape in face.shapes:
        colour = bytes(shape.colour)
        if shape.kind == "rect":
            _fill_rect(buf, width, height, shape, colour)
        else:
            _fill_ellipse(buf, width, height, shape, colour)
    return DisplayFrame(pixels=bytes(buf), width=width, height=height, format=_FORMAT)
