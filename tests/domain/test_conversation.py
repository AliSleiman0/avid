"""Tests for the ``conversation.*`` events and the ``TokenUsage`` value (#99)."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from avid.domain import (
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Event,
    TokenUsage,
)

# --- the five events --------------------------------------------------------

_ENVELOPE = {
    "event_id": uuid4(),
    "correlation_id": uuid4(),
    "timestamp_ms": 123,
    "monotonic_ns": 456,
    "source": "ConversationService",
}


def _make(cls: type[Event], **payload: object) -> Event:
    return cls(**{**_ENVELOPE, **payload})  # type: ignore[arg-type]


def test_turn_started_carries_name_and_payload() -> None:
    e = _make(ConversationTurnStarted, initiator="user")
    assert e.name == "conversation.turn_started"
    assert isinstance(e, ConversationTurnStarted)
    assert e.initiator == "user"


def test_user_transcribed_carries_name_and_payload() -> None:
    e = _make(ConversationUserTranscribed, text="hello pico", is_approximate=False)
    assert e.name == "conversation.user_transcribed"
    assert e.text == "hello pico"
    assert e.is_approximate is False


def test_assistant_responded_carries_name_and_payload() -> None:
    e = _make(ConversationAssistantResponded, text="hi there", item_id="item_7")
    assert e.name == "conversation.assistant_responded"
    assert e.text == "hi there"
    assert e.item_id == "item_7"


def test_turn_ended_carries_name_and_payload() -> None:
    usage = TokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=40)
    e = _make(ConversationTurnEnded, duration_ms=2500, usage=usage)
    assert e.name == "conversation.turn_ended"
    assert e.duration_ms == 2500
    assert e.usage is usage


def test_session_lost_carries_name_and_payload() -> None:
    e = _make(ConversationSessionLost, cause="network", was_mid_turn=True)
    assert e.name == "conversation.session_lost"
    assert e.cause == "network"
    assert e.was_mid_turn is True


_EVENT_CASES = [
    (ConversationTurnStarted, {"initiator": "user"}),
    (ConversationUserTranscribed, {"text": "x", "is_approximate": False}),
    (ConversationAssistantResponded, {"text": "x", "item_id": "i"}),
    (
        ConversationTurnEnded,
        {
            "duration_ms": 1,
            "usage": TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
        },
    ),
    (ConversationSessionLost, {"cause": "network", "was_mid_turn": False}),
]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_event_is_frozen(cls: type[Event], payload: dict[str, object]) -> None:
    e = _make(cls, **payload)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.source = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_event_is_slotted(cls: type[Event], payload: dict[str, object]) -> None:
    assert not hasattr(_make(cls, **payload), "__dict__")


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_event_is_kw_only(cls: type[Event], payload: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        cls(uuid4())  # type: ignore[call-arg]


# --- the TokenUsage value ---------------------------------------------------


def test_token_usage_carries_counts() -> None:
    u = TokenUsage(input_tokens=1000, cached_input_tokens=760, output_tokens=200)
    assert u.input_tokens == 1000
    assert u.cached_input_tokens == 760
    assert u.output_tokens == 200


def test_token_usage_uncached_is_the_expensive_slice() -> None:
    u = TokenUsage(input_tokens=1000, cached_input_tokens=760, output_tokens=200)
    assert u.uncached_input_tokens == 240


def test_token_usage_total_is_input_plus_output() -> None:
    u = TokenUsage(input_tokens=1000, cached_input_tokens=760, output_tokens=200)
    assert u.total_tokens == 1200


def test_token_usage_adds_field_wise() -> None:
    a = TokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=40)
    b = TokenUsage(input_tokens=10, cached_input_tokens=5, output_tokens=3)
    total = a + b
    assert total == TokenUsage(
        input_tokens=110, cached_input_tokens=85, output_tokens=43
    )
    # accumulation must not mutate the operands (frozen values)
    assert a.input_tokens == 100
    assert b.output_tokens == 3


def test_token_usage_add_rejects_other_types() -> None:
    u = TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
    with pytest.raises(TypeError):
        _ = u + 5  # type: ignore[operator]


def test_token_usage_is_frozen_and_slotted() -> None:
    u = TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
    assert not hasattr(u, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.output_tokens = 2  # type: ignore[misc]
