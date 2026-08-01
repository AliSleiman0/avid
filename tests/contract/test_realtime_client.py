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
import contextlib
import json
import logging
import os
import ssl as ssl_module
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import avid.adapters.realtime as rt
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
    ToolCallRequested,
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
            transcription_model="whisper-1",
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
        pytest.param(
            {
                "format": 1,
                "events": [
                    {"delay_ms": 0, "type": "tool_call_requested", "call_id": "c0"}
                ],
            },
            id="tool-call-missing-field",
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


def test_an_error_frame_yields_no_event_and_does_not_close_the_session() -> None:
    """AVID-178: **the socket is the authority on whether the session is alive.**

    An ``error`` frame is the API complaining about one client event; a *close* frame is the
    session ending. This test previously asserted the opposite — that any error becomes
    ``SessionClosed`` — and that mapping is what made every barge-in tear down a working socket:
    ``_translate`` → ``SessionClosed`` → ``_on_session_closed`` → ``_degrade`` →
    ``_teardown_locked`` → ``aclose()``. Ten sessions in eighty seconds of bench, each announcing
    *"one sec, I lost my connection"* to a user whose connection was fine.

    ``_translate`` now has no ``error`` branch at all: the frame is logged and dropped in
    ``_events`` before this function sees it, which is what keeps ``_translate`` pure."""
    assert _translate({"type": "error", "error": {"type": "server_error"}}) is None


def test_translate_function_call_done_becomes_tool_call_requested() -> None:
    """#124 AC-5: the model's finalized tool call maps off ``response.output_item.done`` (item
    type ``function_call``) — the complete arguments ride the ``.done`` frame, so the mapping
    stays stateless, and the streaming ``.delta`` acks are not surfaced (like transcript deltas)."""
    event = _translate(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_0",
                "name": "recall",
                "arguments": '{"query": "travel plans"}',
            },
        }
    )
    assert event == ToolCallRequested(
        call_id="call_0", name="recall", arguments='{"query": "travel plans"}'
    )


def test_translate_non_function_output_item_done_is_ignored() -> None:
    """A non-function output item (e.g. a completed message) is not surfaced — returns None so
    the events loop skips it, exactly like the argument-delta acks."""
    assert (
        _translate({"type": "response.output_item.done", "item": {"type": "message"}})
        is None
    )
    assert (
        _translate({"type": "response.function_call_arguments.delta", "delta": "{"})
        is None
    )


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
        transcription_model="whisper-1",
    )
    assert "sk-super-secret-value" not in repr(client)
    assert isinstance(client, RealtimeClient)  # port-shaped without a connection (P6)


def _openai(**overrides: object) -> OpenAIRealtimeClient:
    kwargs: dict[str, object] = {
        "api_key": "sk-test",
        "model": "gpt-realtime-mini-2025-12-15",
        "voice": "cedar",
        "instructions": "You are a test.",
        "max_output_tokens": 512,
        "turn_detection": {"type": "server_vad"},
        "transcription_model": "whisper-1",
    }
    kwargs.update(overrides)
    return OpenAIRealtimeClient(**kwargs)  # type: ignore[arg-type]


