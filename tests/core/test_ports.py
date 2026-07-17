"""Shape-and-contract tests for the port Protocols (AVID-11).

Protocols have no runtime behaviour, so these assert the *contract*: that the
already-merged bus fits the ``EventBus`` port unchanged, that every port is
runtime-checkable, that the negotiation members and the documented invariants
(clamping, frame-not-screen) are actually present. The type checker enforces the
signatures; these guard the properties a type checker cannot see.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from avid.core.event_bus import AsyncioEventBus, _SystemClock
from avid.core.hal import AudioChunk, Axis, CameraCaps, DisplayFrame, Frame
from avid.core.ports import (
    Camera,
    Clock,
    Display,
    EventBus,
    Microphone,
    Servo,
    Speaker,
)

ALL_PORTS = (Camera, Clock, Display, EventBus, Microphone, Servo, Speaker)


def test_asyncio_event_bus_satisfies_the_eventbus_port() -> None:
    """The load-bearing test: the AVID-9/10 bus fits the port with zero changes.

    The annotation is what mypy checks; the ``isinstance`` is the runtime proof.
    """
    bus: EventBus = AsyncioEventBus()
    assert isinstance(bus, EventBus)


@pytest.mark.parametrize("port", ALL_PORTS)
def test_every_port_is_runtime_checkable(port: type) -> None:
    assert getattr(port, "_is_runtime_protocol", False) is True


def test_negotiation_members_exist() -> None:
    """§3.9.3 capability negotiation — keeps ADR-009 open without conditionals."""
    assert hasattr(Camera, "capabilities")
    assert hasattr(Servo, "axes")


def test_servo_move_to_documents_adapter_side_clamping() -> None:
    """AC + SDS §3.9.1: clamping is the adapter's job, not the caller's."""
    doc = Servo.move_to.__doc__ or ""
    assert "clamp" in doc.lower()
    assert "adapter" in doc.lower()


def test_display_render_takes_a_frame_not_a_screen() -> None:
    """AC: ``Display.render`` takes a ``DisplayFrame``."""
    hints = get_type_hints(Display.render)
    assert hints["frame"] is DisplayFrame


def test_clock_port_requires_sleep() -> None:
    """The ``Clock`` port is a strict superset of the bus's ``_SystemClock``
    stand-in: it adds ``sleep``, which ``_SystemClock`` lacks — so the bus's
    default time source is *not* a full ``Clock`` (that arrives with AVID-12)."""
    assert hasattr(_SystemClock, "now")
    assert hasattr(_SystemClock, "monotonic_ns")
    assert not isinstance(_SystemClock(), Clock)


def test_hal_value_types_are_frozen() -> None:
    """An adapter must not be able to mutate a caller's frame/chunk."""
    cases = (
        (AudioChunk(pcm=b"\x00\x01", sample_rate=16_000, channels=1), "sample_rate"),
        (Frame(data=b"\x00", width=320, height=240, format="RGB888"), "width"),
        (DisplayFrame(pixels=b"\x00", width=240, height=240, format="RGB888"), "width"),
        (CameraCaps(width=1280, height=720, fps=30), "fps"),
        (Axis(name="pan", channel=0, min_deg=-90.0, max_deg=90.0), "name"),
    )
    for value, field in cases:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, 0)
