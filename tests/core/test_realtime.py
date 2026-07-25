"""Tests for the neutral ``RealtimeEvent`` union the ``RealtimeClient`` port yields (#100)."""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from avid.core.hal import AudioChunk
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    RealtimeEvent,
    SessionClosed,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from avid.domain import TokenUsage

_CHUNK = AudioChunk(pcm=b"\x00\x01", sample_rate=24_000, channels=1)


def test_user_transcript_carries_text_and_flag() -> None:
    e = UserTranscript(text="hello pico", is_approximate=True)
    assert e.text == "hello pico"
    assert e.is_approximate is True


def test_assistant_audio_chunk_carries_pcm_and_item() -> None:
    e = AssistantAudioChunk(chunk=_CHUNK, item_id="item_7")
    assert e.chunk is _CHUNK
    assert e.item_id == "item_7"


def test_assistant_transcript_carries_text_and_item() -> None:
    e = AssistantTranscript(text="hi there", item_id="item_7")
    assert e.text == "hi there"
    assert e.item_id == "item_7"


def test_turn_done_carries_usage() -> None:
    usage = TokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=40)
    e = TurnDone(usage=usage)
    assert e.usage is usage


def test_session_closed_carries_cause() -> None:
    e = SessionClosed(cause="network")
    assert e.cause == "network"


def test_tool_call_requested_carries_call_name_and_arguments() -> None:
    e = ToolCallRequested(call_id="call_0", name="recall", arguments='{"query": "x"}')
    assert e.call_id == "call_0"
    assert e.name == "recall"
    assert e.arguments == '{"query": "x"}'


# --- shared shape: every RealtimeEvent member is a frozen, slotted, kw-only value ---

_CASES = [
    UserTranscript(text="x", is_approximate=False),
    AssistantAudioChunk(chunk=_CHUNK, item_id="i"),
    AssistantTranscript(text="x", item_id="i"),
    ToolCallRequested(call_id="call_0", name="recall", arguments="{}"),
    TurnDone(usage=TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1)),
    SessionClosed(cause="network"),
]


@pytest.mark.parametrize("value", _CASES)
def test_member_is_frozen(value: RealtimeEvent) -> None:
    # Mutate a field the value actually has — the members share none, and assigning a
    # non-existent slot name raises TypeError (not FrozenInstanceError) on 3.11.
    field = dataclasses.fields(value)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, "mutated")


@pytest.mark.parametrize("value", _CASES)
def test_member_is_slotted(value: RealtimeEvent) -> None:
    assert not hasattr(value, "__dict__")


def test_union_covers_all_members() -> None:
    """The alias is the closed set the port yields — a member dropped here is a consumer's
    match silently narrowing (SDS §3.9.1). ``ToolCallRequested`` is the #124 tool-call widening."""
    assert set(get_args(RealtimeEvent)) == {
        UserTranscript,
        AssistantAudioChunk,
        AssistantTranscript,
        ToolCallRequested,
        TurnDone,
        SessionClosed,
    }