class _CapturingWs:
    """A stand-in for the ``websockets`` connection: records every JSON payload sent, no socket.

    ``OpenAIRealtimeClient._send`` writes ``json.dumps(payload)`` to ``self._ws.send`` when the
    socket is live, so setting the private ``_ws`` to one of these lets the client-event
    serialisation be asserted entirely offline (like ``_translate``, the other network-free half)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class _FakeWs:
    """An async-iterable stand-in for the socket, so ``_events`` can be driven frame by frame.

    ``_events`` is the inbound half of the vendor boundary and **had no test of any kind** before
    AVID-178 — not for ``ConnectionClosed``, not for the deliberate-close guard, not for the
    truncation rewrite. That gap is why an error frame ending the stream shipped.

    Deliberately *not* a session fixture: a fixture under ``assets/sessions/`` drives
    ``ReplayRealtimeClient``, the **fake**. The defect lives in the real client's ``_translate`` /
    ``_events``, which no fixture can reach — so the frames are canned at the wire level instead.
    """

    def __init__(
        self,
        frames: list[dict[str, object]],
        *,
        then_raise: type[Exception] | None = None,
    ) -> None:
        self._frames = frames
        self._then_raise = then_raise

    def __aiter__(self) -> _FakeWs:
        self._it = iter(self._frames)
        return self

    async def __anext__(self) -> str:
        try:
            return json.dumps(next(self._it))
        except StopIteration:
            if self._then_raise is not None:
                raise self._then_raise(1006, "abnormal closure") from None
            raise StopAsyncIteration from None

    async def send(self, raw: str) -> None:  # pragma: no cover - unused by these tests
        return None


def _stub_websockets(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Install a fake ``websockets.exceptions`` and return its ``ConnectionClosed``.

    ``websockets`` is **not installed in CI** (it belongs to the ``openai`` extra) and ``_events``
    imports it at call time, so the module has to be stubbed exactly as the connect tests stub it.
    The exception carries ``code``/``reason`` because the close *code* is what diagnosed the
    GA-shape defect and ``_events`` now logs it."""
    import sys
    import types

    class _ConnectionClosed(Exception):
        def __init__(self, code: int, reason: str) -> None:
            super().__init__(code, reason)
            self.code = code
            self.reason = reason

    monkeypatch.setitem(
        sys.modules,
        "websockets.exceptions",
        types.SimpleNamespace(ConnectionClosed=_ConnectionClosed),
    )
    return _ConnectionClosed


async def _drain_events(client: OpenAIRealtimeClient) -> list[object]:
    return [event async for event in client.events()]


async def test_an_error_frame_does_not_end_the_event_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The regression test for AVID-178.** An error must not stop the conversation.

    Before the fix the error became ``SessionClosed`` *and* ``_events`` returned on it, so the
    stream died twice over — the service degraded and the generator was gone even if it hadn't."""
    _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {"type": "error", "error": {"type": "invalid_request_error"}},
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "still here",
                "item_id": "item_0",
            },
        ]
    )

    events = await _drain_events(client)

    assert events == [AssistantTranscript(text="still here", item_id="item_0")]
    assert not any(isinstance(e, SessionClosed) for e in events)


async def test_a_connection_close_yields_one_session_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *close* really does end the session — the other half of the rule, and untested until now.

    This is the safety net the "no error type is fatal" decision leans on, so it needs a proof
    rather than a promise."""
    closed = _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs([], then_raise=closed)  # type: ignore[assignment]

    assert await _drain_events(client) == [SessionClosed(cause="network")]


async def test_a_deliberate_aclose_yields_no_session_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """We closed it on purpose, so it is not an outage — the ``_closed`` guard, also untested."""
    closed = _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs([], then_raise=closed)  # type: ignore[assignment]
    client._closed = True

    assert await _drain_events(client) == []


async def test_a_malformed_frame_is_skipped_and_the_stream_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_translate`` subscripts vendor fields unguarded, and it runs on ConvSvc's pump task.

    Same family as AVID-174: one surprising frame would raise out of ``_events`` — which catches
    only ``ConnectionClosed`` — and kill the conversation loop. The delta below has no ``delta``
    key at all."""
    _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {"type": "response.output_audio.delta", "item_id": "item_0"},  # no `delta`
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "survived",
                "item_id": "item_0",
            },
        ]
    )

    assert await _drain_events(client) == [
        AssistantTranscript(text="survived", item_id="item_0")
    ]


