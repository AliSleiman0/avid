"""The M0 walking-skeleton gate (AVID-15, PMP §5.1).

The milestone's headline proof, as a permanent regression test: *an event published
in a test travels through the bus to a fake display, which asserts a frame.* It wires
the real :class:`~avid.core.event_bus.AsyncioEventBus` to the real
:class:`~avid.adapters.display.FakeDisplay` — no mocks, no stubs — publishes one
event, and asserts a well-formed PNG frame landed on the other side.

The renderer here is **demo-scoped**: at M0 nothing renders a face for real — that became
``ExpressionService``'s job at M3 (wired in ``avid/main.py``, proven end to end by
``tests/e2e/test_m3_gate.py``). This handler exists to prove the *mechanism*, not to ship a
feature; now that the real path exists it is exercised by that gate and ExpressionService's
own tests, and this M0 gate stays as its own milestone artifact.

In-process and signal-free, so unlike ``tests/e2e/test_boot.py`` it runs on every
platform including the Windows dev box. Draining is deterministic — the handler sets an
``asyncio.Event`` the test awaits — the injected-Event pattern from
``tests/core/test_lifecycle.py``, never a sleep.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from uuid import uuid4

from avid.adapters.display import FakeDisplay
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import DisplayFrame
from avid.domain import SystemStarted

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A tiny 2x2 RGB888 frame — the smallest thing that proves a real face crossed the port.
_FRAME_WIDTH = 2
_FRAME_HEIGHT = 2
_DEMO_FRAME = DisplayFrame(
    pixels=b"\x00" * (_FRAME_WIDTH * _FRAME_HEIGHT * 3),
    width=_FRAME_WIDTH,
    height=_FRAME_HEIGHT,
    format="RGB888",
)

# Beyond a generous drain margin, something is wrong (a stuck worker), not slow.
_DRAIN_TIMEOUT_S = 5.0


def _system_started() -> SystemStarted:
    """A real boot event — the honest origin fact, not a throwaway type. Its
    ``correlation_id`` is the turn every downstream event would carry (SDS §9.1.1)."""
    return SystemStarted(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=0,
        monotonic_ns=0,
        source="test_m0_gate",
        adapters={"display": True},
    )


def _ihdr_dimensions(png: bytes) -> tuple[int, int]:
    """Read ``(width, height)`` from the PNG's IHDR (mirrors the Display contract test):
    8-byte signature, then a chunk of 4-byte length + 4-byte type + the data."""
    assert png[:8] == _PNG_SIGNATURE
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


async def test_event_travels_bus_to_a_rendered_frame(tmp_path: Path) -> None:
    """M0 gate: publish one event, and a real frame lands on the fake display.

    The whole walking skeleton in one assertion — bus dispatch, a subscriber, the
    Display port, and the fake-that-is-the-simulator, all cooperating.
    """
    display = FakeDisplay(out_dir=tmp_path)
    bus = AsyncioEventBus()
    rendered = asyncio.Event()

    async def render_face(event: SystemStarted) -> None:
        # Demo-scoped renderer: proves the mechanism, not a shipped ExpressionService.
        await display.render(_DEMO_FRAME)
        rendered.set()

    bus.subscribe(SystemStarted, render_face, name="m0-gate-renderer")

    async with bus:
        assert display.frames_rendered == 0
        await bus.publish(_system_started())
        await asyncio.wait_for(rendered.wait(), timeout=_DRAIN_TIMEOUT_S)

    # The face did exactly one thing, and it left a well-formed PNG behind.
    assert display.frames_rendered == 1
    png = display.frames[0].read_bytes()
    assert png[:8] == _PNG_SIGNATURE
    assert _ihdr_dimensions(png) == (_FRAME_WIDTH, _FRAME_HEIGHT)
