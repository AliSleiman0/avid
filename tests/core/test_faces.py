"""Face composition: structure, determinism, distinctness, speed (AVID-70).

What is *not* here is as deliberate as what is: no pixel-exact assertions (SDS §14.8). A
face is meant to change whenever we retune it, so an image-diff test would fail on every
intentional edit and teach the team to regenerate the baseline without looking. These tests
assert the properties that must hold whatever the face looks like — and
:func:`test_writes_a_contact_sheet_for_eyeballing` produces the artifact a human actually
judges it by.
"""

from __future__ import annotations

import ast
import time
from itertools import combinations
from pathlib import Path

import pytest

from avid.adapters import FakeDisplay
from avid.core.faces import _FACES, _span, render_face
from avid.core.hal import DisplayFrame
from avid.domain import Affect

# The real panel (SDS §2.4) and a square surface that is *not* it — every test that cares
# about geometry runs against both, which is what proves the geometry is resolution-relative.
PANEL = (480, 320)
SQUARE = (240, 240)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- AC-1: every affect renders ----------------------------------------------------------


@pytest.mark.parametrize("affect", list(Affect))
@pytest.mark.parametrize(("width", "height"), [PANEL, SQUARE])
def test_every_affect_renders_a_well_formed_rgb888_frame(
    affect: Affect, width: int, height: int
) -> None:
    """All eight affects compose. Exhaustive over ``Affect`` on purpose: this is the test
    that keeps ``_FACES`` complete, so adding a ninth affect without giving it a face fails
    here rather than at 1 a.m. on the Pi."""
    frame = render_face(affect, width=width, height=height)

    assert isinstance(frame, DisplayFrame)
    assert frame.format == "RGB888"  # one of the two formats the adapters accept
    assert frame.width == width
    assert frame.height == height
    assert len(frame.pixels) == width * height * 3


def test_the_table_covers_the_affect_enum_exactly() -> None:
    """No missing face, and no orphan entry for an affect that no longer exists."""
    assert set(_FACES) == set(Affect)


def test_unknown_affect_raises_rather_than_substituting_a_default() -> None:
    """A missing face must be loud. A silent fallback would render a plausible-looking
    *wrong* face, which is indistinguishable from a working system until someone eventually
    notices the robot never looks sad."""

    class _NotAnAffect:
        pass

    with pytest.raises(KeyError):
        render_face(_NotAnAffect(), width=64, height=64)  # type: ignore[arg-type]  # deliberately wrong


# --- AC-3: deterministic -----------------------------------------------------------------


@pytest.mark.parametrize("affect", list(Affect))
def test_rendering_is_deterministic(affect: Affect) -> None:
    """Same input, byte-identical output. Cheap to guarantee today and worth locking in:
    the moment a face depends on a clock or a random jitter, frame caching and the artifact
    diff both stop meaning anything."""
    width, height = PANEL
    first = render_face(affect, width=width, height=height)
    second = render_face(affect, width=width, height=height)
    assert first.pixels == second.pixels


# --- AC-4: the eight faces are actually distinct -----------------------------------------


def test_all_eight_faces_are_pairwise_distinct() -> None:
    """The mechanical stand-in for "the face actually changed" (pixel-exactness is
    deliberately untested, SDS §14.8).

    Reported per colliding *pair* rather than as a set-length mismatch: when a table edit
    accidentally makes two affects identical, "HAPPY and IDLE are the same" is a one-line
    fix and "expected 8, got 7" is a bisect.

    Byte-inequality is a floor, not a ceiling. Two faces differing by a single pixel pass
    this and would still be a failed issue — that is what the contact sheet below is for.
    """
    width, height = PANEL
    rendered = {a: render_face(a, width=width, height=height).pixels for a in Affect}

    collisions = [
        f"{a.name} == {b.name}"
        for a, b in combinations(Affect, 2)
        if rendered[a] == rendered[b]
    ]
    assert not collisions, "these affects render identically: " + ", ".join(collisions)


# --- AC-5: geometry is resolution-relative ------------------------------------------------


def test_faces_module_hardcodes_no_resolution() -> None:
    """Geometry must be fractions of the surface, never pixels.

    ``FakeDisplay``'s bare default is 240x240 while the panel is 480x320, so a hardcoded
    dimension would render a correct-looking face on a laptop and an off-centre one on the
    robot — a bug that survives every other test in this file.
    """
    source = (REPO_ROOT / "avid" / "core" / "faces.py").read_text(encoding="utf-8")
    # Walked as an AST rather than grepped: the prose in that module names the panel size
    # repeatedly and should keep doing so. What must not appear is a resolution baked into
    # an actual numeric literal.
    banned = {240, 320, 480}
    found = sorted(
        {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and node.value in banned
        }
    )
    assert not found, (
        f"avid/core/faces.py hardcodes {found} as numeric literals — face geometry must be "
        f"fractions of width/height (AVID-70 AC-5)"
    )


@pytest.mark.parametrize("affect", list(Affect))
def test_a_face_differs_between_the_two_surfaces_but_stays_proportional(
    affect: Affect,
) -> None:
    """A square surface and the panel produce differently-shaped buffers from one table."""
    panel = render_face(affect, width=PANEL[0], height=PANEL[1])
    square = render_face(affect, width=SQUARE[0], height=SQUARE[1])
    assert len(panel.pixels) == PANEL[0] * PANEL[1] * 3
    assert len(square.pixels) == SQUARE[0] * SQUARE[1] * 3