async def test_a_pending_truncation_marks_the_next_user_transcript_approximate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6.2.4 trap 3: ``truncate`` also drops the transcript for the unplayed portion, so the tail
    is approximate. The behaviour existed with no proof until the harness made it cheap."""
    _stub_websockets(monkeypatch)
    client = _openai()
    client._truncation_pending = True
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "actually never mi",
            }
        ]
    )

    events = await _drain_events(client)

    assert events == [UserTranscript(text="actually never mi", is_approximate=True)]
    assert client._truncation_pending is False


def test_the_error_log_names_the_five_allowed_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-1: the reason must be *readable*. Until now nothing in this 800-line adapter logged an
    inbound frame at all, which is exactly why the bench trace could not say what went wrong."""
    client = _openai()
    with caplog.at_level(logging.WARNING, logger="avid.adapters.realtime"):
        client._note_error(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "response_cancel_not_active",
                    "message": "Cancellation failed: no active response found",
                    "param": "response",
                    "event_id": "avid_12_response_cancel",
                },
            }
        )

    for expected in (
        "invalid_request_error",
        "response_cancel_not_active",
        "no active response found",
        "avid_12_response_cancel",
        "session continues",
    ):
        assert expected in caplog.text


def test_the_error_log_never_echoes_the_raw_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SECURITY.md: an allow-list, never a dict dump.

    An error about ``conversation.item.create`` echoes the payload that caused it — and at M7 that
    payload is a tool output built from on-device memory (§7.10). A rejected
    ``input_audio_buffer.append`` would spill base64 PCM. Neither belongs in a log line."""
    client = _openai()
    with caplog.at_level(logging.WARNING, logger="avid.adapters.realtime"):
        client._note_error(
            {
                "type": "error",
                "audio": "SEVDUkVUIFBDTQ==",  # a sibling key the formatter must not touch
                "error": {"type": "invalid_request_error"},
            }
        )

    assert "SEVDUkVUIFBDTQ==" not in caplog.text


def test_the_error_log_bounds_a_long_vendor_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``message`` is unbounded free text from the vendor; one line per event is a contract the
    bench tracer reads."""
    client = _openai()
    with caplog.at_level(logging.WARNING, logger="avid.adapters.realtime"):
        client._note_error(
            {"type": "error", "error": {"type": "x", "message": "y" * 5000}}
        )

    assert len(caplog.text) < 1000
    assert "…" in caplog.text


def test_repeated_errors_are_numbered(caplog: pytest.LogCaptureFixture) -> None:
    """A storm should read as a storm, not as a wall of identical lines."""
    client = _openai()
    with caplog.at_level(logging.WARNING, logger="avid.adapters.realtime"):
        for _ in range(3):
            client._note_error({"type": "error", "error": {"type": "x"}})

    assert "realtime error #1" in caplog.text
    assert "realtime error #3" in caplog.text


async def test_client_events_carry_a_traceable_event_id() -> None:
    """AVID-178: the API echoes ``event_id`` back in ``error.event_id``.

    Without it the error log can only say *"something you sent was rejected"*. With it the log
    names the client event — which is what turns #178's Stage 2 from a ranked list of hypotheses
    into a fact. No user content and no key: a counter and the message type we chose ourselves."""
    ws = _CapturingWs()
    client = _openai()
    client._ws = ws  # type: ignore[assignment]
    # A response is in flight, so step 5 actually goes out — the tracking itself is proven by
    # test_cancel_is_sent_while_a_response_is_generating, which drives it through real frames.
    client._active_response = "resp_1"

    await client.truncate("item_0", 660)
    await client.cancel()

    assert [p["type"] for p in ws.sent] == [
        "conversation.item.truncate",
        "response.cancel",
    ]
    assert ws.sent[0]["event_id"] == "avid_1_conversation_item_truncate"
    assert ws.sent[1]["event_id"] == "avid_2_response_cancel"


