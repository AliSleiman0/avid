"""``BBox`` and the three ``vision.*`` events (#219, SDS §9.1.3).

The events are transcription — §9.1.3 specified them normatively long before any code — so
these tests are guards against *drift*, not proofs of behaviour: that the catalogued names and
payloads are what shipped, that ``presence_lost`` still passes the P4 validator via the
existing irregular-past allowlist rather than a new exception, and that ``BBox`` is reachable
under the ``core.hal`` spelling the ports use as well as its ``domain`` one.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from avid.core.hal import BBox as HalBBox
from avid.domain import (
    BBox,
    VisionFaceDetected,
    VisionPresenceGained,
    VisionPresenceLost,
)
from avid.domain.events import _IRREGULAR_PAST, validate_event_name

_ENVELOPE_FIELDS = frozenset(
    {"event_id", "correlation_id", "timestamp_ms", "monotonic_ns", "source"}
)


def _envelope(source: str = "PresenceService") -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": source,
    }


# --- BBox -------------------------------------------------------------------


def test_bbox_is_frozen_slotted_and_kw_only() -> None:
    """Like every other HAL value type: an adapter cannot mutate a caller's box, and a
    positional constructor call cannot silently transpose (x, y) with (w, h)."""
    box = BBox(x=10, y=20, w=30, h=40)
    assert not hasattr(box, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        box.x = 0  # type: ignore[misc]
    with pytest.raises(TypeError):
        BBox(10, 20, 30, 40)  # type: ignore[misc]


def test_bbox_area_is_the_product_and_orders_boxes() -> None:
    """``area`` is how ``largest_bbox`` gets chosen (#223). It lives on the box so two callers
    cannot each write their own ``w * h`` and drift apart on the degenerate case."""
    assert BBox(x=0, y=0, w=30, h=40).area == 1200
    assert BBox(x=99, y=99, w=1, h=1).area == 1
    assert BBox(x=0, y=0, w=0, h=40).area == 0
    faces = [BBox(x=0, y=0, w=10, h=10), BBox(x=5, y=5, w=40, h=40)]
    assert max(faces, key=lambda b: b.area).w == 40


def test_bbox_does_not_validate_its_numbers() -> None:
    """Deliberate: clipping to the frame is the detector's job (ADR-013). A domain value that
    raised on an odd box would turn one bad frame into a crashed capture loop — the failure
    mode #223 explicitly guards against, arriving through the back door."""
    assert BBox(x=-5, y=-5, w=0, h=0).area == 0


def test_core_hal_reexports_the_same_bbox() -> None:
    """``avid.core.hal.BBox`` is the spelling ports and adapters use, but it must be the
    *same class*, not a parallel definition — the re-export exists to keep the P1 layers
    contract at zero exceptions (SDS §3.6.5), not to have two boxes."""
    assert HalBBox is BBox


# --- the three events -------------------------------------------------------


def test_presence_gained_carries_its_catalogued_name_and_payload() -> None:
    event = VisionPresenceGained(**_envelope(), confidence=0.82)  # type: ignore[arg-type]
    assert event.name == "vision.presence_gained"
    assert event.confidence == 0.82
    assert [
        f.name for f in dataclasses.fields(event) if f.name not in _ENVELOPE_FIELDS
    ] == ["confidence"]


def test_presence_lost_carries_its_catalogued_name_and_payload() -> None:
    event = VisionPresenceLost(**_envelope(), absent_for_s=21.4)  # type: ignore[arg-type]
    assert event.name == "vision.presence_lost"
    assert event.absent_for_s == 21.4
    assert [
        f.name for f in dataclasses.fields(event) if f.name not in _ENVELOPE_FIELDS
    ] == ["absent_for_s"]


def test_face_detected_carries_its_catalogued_name_and_payload() -> None:
    box = BBox(x=100, y=80, w=120, h=120)
    event = VisionFaceDetected(**_envelope(), count=2, largest_bbox=box)  # type: ignore[arg-type]
    assert event.name == "vision.face_detected"
    assert event.count == 2
    assert event.largest_bbox is box
    assert [
        f.name for f in dataclasses.fields(event) if f.name not in _ENVELOPE_FIELDS
    ] == [
        "count",
        "largest_bbox",
    ]


def test_the_events_are_facts_not_requests() -> None:
    """P4. No ``should_wake``, no ``action``, no ``target`` — that the robot should wake is the
    state table's conclusion from the fact (#224), never the event's instruction. This test is
    the thing that notices if a later issue finds it convenient to add one."""
    imperative = {"should", "action", "command", "request", "wake", "sleep", "target"}
    for event_type in (VisionPresenceGained, VisionPresenceLost, VisionFaceDetected):
        payload = {
            f.name
            for f in dataclasses.fields(event_type)
            if f.name not in _ENVELOPE_FIELDS
        }
        assert not any(word in field for field in payload for word in imperative), (
            event_type
        )


# --- the P4 validator -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["vision.presence_gained", "vision.presence_lost", "vision.face_detected"],
)
def test_validator_accepts_all_three_names(name: str) -> None:
    validate_event_name(name)  # must not raise


def test_presence_lost_passes_via_the_existing_irregular_past_allowlist() -> None:
    """Not by a new exception being added for it. ``lost`` was already in ``_IRREGULAR_PAST``
    before this issue existed, and ``vision.presence_lost`` is one of the two names the
    allowlist was written for. If someone later "tidies" that set, this is what notices —
    which is worth more than the assertion above, because the name would still *look* fine."""
    assert "lost" in _IRREGULAR_PAST
    verb = "presence_lost".rsplit("_", 1)[-1]
    assert not verb.endswith("ed")
    assert verb in _IRREGULAR_PAST
