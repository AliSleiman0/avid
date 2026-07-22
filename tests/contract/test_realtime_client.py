"""Contract + adapter tests for the ``RealtimeClient`` port (#100 skeleton, #101 fake, #105 real).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). ``RealtimeClient`` has two adapters: the deterministic
:class:`~avid.adapters.realtime.ReplayRealtimeClient` (#101 — recorded once, replayed forever,
no key, no network) and the :class:`~avid.adapters.realtime.OpenAIRealtimeClient` WSS client
(#105). #100 landed this file as a skeleton with both legs skipped; #101 flipped the ``"fake"``
leg **live**; #105 flips the ``"real"`` leg live but **network-gated** — it constructs only when a
key is present and ``AVID_LIVE`` is set, skipping in CI exactly like the Pi HAL real-legs
(SDS §14.4). That is the AVID-50 → #85 placeholder-skip pattern, one leg at a time.

The shared block asserts an adapter is port-shaped and that
:meth:`~avid.core.ports.RealtimeClient.events` is an async iterator. The
**ReplayRealtimeClient tail** then drives the deeper behaviour the fake owns (SDS §14.3), and the
**mapping tail** unit-tests the openai adapter's vendor→neutral translation offline, with canned
JSON frames and no socket — the only part of the real client that can be proven without network.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.realtime import (
    OpenAIRealtimeClient,
    ReplayRealtimeClient,
    _translate,
)
from avid.core.hal import AudioChunk
from avid.core.ports import RealtimeClient
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    RealtimeEvent,
    SessionClosed,
    TurnDone,
    UserTranscript,
)

# The committed session fixtures ship as runtime assets beside the cue bank (§14.5), so the
# path is resolved from the repo root, not a tests/ subtree.
_SESSIONS = Path(__file__).resolve().parents[2] / "assets" / "sessions"


def _live_enabled() -> bool:
    """Whether the network-gated ``"real"`` leg runs: an ``OPENAI_API_KEY`` **and** an explicit
    ``AVID_LIVE`` opt-in, so a dev with a key in their env does not spend money on every run and CI
    (which has neither) always skips — the same shape as ``_hardware.on_pi`` for the Pi legs."""
    return bool(os.environ.get("OPENAI_API_KEY")) and bool(os.environ.get("AVID_LIVE"))


# The replay fake is #101 (live below); the openai real is #105, network-gated: it skips unless a
# key + AVID_LIVE are present, so CI only ever exercises the fake — like the Pi HAL contract legs.
_FAKE_REAL_PARAMS = [
    "fake",
    pytest.param(
        "real",
        marks=pytest.mark.skipif(
            not _live_enabled(),
            reason="openai RealtimeClient real leg needs OPENAI_API_KEY + AVID_LIVE (network)",
        ),
    ),
]


@pytest.fixture(params=_FAKE_REAL_PARAMS)
def client(request: pytest.FixtureRequest) -> RealtimeClient:
    """Every RealtimeClient adapter, real and fake, must satisfy the shared block (P6).

    The ``"fake"`` leg is the replay adapter built from the ``two_turn`` recording on a
    ``FakeClock``; ``"real"`` is the openai adapter, constructed from the injected key — and it
    only reaches here when :func:`_live_enabled` is true, so CI never builds it (no change to the
    contract, only the fixture)."""
    if request.param == "real":
        return OpenAIRealtimeClient(
            api_key=os.environ["OPENAI_API_KEY"],
            model="gpt-realtime-mini-2025-12-15",
            voice="cedar",
            instructions="You are a test.",
            max_output_tokens=512,
            turn_detection={"type": "server_vad"},
        )
    return ReplayRealtimeClient.from_dir(_SESSIONS / "two_turn", clock=FakeClock())


def test_adapter_satisfies_the_realtime_client_port(client: RealtimeClient) -> None:
    assert isinstance(client, RealtimeClient)


def test_events_is_an_async_iterator(client: RealtimeClient) -> None:
    """§3.9.1: the session surfaces as a stream of neutral events — an async iterator whose
    items are RealtimeEvents, never a vendor message shape."""
    assert isinstance(client.events(), AsyncIterator)


# --- ReplayRealtimeClient tail (#101): the behaviour the fake owns (AC-5) ------------------


async def _drain(client: ReplayRealtimeClient, clock: FakeClock) -> list[RealtimeEvent]:
    """Collect everything ``events()`` yields, driving the injected clock (never wall time).

    A consumer task iterates the stream while we advance virtual time in coarse hops — larger
    than any single ``delay_ms`` — so every recorded deadline is crossed and the whole timeline
    replays deterministically and instantly (AC-4)."""
    collected: list[RealtimeEvent] = []

    async def consume() -> None:
        async for event in client.events():
            collected.append(event)

    task = asyncio.create_task(consume())
    for _ in range(100):
        await clock.advance(2.0)
        if task.done():
            break
    await task
    return collected


def _load(name: str) -> tuple[ReplayRealtimeClient, FakeClock]:
    clock = FakeClock()
    return ReplayRealtimeClient.from_dir(_SESSIONS / name, clock=clock), clock


async def test_two_turn_replays_the_recorded_sequence() -> None:
    """The normal fixture replays two full turns in order (AC-3, AC-5)."""
    replay, clock = _load("two_turn")
    await replay.open()
    events = await _drain(replay, clock)
    assert [type(e) for e in events] == [
        UserTranscript,
        AssistantTranscript,
        AssistantAudioChunk,
        TurnDone,
        UserTranscript,
        AssistantTranscript,
        AssistantAudioChunk,
        TurnDone,
    ]
    # Assistant PCM arrives as an AudioChunk at the §6.2.4 playback rate, not raw bytes.
    chunks = [e for e in events if isinstance(e, AssistantAudioChunk)]
    assert [c.chunk.sample_rate for c in chunks] == [24_000, 24_000]
    assert all(c.chunk.channels == 1 and c.chunk.pcm for c in chunks)


async def test_barge_in_carries_the_post_truncation_delta(  # AC-3
) -> None:
    """The barge-in fixture interleaves an approximate user transcript mid-reply and a
    *post-truncation* assistant delta for the same item — the delta #104's muting must later
    swallow (§6.2.4 step 6). Replay's job is to faithfully emit it, not to mute it."""
    replay, clock = _load("barge_in")
    await replay.open()
    events = await _drain(replay, clock)

    barge = next(
        e for e in events if isinstance(e, UserTranscript) and e.is_approximate
    )
    assert barge.is_approximate is True  # §6.2.4: truncated tail is unreliable

    after = events[events.index(barge) + 1 :]
    assert any(
        isinstance(e, AssistantAudioChunk) and e.item_id == "item_0" for e in after
    ), "a recorded delta for the interrupted item must arrive after the barge-in"