async def test_the_first_token_split_is_reported_once_per_reply(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#106 AC-4: O1 missed its budget on two runs and nobody could say which leg spent it.

    The split is anchored on the SERVER's ``input_audio_buffer.speech_stopped``, not on our own
    falling edge, so it does not move when ``[gate] silence_hold_ms`` is retuned (AVID-176) — and
    it is taken at wire arrival, which is only meaningful because AVID-182 made the reader drain
    at wire speed. Read at consumption speed it would have measured playback.

    Once per reply, not once per delta: a 6 s reply is hundreds of deltas, and a per-delta line
    would bury the one that matters under its own output."""
    _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {"type": "input_audio_buffer.speech_stopped"},
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_audio.delta", "item_id": "item_0", "delta": ""},
            {"type": "response.output_audio.delta", "item_id": "item_0", "delta": ""},
            {"type": "response.output_audio.delta", "item_id": "item_0", "delta": ""},
        ]
    )

    with caplog.at_level(logging.INFO, logger="avid.adapters.realtime"):
        stream = client.events()
        with contextlib.suppress(StopAsyncIteration):
            async for _ in stream:
                pass

    reports = [r for r in caplog.records if "first token" in r.message]
    assert len(reports) == 1, "one line per reply, not one per delta"
    assert "response.created" in reports[0].getMessage()


async def test_the_socket_is_read_at_wire_speed_not_at_consumption_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVID-182: the session's own state must be current even while a long reply drains.

    The original shape — a generator doing ``async for raw in ws`` and ``yield``ing — read the
    socket **at playback speed**, because an async generator is suspended at its ``yield`` until
    the consumer asks again, and the consumer awaits the ALSA write for every audio delta. So
    ``response.done``, which the server sends when *generation* ends, went unread for the whole
    remaining duration of the reply. ``_active_response`` therefore said "generating" during
    exactly the window a user barges in, and every resulting ``response.cancel`` came back
    ``response_cancel_not_active``.

    Proven by consuming **nothing**: the frames must still have been read and interpreted."""
    _stub_websockets(monkeypatch)
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {
                "type": "response.output_audio_transcript.done",
                "transcript": "a long reply",
                "item_id": "item_0",
            },
            {"type": "response.done", "response": {"id": "resp_1"}},
        ]
    )

    stream = client.events()
    first = await stream.__anext__()  # take ONE event, then stop consuming
    assert isinstance(first, AssistantTranscript)

    for _ in range(20):  # let the reader run ahead of us
        await asyncio.sleep(0)

    assert client._active_response is None, (
        "response.done was not observed until the consumer asked for it — the socket is still "
        "being read at playback speed"
    )
    await stream.aclose()


async def test_cancel_is_skipped_when_no_response_is_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AVID-178 Stage 2, measured against the live API rather than guessed::

        code    'response_cancel_not_active'
        message 'Cancellation failed: no active response found'

    And it is the *common* case: generation finishes long before playback does, so by the time a
    user interrupts a reply they are still hearing, the model stopped generating seconds ago.

    ⚠️ The skip is **logged**, and that is not decoration. If this guard is ever wrong the failure
    is silent — no cancel goes out, the user hears nothing amiss (step 6's mute drops the deltas
    anyway), and the model believes it said the whole reply, poisoning context and billing output
    tokens for audio nobody heard. A bench run is graded on *"a sent cancel or a logged skip"*."""
    ws = _CapturingWs()
    client = _openai()
    client._ws = ws  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger="avid.adapters.realtime"):
        await client.cancel()

    assert ws.sent == [], "cancelled a response that was never in flight"
    assert "no active response to cancel" in caplog.text


async def test_cancel_is_sent_while_a_response_is_generating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other edge, and the one that catches the silent failure: a *live* response is cancelled.

    Driven through ``_events`` with real vendor frames rather than by setting the private flag, so
    the test proves the tracking as well as the guard."""
    _stub_websockets(monkeypatch)
    ws = _CapturingWs()
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [{"type": "response.created", "response": {"id": "resp_1"}}]
    )
    await _drain_events(client)

    client._ws = ws  # type: ignore[assignment]
    await client.cancel()

    assert [p["type"] for p in ws.sent] == ["response.cancel"]


