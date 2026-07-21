"""Tests for the ``audio.*`` events and the pre-roll ring buffer (#86)."""

from __future__ import annotations

import dataclasses
from uuid import UUID, uuid4

import pytest

from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    Event,
)

# --- the four events --------------------------------------------------------

_ENVELOPE = {
    "event_id": uuid4(),
    "correlation_id": uuid4(),
    "timestamp_ms": 123,
    "monotonic_ns": 456,
    "source": "AudioService",
}


def _make(cls: type[Event], **payload: object) -> Event:
    return cls(**{**_ENVELOPE, **payload})  # type: ignore[arg-type]


def test_speech_started_carries_name_and_payload() -> None:
    e = _make(AudioSpeechStarted, ring_buffer_ms=300)
    assert e.name == "audio.speech_started"
    assert isinstance(e, AudioSpeechStarted)
    assert e.ring_buffer_ms == 300


def test_speech_ended_carries_name_and_payload() -> None:
    e = _make(AudioSpeechEnded, duration_ms=1500)
    assert e.name == "audio.speech_ended"
    assert e.duration_ms == 1500


def test_playback_started_carries_name_and_payload() -> None:
    e = _make(AudioPlaybackStarted, item_id="item_42")
    assert e.name == "audio.playback_started"
    assert e.item_id == "item_42"


def test_playback_finished_carries_name_and_payload() -> None:
    e = _make(AudioPlaybackFinished, item_id="item_42", played_ms=980, truncated=False)
    assert e.name == "audio.playback_finished"
    assert e.item_id == "item_42"
    assert e.played_ms == 980
    # At M4 there is no truncation path, so truncated is always False (AC-3).
    assert e.truncated is False


_EVENT_CASES = [
    (AudioSpeechStarted, {"ring_buffer_ms": 300}),
    (AudioSpeechEnded, {"duration_ms": 1500}),
    (AudioPlaybackStarted, {"item_id": "x"}),
    (AudioPlaybackFinished, {"item_id": "x", "played_ms": 1, "truncated": False}),
]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_events_are_frozen(cls: type[Event], payload: dict[str, object]) -> None:
    e = _make(cls, **payload)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.source = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(("cls", "payload"), _EVENT_CASES)
def test_events_are_slotted(cls: type[Event], payload: dict[str, object]) -> None:
    assert not hasattr(_make(cls, **payload), "__dict__")


# --- AC-2: turn origin mints, downstream propagates -------------------------


def test_speech_started_correlation_id_propagates_to_downstream() -> None:
    """The turn origin's id is carried unchanged onto a downstream event.

    Minting the fresh id is the publisher's act (AudioService, #87); here we assert the
    invariant the domain owns: a downstream ``audio.speech_ended`` built with the origin's
    ``correlation_id`` compares equal to it — propagated, not re-minted (SDS §3.12.2).
    """
    turn_id = uuid4()
    started = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": turn_id}, ring_buffer_ms=300
    )
    ended = AudioSpeechEnded(
        **{**_ENVELOPE, "event_id": uuid4(), "correlation_id": started.correlation_id},
        duration_ms=800,
    )
    assert started.correlation_id == turn_id
    assert ended.correlation_id == turn_id


def test_two_turn_origins_have_distinct_ids() -> None:
    """Each turn origin mints its own id, so two independently-minted origins differ."""
    first = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": uuid4()}, ring_buffer_ms=300
    )
    second = AudioSpeechStarted(
        **{**_ENVELOPE, "correlation_id": uuid4()}, ring_buffer_ms=300
    )
    assert isinstance(first.correlation_id, UUID)
    assert first.correlation_id != second.correlation_id


# --- AC-4: the pre-roll ring buffer -----------------------------------------

# 1 byte/ms keeps the arithmetic obvious: a frame's length in bytes is its length in ms.
_BPMS = 1


def test_drains_in_order_within_capacity() -> None:
    buf = AudioPreRoll(capacity_ms=10, bytes_per_ms=_BPMS)
    buf.append(b"aa")
    buf.append(b"bb")
    buf.append(b"cc")
    assert buf.buffered_ms == 6
    assert buf.drain() == b"aabbcc"


def test_capacity_honoured_and_oldest_dropped_on_overflow() -> None:
    buf = AudioPreRoll(capacity_ms=4, bytes_per_ms=_BPMS)
    buf.append(b"aa")  # 2
    buf.append(b"bb")  # 4 — at capacity
    buf.append(b"cc")  # 6 -> evict "aa" -> 4
    assert buf.buffered_ms == 4
    assert buf.buffered_ms <= 4  # capacity in ms honoured
    assert buf.drain() == b"bbcc"  # oldest ("aa") gone, order preserved


def test_drain_clears_the_buffer() -> None:
    buf = AudioPreRoll(capacity_ms=10, bytes_per_ms=_BPMS)
    buf.append(b"aa")
    assert buf.drain() == b"aa"
    assert buf.buffered_ms == 0
    assert buf.drain() == b""


def test_single_oversized_frame_is_retained() -> None:
    """A frame larger than the whole capacity is kept — the buffer is never emptied to
    nothing by one oversized append (the newest frame is never evicted)."""
    buf = AudioPreRoll(capacity_ms=2, bytes_per_ms=_BPMS)
    buf.append(b"aaaaa")  # 5 ms into a 2 ms buffer
    assert buf.buffered_ms == 5
    assert buf.drain() == b"aaaaa"


def test_bytes_per_ms_scales_buffered_ms() -> None:
    """buffered_ms reflects the injected frame size, not raw byte count."""
    buf = AudioPreRoll(capacity_ms=300, bytes_per_ms=48)  # 24 kHz mono 16-bit
    buf.append(b"\x00" * 48)  # exactly 1 ms
    buf.append(b"\x00" * 96)  # 2 ms
    assert buf.buffered_ms == 3