async def test_session_loss_ends_mid_turn_without_a_turn_done() -> None:
    """The session-loss fixture drops the socket before the turn completes (AC-3): the last
    event is a ``SessionClosed`` and no ``TurnDone`` is emitted — the mid-turn drop that drives
    ``-> DEGRADED``."""
    replay, clock = _load("session_loss")
    await replay.open()
    events = await _drain(replay, clock)
    assert isinstance(events[-1], SessionClosed)
    assert events[-1].cause == "network"
    assert not any(isinstance(e, TurnDone) for e in events)


async def test_empty_timeline_drains_to_nothing() -> None:
    """An empty session yields nothing and returns cleanly (AC-5) — no hang, no error."""
    clock = FakeClock()
    replay = ReplayRealtimeClient(clock=clock, timeline=())
    await replay.open()
    assert await _drain(replay, clock) == []


async def test_aclose_halts_the_stream_and_is_idempotent() -> None:
    """Closing before consuming halts ``events()`` (nothing replays); ``aclose`` twice is safe."""
    replay, clock = _load("two_turn")
    await replay.open()
    await replay.aclose()
    assert await _drain(replay, clock) == []
    await replay.aclose()  # idempotent
    assert replay.closed is True


async def test_send_audio_truncate_and_cancel_are_recorded() -> None:
    """Barge-in control calls are recorded on the off-port trace, non-blocking (P8). A replay
    ignores mic input (the reply is pre-recorded) but keeps the calls assertable for #104."""
    from avid.core.hal import AudioChunk

    replay, _clock = _load("two_turn")
    await replay.send_audio(AudioChunk(pcm=b"\x00\x01", sample_rate=16_000, channels=1))
    await replay.truncate("item_0", 480)
    await replay.cancel()
    assert len(replay.sent) == 1
    assert replay.truncations == [("item_0", 480)]
    assert replay.cancels == 1


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param({"format": 2, "events": []}, id="unsupported-format"),
        pytest.param(
            {"format": 1, "events": [{"delay_ms": 0, "type": "nope"}]},
            id="unknown-event-type",
        ),
        pytest.param(
            {"format": 1, "events": [{"delay_ms": 0, "type": "session_closed"}]},
            id="missing-field",
        ),
    ],
)
def test_malformed_fixture_raises_at_load_not_mid_replay(
    manifest: dict[str, object], tmp_path: Path
) -> None:
    """A broken manifest fails loudly at construction (AC-5) — before the loop runs — never as
    a surprise part-way through a replayed session."""
    (tmp_path / "session.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        ReplayRealtimeClient.from_dir(tmp_path, clock=FakeClock())


# --- OpenAIRealtimeClient mapping tail (#105): vendor → neutral, offline (AC-1) ------------
#
# The only part of the real client provable without a socket: that each Realtime **server**
# message maps to the right neutral RealtimeEvent (and that no vendor shape crosses). Canned JSON
# frames, no network — the live round-trip is the network-gated "real" leg above.


def test_translate_user_transcript() -> None:
    event = _translate(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello there",
        }
    )
    assert event == UserTranscript(text="hello there", is_approximate=False)