@pytest.mark.parametrize("affect", list(Affect))
def test_renders_at_absurdly_small_sizes_without_crashing(affect: Affect) -> None:
    """Fractional geometry rounds to zero-width and zero-height shapes at tiny surfaces.
    Those must clip away to nothing, not raise and not corrupt the buffer."""
    frame = render_face(affect, width=3, height=2)
    assert len(frame.pixels) == 3 * 2 * 3


def test_renders_a_zero_sized_surface_as_an_empty_frame() -> None:
    """The degenerate case, asserted rather than left to chance.

    Every shape collapses to zero extent here, which is the only way to reach the radius
    guard in ``_fill_ellipse`` — and that guard exists to stop a divide-by-zero, not merely
    to skip pointless work. Nothing in the system asks for a 0x0 face today; the point is
    that a misconfigured ``[display]`` geometry should produce an empty frame rather than a
    ``ZeroDivisionError`` from inside the render path.
    """
    frame = render_face(Affect.IDLE, width=0, height=0)
    assert frame.pixels == b""


# --- the span primitive's guards ----------------------------------------------------------


def test_span_ignores_a_run_that_clips_away_to_nothing() -> None:
    """Directly exercised because the consequence of dropping this guard is subtle and bad.

    A ``bytearray`` slice assignment whose stop precedes its start does not write nothing —
    it *inserts*, resizing the buffer. A frame that silently grew by a few bytes would sail
    past every length assertion made before it and land as a garbled panel.
    """
    buf = bytearray(b"\x00" * (4 * 3))
    _span(buf, 4, 0, 3, 1, b"\xff\xff\xff")  # stop before start
    _span(buf, 4, 0, -5, -2, b"\xff\xff\xff")  # entirely off the left edge
    assert buf == bytearray(b"\x00" * (4 * 3))
    assert len(buf) == 4 * 3


def test_span_clips_to_the_surface_edges() -> None:
    """An oversized run paints up to the edge and no further — erasers rely on this."""
    buf = bytearray(b"\x00" * (4 * 3))
    _span(buf, 4, 0, -10, 99, b"\xff\xff\xff")
    assert buf == bytearray(b"\xff" * (4 * 3))


# --- AC-2: span fills, not a per-pixel loop -----------------------------------------------


def test_a_panel_sized_face_composes_well_under_the_budget() -> None:
    """Best-of-5, not a single shot: one timing assertion on a shared CI box is a flake
    generator, while best-of-N still measures the thing that matters — whether the
    algorithm is span-based at all.

    The margin is what makes this robust. Span fills land in the sub-millisecond range; a
    per-pixel Python loop over 460,800 bytes is ~100 ms and misses this by 4x. There is no
    plausible implementation that lands *near* 25 ms, so scheduler noise cannot flip it.
    """
    width, height = PANEL
    best = min(
        _elapsed_ms(lambda: render_face(Affect.HAPPY, width=width, height=height))
        for _ in range(5)
    )
    assert best < 25.0, (
        f"480x320 face took {best:.1f} ms — is something looping per pixel?"
    )


def _elapsed_ms(fn: object) -> float:
    start = time.perf_counter()
    fn()  # type: ignore[operator]  # a zero-arg callable; typing it fully adds nothing here
    return (time.perf_counter() - start) * 1000.0


# --- AC-6: the artifact a human actually looks at -----------------------------------------


def _blit(
    dst: bytearray, dst_width: int, src: DisplayFrame, *, at_x: int, at_y: int
) -> None:
    """Copy a frame into a larger buffer, one scanline slice at a time.

    Lives in the test, not in ``faces.py``: tiling is a convenience for eyeballing, and the
    robot never composes a contact sheet.
    """
    for row in range(src.height):
        src_off = row * src.width * 3
        dst_off = ((at_y + row) * dst_width + at_x) * 3
        dst[dst_off : dst_off + src.width * 3] = src.pixels[
            src_off : src_off + src.width * 3
        ]


async def test_writes_a_contact_sheet_for_eyeballing() -> None:
    """Render all eight faces through ``FakeDisplay``, then a 4x2 sheet of them.

    SDS §14.8's "look at it" check, made concrete. Every assertion in this file can pass on
    eight faces that are byte-distinct and visually identical; this is the artifact that
    catches that, and the reason it is a test rather than a script is that CI uploads
    ``.artifacts/`` and a reviewer can open the sheet from the build.

    Given its own ``out_dir`` so its numbering never collides with another test's
    ``FakeDisplay``, and rooted at the repo rather than the cwd so it lands in the same
    place however pytest was invoked.
    """
    out_dir = REPO_ROOT / ".artifacts" / "frames" / "faces"
    display = FakeDisplay(out_dir=out_dir, resolution=PANEL)

    tile_w, tile_h = 240, 160
    faces = [render_face(a, width=tile_w, height=tile_h) for a in Affect]
    for frame in faces:
        await display.render(frame)

    columns, rows = 4, 2
    sheet_w, sheet_h = tile_w * columns, tile_h * rows
    sheet = bytearray(sheet_w * sheet_h * 3)
    for index, frame in enumerate(faces):
        _blit(
            sheet,
            sheet_w,
            frame,
            at_x=(index % columns) * tile_w,
            at_y=(index // columns) * tile_h,
        )
    await display.render(
        DisplayFrame(
            pixels=bytes(sheet), width=sheet_w, height=sheet_h, format="RGB888"
        )
    )

    assert display.frames_rendered == len(Affect) + 1
    assert all(path.exists() and path.stat().st_size > 0 for path in display.frames)
