"""Tests for the Event envelope and the P4 naming validator (AVID-6)."""

from __future__ import annotations

import dataclasses
from typing import ClassVar
from uuid import uuid4

import pytest

from avid.domain import (
    EVENT_DOMAINS,
    Event,
    SystemDegradedEntered,
    SystemDegradedExited,
    validate_event_name,
)

# The full v1 event catalog (SDS §9.1.3). The validator must accept every one —
# including the non-"-ed" names presence_lost / session_lost (irregular past
# "lost") and the system.shutting_down anomaly.
CATALOG = [
    "system.started",
    "system.shutting_down",
    "system.handler_failed",
    "system.degraded_entered",
    "system.degraded_exited",
    "audio.speech_started",
    "audio.speech_ended",
    "audio.playback_started",
    "audio.playback_finished",
    "conversation.turn_started",
    "conversation.user_transcribed",
    "conversation.assistant_responded",
    "conversation.turn_ended",
    "conversation.session_lost",
    "affect.changed",
    "state.transitioned",
    "vision.presence_gained",
    "vision.presence_lost",
    "vision.face_detected",
    "memory.fact_stored",
    "memory.fact_superseded",
    "memory.fact_deleted",
    "memory.recall_completed",
    "behavior.trigger_fired",
    "behavior.proactive_delivered",
    "behavior.proactive_suppressed",
    "behavior.trigger_disabled",
    "motion.gesture_started",
    "motion.gesture_completed",
    "motion.gesture_preempted",
]

BAD_NAMES = [
    "display.set_emotion",  # non-domain + a command wearing an event's clothes
    "affect.change",  # present tense
    "affect.changed.extra",  # too many segments
    "Affect.Changed",  # not lowercase
    "affect_changed",  # missing the dot
    "unknown.happened",  # unknown domain
    "affect.",  # empty verb
    ".changed",  # empty domain
    "",  # empty
]


def make_event(**overrides: object) -> Event:
    fields: dict[str, object] = {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": "test",
    }
    fields.update(overrides)
    return Event(**fields)  # type: ignore[arg-type]


# --- the envelope -----------------------------------------------------------


def test_carries_all_five_fields() -> None:
    eid, cid = uuid4(), uuid4()
    e = Event(
        event_id=eid,
        correlation_id=cid,
        timestamp_ms=123,
        monotonic_ns=456,
        source="AudioService",
    )
    assert e.event_id == eid
    assert e.correlation_id == cid
    assert e.timestamp_ms == 123
    assert e.monotonic_ns == 456
    assert e.source == "AudioService"


def test_is_frozen() -> None:
    e = make_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.source = "mutated"  # type: ignore[misc]


def test_is_slotted() -> None:
    e = make_event()
    assert not hasattr(e, "__dict__")


def test_is_kw_only() -> None:
    with pytest.raises(TypeError):
        Event(uuid4(), uuid4(), 1, 2, "test")  # type: ignore[call-arg]


# --- the naming validator ---------------------------------------------------


def test_domains_are_the_nine() -> None:
    assert len(EVENT_DOMAINS) == 9
    assert "display" not in EVENT_DOMAINS
    assert "expression" not in EVENT_DOMAINS


@pytest.mark.parametrize("name", CATALOG)
def test_validator_accepts_the_catalog(name: str) -> None:
    validate_event_name(name)  # must not raise


@pytest.mark.parametrize("name", BAD_NAMES)
def test_validator_rejects(name: str) -> None:
    with pytest.raises(ValueError):
        validate_event_name(name)


# --- subclass name binding --------------------------------------------------


def test_valid_subclass_carries_name_and_stays_frozen() -> None:
    @dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
    class AffectChanged(Event):
        name: ClassVar[str] = "affect.changed"
        tier: int

    e = AffectChanged(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=1,
        monotonic_ns=2,
        source="AffectService",
        tier=1,
    )
    assert e.name == "affect.changed"
    assert e.tier == 1
    assert not hasattr(e, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.tier = 2  # type: ignore[misc]


def test_degraded_events_carry_their_catalogued_names_and_payloads() -> None:
    """The two ``system.degraded_*`` facts ConversationService publishes (#102): catalogued
    names (SDS §9.1.3) and their distinct payloads — a cause on entry, a downtime on exit."""
    base = dict(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=1,
        monotonic_ns=2,
        source="ConversationService",
    )
    entered = SystemDegradedEntered(**base, cause="network")  # type: ignore[arg-type]
    exited = SystemDegradedExited(**base, downtime_s=4.5)  # type: ignore[arg-type]
    assert entered.name == "system.degraded_entered"
    assert entered.cause == "network"
    assert exited.name == "system.degraded_exited"
    assert exited.downtime_s == 4.5


def test_subclass_with_bad_name_raises_at_class_creation() -> None:
    with pytest.raises(ValueError):

        @dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
        class Bad(Event):
            name: ClassVar[str] = "display.set_emotion"
            x: int


def test_slotted_subclass_does_not_break_on_python_311() -> None:
    """Regression guard (issue #27): defining an ``Event`` subclass must not raise on
    Python 3.11. ``slots=True`` recreates ``Event``, and a zero-arg ``super()`` in
    ``__init_subclass__`` would then read a stale ``__class__`` cell and raise
    ``TypeError`` on 3.11 (fixed on CPython 3.12). This whole module already imports a
    slotted subclass (``SystemHandlerFailed``), so on 3.11 the bug shows up at *import*;
    this test states the guarantee where a reader can see it.

    Declared locally on purpose — the guarantee is about *defining* a subclass, so it has
    to happen inside the test. It used to be named ``StateTransitioned``; that is a real
    class now (AVID-69, ``avid/domain/state.py``), so this probe got a neutral name to
    keep exactly one ``StateTransitioned`` in the repo."""

    @dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
    class _SlotsProbe(Event):
        name: ClassVar[str] = "system.probed"

    assert issubclass(_SlotsProbe, Event)
    assert _SlotsProbe.name == "system.probed"