def test_translate_assistant_transcript() -> None:
    event = _translate(
        {
            "type": "response.output_audio_transcript.done",
            "transcript": "hi!",
            "item_id": "item_7",
        }
    )
    assert event == AssistantTranscript(text="hi!", item_id="item_7")


def test_translate_assistant_audio_decodes_base64_pcm() -> None:
    import base64

    pcm = b"\x01\x02\x03\x04"
    event = _translate(
        {
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(pcm).decode("ascii"),
            "item_id": "item_0",
        }
    )
    assert isinstance(event, AssistantAudioChunk)
    assert event.item_id == "item_0"
    # Decoded to the §6.2.4 playback format: 24 kHz mono PCM.
    assert event.chunk == AudioChunk(pcm=pcm, sample_rate=24_000, channels=1)


def test_translate_turn_done_maps_the_usage_payload() -> None:
    event = _translate(
        {
            "type": "response.done",
            "response": {
                "usage": {
                    "input_tokens": 320,
                    "output_tokens": 48,
                    "input_token_details": {"cached_tokens": 256},
                }
            },
        }
    )
    assert isinstance(event, TurnDone)
    assert event.usage.input_tokens == 320
    assert event.usage.cached_input_tokens == 256
    assert event.usage.output_tokens == 48


def test_translate_error_becomes_session_closed() -> None:
    event = _translate({"type": "error", "error": {"type": "server_error"}})
    assert event == SessionClosed(cause="server_error")


def test_translate_ignores_unmodelled_messages() -> None:
    """A delta/ack we do not surface returns None so the events loop skips it."""
    assert _translate({"type": "response.output_audio.delta.done"}) is None
    assert _translate({"type": "input_audio_buffer.speech_started"}) is None


def test_openai_client_repr_never_leaks_the_key() -> None:
    """AC-6: the secret must not reach a log line via ``repr`` (SECURITY.md)."""
    client = OpenAIRealtimeClient(
        api_key="sk-super-secret-value",
        model="gpt-realtime-mini-2025-12-15",
        voice="cedar",
        instructions="You are a test.",
        max_output_tokens=512,
        turn_detection={"type": "server_vad"},
    )
    assert "sk-super-secret-value" not in repr(client)
    assert isinstance(client, RealtimeClient)  # port-shaped without a connection (P6)