async def test_a_finished_response_is_no_longer_cancellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``response.done`` clears the flag — including for a response that was *itself* cancelled,
    which is why the terminal frame is tracked rather than the happy path only."""
    _stub_websockets(monkeypatch)
    ws = _CapturingWs()
    client = _openai()
    client._ws = _FakeWs(  # type: ignore[assignment]
        [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.done", "response": {"id": "resp_1"}},
        ]
    )
    await _drain_events(client)

    client._ws = ws  # type: ignore[assignment]
    await client.cancel()

    assert ws.sent == []


async def test_truncate_is_always_sent_because_the_api_accepts_it() -> None:
    """Step 4 is deliberately **not** guarded. The probe confirmed the live API accepts
    ``conversation.item.truncate`` with our device-derived ``audio_end_ms`` — only step 5 was
    rejected — and §6.2.4 calls that accepted figure "the deliberate choice, not an oversight".

    Guarding truncate as well would have been the tempting symmetric change and would have broken
    the one part of barge-in that was working."""
    ws = _CapturingWs()
    client = _openai()
    client._ws = ws  # type: ignore[assignment]

    await client.truncate("item_0", 660)

    assert [p["type"] for p in ws.sent] == ["conversation.item.truncate"]
    assert ws.sent[0]["audio_end_ms"] == 660


def test_tools_are_declared_in_the_session_update_prefix() -> None:
    """#124 AC-2: tool declarations ride the session.update payload — the cached prefix (§6.2.2),
    static for the session — not a per-turn message. Empty by default (until #125 supplies the
    recall/forget/remember_fact schemas); when present they land under ``tools``."""
    tool = {"type": "function", "name": "recall", "parameters": {}}
    assert "tools" not in _openai()._session_config()  # empty default — no key at all
    assert _openai(tools=[tool])._session_config()["tools"] == [tool]


def test_the_session_update_uses_the_ga_shape_not_the_disabled_beta_one() -> None:
    """The GA session shape, pinned (§6.10 volatility, R-10).

    All three assertions are regressions from the **first live run this adapter ever had** (the
    #106 prep — CI had only ever exercised the ``replay`` fake). The beta interface is switched off
    server-side: a bare ``"pcm16"`` format string with no session ``type`` gets the socket closed
    with ``4000 invalid_request_error.beta_api_shape_disabled``, and omitting either ``rate`` earns
    ``missing_required_parameter: session.audio.output.format.rate``.

    Both rates are 24 kHz because that is the API's **floor** on input, not because it is the mic's
    rate — the Pi captures 16 kHz (Silero takes only 8/16 kHz) and ``send_audio`` resamples up.
    """
    config = _openai()._session_config()

    assert config["type"] == "realtime"
    assert config["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert config["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}


def test_mic_audio_is_resampled_up_to_the_api_floor() -> None:
    """16 kHz capture reaches the wire as 24 kHz (a 3:2 sample count), and downsampling is refused.

    The API rejects input below 24 kHz outright; ADR-007's Silero gate accepts only 8/16 kHz. Both
    constraints are real, so the adapter absorbs the conflict — which means the *count* of samples
    it puts on the wire must change, and a regression here is inaudible to every offline test but
    fatal on hardware."""
    frame = b"\x00\x10" * 320  # 20 ms of 16 kHz mono S16_LE
    out = rt._resample_pcm16(frame, source_rate=16000, target_rate=24000)

    assert len(out) == 480 * 2  # 320 samples in, 480 out — the 3:2 ratio
    assert rt._resample_pcm16(frame, source_rate=24000, target_rate=24000) is frame
    with pytest.raises(ValueError, match="refusing to downsample"):
        rt._resample_pcm16(frame, source_rate=48000, target_rate=24000)


def test_the_session_update_asks_the_api_to_transcribe_the_user() -> None:
    """Realtime does **not** transcribe input audio unless the session asks it to.

    Without this key no ``conversation.item.input_audio_transcription.completed`` frame ever
    arrives, so ``UserTranscript`` never crosses the port and ``conversation.user_transcribed`` is
    never published: the robot answers out loud with no record of what was said, leaving §7.5/§7.6
    nothing to extract a memory from. Every ``assets/sessions/`` fixture *records* that
    frame, which is precisely why replay-based CI could not see it missing — it took one live
    session (#106 prep) to find, and this test is what stops it coming back."""
    config = _openai(transcription_model="whisper-1")._session_config()

    assert config["audio"]["input"]["transcription"] == {"model": "whisper-1"}


def test_memory_block_is_appended_after_the_static_instructions() -> None:
    """#126 AC-2/AC-3: the layer-4 memory block is appended **after** the static instructions
    (layers 1–3), so those stay byte-identical and the cached prefix survives (§6.2.2)."""
    base = _openai()._session_config()["instructions"]
    composed = _openai()._session_config("MEM FACTS")["instructions"]
    assert composed == f"{base}\n\nMEM FACTS"
    assert composed.startswith(base)  # layers 1–3 unchanged as the prefix


def test_empty_memory_block_leaves_the_instructions_unchanged() -> None:
    """#126 AC-4: an empty block yields exactly the stateless prefix — a fresh device or a failed
    retrieval degrades to a robot that talks but does not remember, never one that does not talk."""
    assert (
        _openai()._session_config("")["instructions"]
        == _openai()._session_config()["instructions"]
        == "You are a test."
    )


async def test_open_overlaps_memory_retrieval_with_the_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#126 AC-1: ``top_facts`` runs **concurrently** with the WSS connect (``asyncio.gather``), so
    a slow retrieval does not extend time-to-session-ready by its own duration. With a faked socket
    that takes ~100 ms and a memory fetch that also takes ~100 ms, ``open`` completes in ~100 ms
    (the max), not ~200 ms (the sum); the block still lands in the one ``session.update``."""
    import sys
    import time
    import types

    ws = _CapturingWs()
    delay = 0.1

    seen_ssl: list[object] = []

    async def fake_connect(
        url: str, *, additional_headers: object = None, ssl: object = None
    ) -> _CapturingWs:
        seen_ssl.append(ssl)
        await asyncio.sleep(delay)
        return ws

    monkeypatch.setitem(
        sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect)
    )

    async def slow_memory() -> str:
        await asyncio.sleep(delay)
        return "MEMORY BLOCK"

    client = _openai()
    # Pre-build the TLS context (AVID-157). It is one-time work per adapter, and on a cold run it
    # costs tens of milliseconds — real, but not what this test measures, and enough to eat the
    # margin below on a loaded runner. Warming it here keeps the assertion about the *overlap*.
    client._ssl_context = ssl_module.create_default_context()
    start = time.monotonic()
    await client.open(memory=slow_memory())
    elapsed = time.monotonic() - start

    assert elapsed < delay + 0.05  # ~max(0.1, 0.1), not the ~0.2 s sum
    # AVID-157: the context is built by us and handed over, rather than letting websockets
    # build a fresh one — and parse the whole system CA bundle — on every connect.
    assert isinstance(seen_ssl[0], ssl_module.SSLContext)
    assert client._ssl_context is seen_ssl[0]
    update = ws.sent[0]
    assert update["type"] == "session.update"
    assert "MEMORY BLOCK" in update["session"]["instructions"]  # type: ignore[index]


async def test_send_tool_output_returns_the_result_then_requests_a_response() -> None:
    """#124 AC-3: the return leg is two client events in order — ``conversation.item.create``
    (function_call_output, call_id echoed) **then** ``response.create``. The second is the
    step-5 trap (§6.6): without it the model silently sits."""
    client = _openai()
    ws = _CapturingWs()
    client._ws = ws  # inject the fake socket; no connect
    await client.send_tool_output("call_0", '{"facts": ["Lisbon"]}')

    assert [m["type"] for m in ws.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    item = ws.sent[0]["item"]
    assert item == {
        "type": "function_call_output",
        "call_id": "call_0",
        "output": '{"facts": ["Lisbon"]}',
    }


async def test_tool_call_fixture_replays_the_recorded_exchange() -> None:
    """#124 AC-4: the committed ``tool_call`` fixture replays a ``recall`` invocation interleaved
    in a normal turn — the widened replay adapter emits ``ToolCallRequested`` in sequence."""
    replay, clock = _load("tool_call")
    await replay.open()
    events = await _drain(replay, clock)
    assert [type(e) for e in events] == [
        UserTranscript,
        ToolCallRequested,
        AssistantTranscript,
        AssistantAudioChunk,
        TurnDone,
    ]
    call = next(e for e in events if isinstance(e, ToolCallRequested))
    assert call.name == "recall"
    assert call.call_id == "call_0"


async def test_replay_records_tool_output_without_acting_on_it() -> None:
    """A replay does not act on a returned tool output (the follow-up is pre-recorded) but keeps
    the ``(call_id, output)`` pair on its off-port trace, assertable for the dispatch tests (#125)."""
    replay, _clock = _load("two_turn")
    await replay.send_tool_output("call_0", '{"deleted": 1}')
    assert replay.tool_outputs == [("call_0", '{"deleted": 1}')]


# --- AVID-157: the session-open phase breakdown, offline ------------------------------------


async def test_timed_reports_the_elapsed_time_of_an_await() -> None:
    """The measurement primitive. Monotonic, never wall clock — an NTP step or the Pi's
    boot-time clock correction yields negative latencies, and latency is the metric this
    project is graded on (SDS §9.1.1)."""

    async def _work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result, elapsed_ns = await rt._timed(_work())

    assert result == "done"
    assert elapsed_ns >= 10 * 1_000_000  # at least the 10 ms it slept
    assert elapsed_ns < 5_000_000_000  # and not a wall-clock artefact


def test_timed_sync_reports_the_elapsed_time_of_a_call() -> None:
    value, elapsed_ns = rt._timed_sync(lambda: 42)
    assert value == 42
    assert elapsed_ns >= 0


def test_the_open_report_names_every_phase_and_marks_a_cold_open() -> None:
    """AVID-157: the line every open logs, so any bench run is also a measurement — the same
    pattern #159's echo gate uses for its calibration datum."""
    line = rt._format_open_report(
        ssl_ns=312_000_000,
        connect_ns=1_180_000_000,
        memory_ns=28_000_000,
        send_ns=4_000_000,
        total_ns=1_204_000_000,
        cold=True,
    )

    assert line == (
        "realtime open: ssl 312 ms, connect 1180 ms, memory 28 ms, "
        "send 4 ms, total 1204 ms (cold)"
    )


def test_the_open_report_marks_a_warm_open() -> None:
    """Cold vs warm is the distinction the whole issue turns on: the bench saw 1494/1922/832 ms
    across three opens and 6652 ms on a first one. "Once per process" and "once per
    conversation" are very different conclusions about §6.3's budget."""
    line = rt._format_open_report(
        ssl_ns=0,
        connect_ns=830_000_000,
        memory_ns=0,
        send_ns=1_000_000,
        total_ns=832_000_000,
        cold=False,
    )

    assert "(warm)" in line
    assert "ssl 0 ms" in line  # the context was built on the cold open and reused


def test_the_open_report_total_is_not_the_sum_of_its_phases() -> None:
    """``connect`` and ``memory`` are gathered concurrently — that overlap is the payoff §6.7
    claims. Anyone reading the line as a sum would conclude the phases do not add up and go
    looking for missing time that was never spent."""
    line = rt._format_open_report(
        ssl_ns=0,
        connect_ns=150_000_000,
        memory_ns=30_000_000,
        send_ns=2_000_000,
        total_ns=153_000_000,
        cold=False,
    )

    assert "connect 150 ms" in line and "memory 30 ms" in line
    assert "total 153 ms" in line  # not 182
