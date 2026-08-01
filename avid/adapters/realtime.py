"""RealtimeClient adapters — the ``replay`` fake, the real ``openai`` client, and capture (#101/#105).

The :class:`~avid.core.ports.RealtimeClient` port is the vendor blast radius (R-10,
CLAUDE.md §3): ``ConversationService`` (#102) depends only on it, and its adapters
translate a Realtime session into the neutral :class:`~avid.core.realtime.RealtimeEvent`
stream. This module ships three:

* :class:`ReplayRealtimeClient` (#101) — the **fake** that plays a *recorded* session back
  deterministically (SDS §14.3): recorded once, replayed forever, with **no key, no network,
  no cost**. It is what lets the whole M5 conversation arc — the service (#102), the audio
  seam (#103), barge-in (#104) — be built and CI-gated entirely on a laptop (SDS §14.5).
* :class:`OpenAIRealtimeClient` (#105) — the **real** WebSocket client (SDS §6.2.1, ADR-010).
  It maps the OpenAI Realtime wire protocol onto the neutral port: server messages →
  :class:`~avid.core.realtime.RealtimeEvent`, our port calls → Realtime *client* events. **No
  ``openai``/Realtime type crosses the port** — the vendor's message shapes are this class's
  private business, so if the Realtime API changes exactly this one class changes (R-10).
* :class:`CapturingRealtimeClient` (#105) — a **recording decorator** that wraps any client and
  writes the neutral events flowing through it into the ``replay`` fixture format, so replay can
  never drift from real API behaviour (AC-4). Stdlib only, vendor-free.

**The vendor import is lazy** (AC-2): :class:`OpenAIRealtimeClient` imports ``websockets`` only
inside its connect helper — the ``openai`` optional group (``uv sync --extra openai``) is absent
off a networked host, so keeping the import out of module scope lets this file load everywhere
(the fake path, ``CapturingRealtimeClient``, mypy, the composition-root import), exactly as
``SileroVad`` does for ``onnxruntime`` (ADR-008). :class:`ReplayRealtimeClient` and
:class:`CapturingRealtimeClient` import nothing from the vendor at all.

The fixture format (AC-2), consumed by :meth:`ReplayRealtimeClient.from_dir` and written by
the future ``--capture`` mode (#105):

* A session is a **directory** under ``assets/sessions/<name>/`` holding one
  ``session.json`` manifest plus the small WAV clips it references — the same shipped-asset
  convention as ``assets/cues/`` / ``CUE_FILES`` (``services/cue_bank.py``).
* ``session.json`` is ``{"format": 1, "events": [ ... ]}``; each event is
  ``{"delay_ms": int, "type": <member>, ...fields}`` where ``delay_ms`` is the gap *before*
  the event and ``type`` selects a :class:`RealtimeEvent` member (its remaining keys are
  that member's fields). ``assistant_audio_chunk`` carries ``"wav": "<filename>"``, resolved
  next to the manifest and loaded as one :class:`~avid.core.hal.AudioChunk` (24 kHz mono
  S16_LE, the §6.2.4 playback format).

Timing is driven by the **injected** :class:`~avid.core.ports.Clock` (AC-4), never wall
time: under ``FakeClock`` the tests run instantly and deterministically; under
``SystemClock`` a laptop sim plays at the recorded pace. Constructed only by the composition
root or a test fixture (P3); everything else depends on the port (P2).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
import wave
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar, assert_never

from avid.core.hal import AudioChunk
from avid.core.ports import Clock, RealtimeClient
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    RealtimeEvent,
    SessionClosed,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from avid.core.tasks import spawn
from avid.domain import TokenUsage

_log = logging.getLogger(__name__)

_MANIFEST = "session.json"
_SUPPORTED_FORMAT = 1
_NS_PER_MS = 1_000_000

# Cap on the vendor free-text fields that reach the log (AVID-178). ``error.message`` and
# ``error.param`` are unbounded strings written by the API and can echo the client event that
# caused them — see :func:`_format_error_frame` for why that is a security bound, not tidiness.
_MAX_ERROR_CHARS = 200

_T = TypeVar("_T")


def _timed_sync(call: Callable[[], _T]) -> tuple[_T, int]:
    """Run *call* and report how long it took, in nanoseconds. See :func:`_timed`."""
    started_ns = time.monotonic_ns()
    result = call()
    return result, time.monotonic_ns() - started_ns


async def _timed(awaitable: Awaitable[_T]) -> tuple[_T, int]:
    """Await *awaitable* and report how long it took, in nanoseconds.

    ``time.monotonic_ns`` and never a wall clock: an NTP step or the Pi's boot-time clock
    correction yields negative latencies, which is precisely the metric this project is graded
    on (SDS §9.1.1)."""
    started_ns = time.monotonic_ns()
    result = await awaitable
    return result, time.monotonic_ns() - started_ns


def _format_error_frame(msg: dict[str, Any], *, count: int) -> str:
    """Render an ``error`` server frame as one log line — an **allow-list**, never the raw frame.

    Five fields cross into the log and no others (AVID-178). That bound is a security property,
    not tidiness: an error about ``conversation.item.create`` echoes the offending payload, and at
    M7 that payload is a tool output **built from on-device memory** — the top-facts block is the
    only memory OpenAI ever sees (§7.10), and a log line is not a place to widen it. Dumping the
    frame would also spill base64 PCM from a rejected ``input_audio_buffer.append``.

    ``message``/``param`` are bounded and ``!r``-quoted so a multi-line vendor string cannot break
    the one-line-per-event contract the bench tracer reads. ``count`` is a per-session ordinal, so
    an error *storm* is visible as a storm rather than as a wall of identical lines. The trailing
    clause is there so nobody at the bench reads this WARNING as the outage it used to cause.
    """
    error = msg.get("error") or {}

    def _bounded(value: object) -> str:
        text = str(value)
        if len(text) > _MAX_ERROR_CHARS:
            text = text[:_MAX_ERROR_CHARS] + "…"
        return repr(text)

    return (
        f"realtime error #{count}: type={error.get('type')!r} code={error.get('code')!r} "
        f"event_id={error.get('event_id')!r} param={_bounded(error.get('param'))} "
        f"message={_bounded(error.get('message'))} (session continues)"
    )


def _format_open_report(
    *,
    ssl_ns: int,
    connect_ns: int,
    memory_ns: int,
    send_ns: int,
    total_ns: int,
    cold: bool,
) -> str:
    """The one-line session-open breakdown (AVID-157).

    **``total`` is not the sum**: ``connect`` and ``memory`` are gathered concurrently, which is
    the whole point of §6.7's overlap, so ``total`` is roughly ``ssl + max(connect, memory) +
    send``. A ``memory`` figure approaching ``connect`` means the overlap has stopped being free.

    ``cold`` marks the first open of the process, which is where one-time costs land — the CA
    bundle parse below, DNS and TLS caches, the lazy ``websockets`` import. The bench measured
    1494/1922/832 ms across three opens and 6652 ms on a first one; telling those apart is the
    difference between "once per conversation" and "once per process", and they imply very
    different things about SDS §6.3's budget."""
    return (
        f"realtime open: ssl {ssl_ns / _NS_PER_MS:.0f} ms, "
        f"connect {connect_ns / _NS_PER_MS:.0f} ms, "
        f"memory {memory_ns / _NS_PER_MS:.0f} ms, "
        f"send {send_ns / _NS_PER_MS:.0f} ms, "
        f"total {total_ns / _NS_PER_MS:.0f} ms ({'cold' if cold else 'warm'})"
    )


# The assistant playback format the Realtime API emits (SDS §6.2.4): PCM16, 24 kHz mono.
_ASSISTANT_SAMPLE_RATE = 24_000
_ASSISTANT_CHANNELS = 1
_S16_WIDTH_BYTES = 2  # S16_LE: 2 bytes/sample

# The Realtime WebSocket endpoint (SDS §6.2.1 — WSS, server key, no ephemeral token dance).
_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# The rate we must DECLARE and SEND on the input side. Not a preference: the GA API rejects
# anything lower with `integer_below_min_value` ("Expected a value >= 24000"). Our capture is
# 16 kHz because Silero v5 — the ADR-007 local gate — accepts only 8 or 16 kHz, so the two
# constraints genuinely conflict and :meth:`OpenAIRealtimeClient.send_audio` resamples between
# them. Keep it equal to _ASSISTANT_SAMPLE_RATE: one wire rate in both directions.
_WIRE_INPUT_RATE = _ASSISTANT_SAMPLE_RATE


def _resample_pcm16(pcm: bytes, *, source_rate: int, target_rate: int) -> bytes:
    """Linearly resample mono S16_LE *pcm* from *source_rate* to *target_rate*.

    Stdlib only (``array``), because this sits on the audio path and the default runtime is
    pydantic-only (ADR-012) — pulling numpy in here would drag the lazy ``memory`` extra into
    every conversation.

    Linear interpolation is honest for the case we have (16 kHz → 24 kHz, *up*): the source is
    already band-limited to 8 kHz, so interpolating invents no aliases — it only gently attenuates
    the top octave. **Downsampling is refused rather than faked**: doing it without a low-pass
    would fold high frequencies back as aliasing, and a quietly wrong microphone is precisely the
    class of defect #146 cost us a gate to learn.
    """
    if source_rate == target_rate:
        return pcm
    if source_rate > target_rate:
        raise ValueError(
            f"refusing to downsample {source_rate} Hz to {target_rate} Hz without an "
            f"anti-alias filter — capture at {target_rate} Hz or lower instead"
        )
    src = array("h")
    src.frombytes(pcm)
    if not src:
        return pcm
    count = len(src) * target_rate // source_rate
    step = source_rate / target_rate
    out = array("h")
    for index in range(count):
        position = index * step
        left = int(position)
        right = min(left + 1, len(src) - 1)
        frac = position - left
        out.append(int(src[left] + (src[right] - src[left]) * frac))
    return out.tobytes()


def _load_wav(path: Path) -> AudioChunk:
    """Read an S16_LE WAV into a single :class:`AudioChunk` (stdlib ``wave``).

    Synchronous, called at construction *before* the loop runs (like config load), so P8 is
    not implicated. The WAV's own rate/channels are adopted so the chunk advertises the
    format the clip actually carries (assistant audio is 24 kHz mono, §6.2.4)."""
    with wave.open(str(path), "rb") as handle:
        return AudioChunk(
            pcm=handle.readframes(handle.getnframes()),
            sample_rate=handle.getframerate(),
            channels=handle.getnchannels(),
        )


def _build_event(record: dict[str, Any], *, base: Path) -> RealtimeEvent:
    """Map one manifest record to a neutral :class:`RealtimeEvent` (AC-2).

    ``type`` selects the member; its remaining keys are the member's fields. A missing key
    or unknown ``type`` is a :class:`ValueError` — the fixture is malformed and the failure
    belongs at load time, not mid-stream (AC-5)."""
    kind = record.get("type")
    try:
        if kind == "user_transcript":
            return UserTranscript(
                text=record["text"], is_approximate=record["is_approximate"]
            )
        if kind == "assistant_transcript":
            return AssistantTranscript(text=record["text"], item_id=record["item_id"])
        if kind == "tool_call_requested":
            return ToolCallRequested(
                call_id=record["call_id"],
                name=record["name"],
                arguments=record["arguments"],
            )
        if kind == "assistant_audio_chunk":
            return AssistantAudioChunk(
                chunk=_load_wav(base / record["wav"]), item_id=record["item_id"]
            )
        if kind == "turn_done":
            usage = record["usage"]
            return TurnDone(
                usage=TokenUsage(
                    input_tokens=usage["input_tokens"],
                    cached_input_tokens=usage["cached_input_tokens"],
                    output_tokens=usage["output_tokens"],
                )
            )
        if kind == "session_closed":
            return SessionClosed(cause=record["cause"])
    except KeyError as exc:
        raise ValueError(f"{kind!r} event is missing field {exc}") from exc
    raise ValueError(f"unknown event type {kind!r}")


class ReplayRealtimeClient:
    """The :class:`~avid.core.ports.RealtimeClient` fake (P6): replay a recorded session.

    ``timeline`` is the ordered ``(delay_ms, event)`` script :meth:`events` plays through the
    injected ``clock``; :meth:`from_dir` builds one from an ``assets/sessions/`` directory.
    :meth:`truncate`/:meth:`cancel`/:meth:`send_audio` are **recorded, not acted on** — a
    replay of a recording keeps emitting the recorded post-truncation deltas, because muting
    them by ``item_id`` (§6.2.4 step 6) is the *service's* job (#104); the barge-in fixture
    exists precisely to feed those deltas to that logic. The off-port :attr:`sent`,
    :attr:`truncations`, :attr:`cancels`, :attr:`opened` and :attr:`closed` attributes are the
    assertable trace (like ``FakeSpeaker.played``/``stops``), off the port because no
    application reads them back.
    """

    def __init__(
        self, *, clock: Clock, timeline: Sequence[tuple[int, RealtimeEvent]]
    ) -> None:
        self._clock = clock
        self._timeline = tuple(timeline)
        # Advertised, off the port (the contract's observation points, not an app need).
        self.sent: list[AudioChunk] = []
        self.truncations: list[tuple[str, int]] = []
        self.tool_outputs: list[tuple[str, str]] = []
        self.injected: list[str] = []
        self.cancels = 0
        self.opened = False
        self.closed = False

    @classmethod
    def from_dir(cls, path: Path, *, clock: Clock) -> ReplayRealtimeClient:
        """Build a replay client from a recorded-session directory (AC-2).

        Reads ``<path>/session.json`` and resolves each ``assistant_audio_chunk``'s WAV
        beside it, synchronously and before the loop (P8), like ``FakeMicrophone.from_wav``.
        A wrong ``format`` version or a malformed record raises :class:`ValueError` here — a
        broken fixture fails loudly at load, never mid-replay (AC-5)."""
        manifest = json.loads((path / _MANIFEST).read_text(encoding="utf-8"))
        version = manifest.get("format")
        if version != _SUPPORTED_FORMAT:
            raise ValueError(
                f"unsupported session format {version!r} (expected {_SUPPORTED_FORMAT})"
            )
        timeline = [
            (int(record["delay_ms"]), _build_event(record, base=path))
            for record in manifest["events"]
        ]
        return cls(clock=clock, timeline=timeline)

    async def open(self, *, memory: Awaitable[str] | None = None) -> None:
        """Open a fresh session — cold, no resume (SDS §6.2.3). Rewinds so a re-open replays
        from the top, clearing any prior :meth:`aclose`.

        A replay carries recorded instructions, so it does not seed a ``session.update`` — but it
        **awaits** the injected ``memory`` block (#126) so the top-facts fetch actually runs on every
        open (the cold re-seed, AC-5) and does not leak an un-awaited coroutine, recording the resolved
        text on the off-port :attr:`injected` trace for the dispatch tests, like :attr:`truncations`."""
        if memory is not None:
            self.injected.append(await memory)
        self.opened = True
        self.closed = False

    async def aclose(self) -> None:
        """Tear the session down. Idempotent; halts an in-flight :meth:`events` stream."""
        self.closed = True

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Record one mic chunk (the model's input is ignored by a replay — the reply is
        pre-recorded). Non-blocking (P8)."""
        self.sent.append(chunk)

    def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield the recorded neutral events, paced by the injected clock. See :meth:`_events`."""
        return self._events()

    async def _events(self) -> AsyncIterator[RealtimeEvent]:
        """Replay ``timeline``: sleep each event's ``delay_ms`` on the injected clock (AC-4),
        then yield it — until the script is exhausted or :meth:`aclose` halts the stream. An
        empty timeline yields nothing and returns cleanly (AC-5)."""
        for delay_ms, event in self._timeline:
            if self.closed:
                return
            await self._clock.sleep(delay_ms / 1000)
            if self.closed:
                return
            yield event

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Barge-in step 4 (§6.2.4): record the truncation. Non-blocking (P8)."""
        self.truncations.append((item_id, audio_end_ms))

    async def cancel(self) -> None:
        """Barge-in step 5 (§6.2.4): record the cancel. Non-blocking (P8)."""
        self.cancels += 1

    async def send_tool_output(self, call_id: str, output: str) -> None:
        """Record one tool result (§6.6). A replay does not act on it — the model's follow-up is
        pre-recorded (the next timeline events), so this only keeps the ``(call_id, output)`` pair
        assertable for the dispatch tests (#125), like :attr:`truncations`. Non-blocking (P8)."""
        self.tool_outputs.append((call_id, output))


# --- OpenAIRealtimeClient (#105): the real WSS client -------------------------------------


def _translate(msg: dict[str, Any]) -> RealtimeEvent | None:
    """Map one parsed Realtime **server** message to a neutral :class:`RealtimeEvent` (AC-1).

    Pure and vendor-free at the type level — it takes an already-parsed ``dict`` and returns one
    of *our* frozen values, so no ``openai`` shape crosses the port. A message type we do not
    model returns ``None`` (the caller skips it): the union is closed on *our* side, and the
    Realtime stream carries many deltas/acks we deliberately do not surface (e.g. incremental
    transcript deltas — we take the ``.done`` rollup). Exposed at module scope so the vendor→neutral
    mapping is unit-tested with canned frames, no socket (SDS §14.3). ``is_approximate`` on a user
    transcript is decided by the caller (a barge-in truncation was in flight), not here — the raw
    message carries no such flag (§6.2.4 trap 3).
    """
    kind = msg.get("type")
    if kind == "conversation.item.input_audio_transcription.completed":
        return UserTranscript(text=str(msg.get("transcript", "")), is_approximate=False)
    if kind == "response.output_audio_transcript.done":
        return AssistantTranscript(
            text=str(msg.get("transcript", "")), item_id=str(msg["item_id"])
        )
    if kind == "response.output_audio.delta":
        pcm = base64.b64decode(msg["delta"])
        return AssistantAudioChunk(
            chunk=AudioChunk(
                pcm=pcm,
                sample_rate=_ASSISTANT_SAMPLE_RATE,
                channels=_ASSISTANT_CHANNELS,
            ),
            item_id=str(msg["item_id"]),
        )
    if kind == "response.output_item.done":
        item = msg.get("item") or {}
        if item.get("type") == "function_call":
            # §6.6: the model finalized a tool call. Arguments streamed in on
            # response.function_call_arguments.delta (unmodelled, like transcript deltas); this
            # .done frame carries the complete arguments, so we map off it and stay stateless.
            return ToolCallRequested(
                call_id=str(item["call_id"]),
                name=str(item["name"]),
                arguments=str(item.get("arguments", "")),
            )
        return None  # a non-function output item (e.g. a message) — not surfaced here
    if kind == "response.done":
        usage = (msg.get("response") or {}).get("usage") or {}
        details = usage.get("input_token_details") or {}
        return TurnDone(
            usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                cached_input_tokens=int(details.get("cached_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            )
        )
    # NOTE: there is deliberately no ``error`` branch here. An error frame is a complaint about
    # one client event, not a session ending, and it yields no neutral event at all — it is
    # logged and dropped in :meth:`OpenAIRealtimeClient._note_error` before this function is
    # reached (AVID-178, SDS §6.2.5). Mapping it to ``SessionClosed`` is what made every barge-in
    # tear down a perfectly good socket.
    return None


class OpenAIRealtimeClient:
    """The real :class:`~avid.core.ports.RealtimeClient`, over the OpenAI Realtime WebSocket (#105).

    Maps the vendor protocol onto the neutral port and **seals the vendor inside** (CLAUDE.md §3,
    R-10): server messages are translated by :func:`_translate` before they cross :meth:`events`,
    and our port calls (:meth:`send_audio`/:meth:`truncate`/:meth:`cancel`) are serialised to
    Realtime *client* events — no ``openai``/Realtime type is ever visible past this class. The
    transport is raw ``websockets`` + JSON (SDS §6.2.1, ADR-010: WSS with a server key, no
    ephemeral-token dance), imported **lazily** in :meth:`open` so the module loads without the
    ``openai`` optional group (AC-2).

    A session is **cold** (SDS §6.2.3, no resume): :meth:`open` connects and sends one
    ``session.update`` carrying the instructions/voice/turn-detection/max-output budget, and a
    dropped socket surfaces as :class:`~avid.core.realtime.SessionClosed` for
    ``ConversationService`` to re-open. Constructed only by the composition root (P3); the API key
    is injected already-unwrapped and is used only to build the ``Authorization`` header — never
    logged, never in ``repr`` (AC-3/AC-6, SECURITY.md).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        instructions: str,
        max_output_tokens: int,
        turn_detection: dict[str, Any],
        transcription_model: str,
        tools: Sequence[dict[str, Any]] = (),
    ) -> None:
        self._api_key = (
            api_key  # private; only ever used to build the connect header (AC-6)
        )
        self._model = model
        self._voice = voice
        self._instructions = instructions
        self._max_output_tokens = max_output_tokens
        self._turn_detection = turn_detection
        self._transcription_model = transcription_model
        # Tool declarations (§6.6) are session-level and part of the cached prefix (§6.2.2), so
        # they are fixed at construction, never sent per-turn. Empty until #125 supplies the
        # recall/forget/remember_fact schemas via the composition root; a vendor-shaped dict
        # injected here (like turn_detection) does not cross the port.
        self._tools = tuple(tools)
        self._ws: Any = None  # the websockets connection, untyped (lazy vendor import)
        # Built once on the first open() and reused for the process's life (AVID-157).
        self._ssl_context: ssl.SSLContext | None = None
        self._closed = False
        # Set on a truncate() and consumed by the next user transcript: audio/transcript alignment
        # is imprecise at a barge-in boundary, so that transcript's tail is approximate (§6.2.4).
        self._truncation_pending = False
        # Per-session counters (AVID-178). ``_sent_seq`` stamps outbound events so the API can name
        # which one it rejected; ``_error_count`` numbers inbound complaints so a storm reads as one.
        self._sent_seq = 0
        self._error_count = 0
        # The response the model is currently generating, or None (AVID-178). Tracked here rather
        # than in the service because ``response.created``/``response.done`` are vendor shapes
        # (CLAUDE.md §3) — and because ConversationService._turn_active only LOOKS like the same
        # state: AVID-158 established it is set on the user transcript, which arrives after the
        # assistant's audio and sometimes after the turn has ended.
        self._active_response: str | None = None
        # Neutral events read from the socket, awaiting the consumer (AVID-182). Unbounded on
        # purpose: this queue REPLACES the vendor library's own read buffer rather than adding a
        # second one, so bounding it would drop assistant audio the previous design simply held.
        # It drains at playback speed and is emptied on every open/close, so it cannot grow past
        # one reply's worth of deltas.
        self._inbox: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        # The O1 decomposition (#106 AC-4). Wire-arrival marks for the frames between the
        # SERVER deciding the user stopped talking and the first byte of reply audio — see
        # :meth:`_note_first_token_timing` for why they are read here and not computed anywhere else.
        self._speech_stopped_ns: int | None = None
        self._response_created_ns: int | None = None

    def __repr__(self) -> str:
        """Key-free repr (AC-6): the secret must never reach a log line via ``repr``."""
        return f"OpenAIRealtimeClient(model={self._model!r}, voice={self._voice!r})"

    def _session_config(self, memory_block: str = "") -> dict[str, Any]:
        """The ``session.update`` payload (SDS §6.2.2). ``instructions``/``voice``/``model`` are the
        cacheable, session-static prefix (§6.10.2, Fact 1); ``max_output_tokens`` is a cost
        guardrail (§6.10). ``create_response``/``interrupt_response`` let the server VAD drive
        turn-taking and barge-in. ``tools`` rides the same static payload (§6.6) — part of the
        cached prefix, so declared once here and never mutated mid-session (§6.2.2).

        ``memory_block`` is the §6.7-path-1 layer-4 injection (#126): **appended after** the static
        instructions (layers 1–3), never interleaved, so those layers stay byte-identical and the
        cached prefix survives (§6.2.2, AC-2). Empty by default → the instruction string is exactly
        the stateless prefix (AC-4)."""
        instructions = self._instructions
        if memory_block:
            instructions = f"{instructions}\n\n{memory_block}"
        config: dict[str, Any] = {
            # GA shape (§6.10 volatility, R-10). The beta interface — `OpenAI-Beta: realtime=v1`
            # plus a bare "pcm16" format string and no session type — is switched off server-side
            # and closes the socket with 4000 invalid_request_error.beta_api_shape_disabled.
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": _WIRE_INPUT_RATE},
                    # Without this the API never transcribes the user and
                    # `conversation.item.input_audio_transcription.completed` never arrives — so
                    # `UserTranscript` never crosses the port and `conversation.user_transcribed`
                    # is never published: the robot answers aloud with no record of what was
                    # said, so §7.5/§7.6 have nothing to extract a memory from.
                    "transcription": {"model": self._transcription_model},
                    "turn_detection": {
                        **self._turn_detection,
                        "create_response": True,
                        # FALSE, and this is load-bearing. Barge-in is OURS (§6.2.4, #104): local
                        # VAD cuts the speaker, then this adapter sends truncate(item, played_ms)
                        # + cancel with the ms the device really emitted. Letting the server also
                        # interrupt is not redundancy, it is a second cancel racing ours on worse
                        # information.
                        #
                        # How we learned it, since the symptom looks nothing like the cause: at the
                        # #106 bench run every response went `response.created` -> `response.done`
                        # with no output items and zero usage, so the robot only ever played its
                        # thinking cue — the user hears "one second", forever. `AudioService` then
                        # buffered each utterance and handed it over in one burst, and a burst
                        # arriving while a reply is in flight is indistinguishable from a barge-in,
                        # so the server cancelled every reply it had just started. #153 removed the
                        # burst (capture is streamed live now, §6.3), which removes that particular
                        # trigger — but this setting stays off for the reason above, which never
                        # depended on it. Turning it back on is a second canceller, not a fix.
                        "interrupt_response": False,
                    },
                },
                "output": {
                    # 24 kHz is what the model emits and what _translate stamps on every
                    # AssistantAudioChunk; GA requires the rate stated rather than implied.
                    "format": {
                        "type": "audio/pcm",
                        "rate": _ASSISTANT_SAMPLE_RATE,
                    },
                    "voice": self._voice,
                },
            },
            "max_output_tokens": self._max_output_tokens,
        }
        if self._tools:
            config["tools"] = list(self._tools)  # §6.6 — static for the session's life
        return config

    async def open(self, *, memory: Awaitable[str] | None = None) -> None:
        """Connect a fresh cold session and send ``session.update`` (SDS §6.2.2/§6.2.3).

        The lazy ``websockets`` import (AC-2) keeps the vendor transport out of module scope. The
        key builds the ``Authorization`` header and nothing else (AC-6). ``model`` is fixed in the
        URL at connect (§6.2.2 — model/voice cannot change within a session).

        The §6.7-path-1 memory injection (#126): ``memory`` — an awaitable resolving to the layer-4
        block — is awaited **concurrently with the socket connect** (``asyncio.gather``), so the
        ~30 ms local retrieval overlaps the ~150 ms WSS setup and adds no wall-clock latency to
        time-to-session-ready (AC-1). Only the single ``session.update`` that follows depends on the
        block, and it carries the static prefix + that block as layer 4 (§6.2.2, AC-2).

        **Every open reports its phase breakdown** (AVID-157, see :func:`_format_open_report`).
        The bench measured this call at 1.5–6.7 s against §6.3's ~200 ms budget, and while it is in
        flight *no audio reaches the API at all* — so the utterance that opens a session is fully
        buffered behind it, which makes #153's streaming a no-op for that turn. The breakdown is
        what decides whether that time is ours or the API's, and it is logged on every open rather
        than behind a flag so any bench run is also a measurement.
        """
        import websockets  # lazy, adapter-local optional group (AC-2, ADR-008)

        started_ns = time.monotonic_ns()
        cold = self._ssl_context is None
        ssl_ns = 0
        if self._ssl_context is None:
            # ONCE per adapter, not once per connect (AVID-157). Passing no ``ssl=`` makes
            # ``websockets`` build a default context itself on every call, and building one parses
            # the whole system CA bundle — measured at 49.6 ms cold / ~7 ms warm on a fast laptop,
            # and a Pi is much slower at it. Reusing a context across connections is the documented
            # pattern; there is no per-connection state in it.
            self._ssl_context, ssl_ns = _timed_sync(ssl.create_default_context)

        # No `OpenAI-Beta: realtime=v1`: that header selects the beta interface, which is disabled
        # server-side and rejects the GA session shape this adapter sends (see _session_config).
        headers = {"Authorization": f"Bearer {self._api_key}"}
        connect = websockets.connect(
            f"{_REALTIME_URL}?model={self._model}",
            additional_headers=headers,
            ssl=self._ssl_context,
        )
        if memory is None:
            self._ws, connect_ns = await _timed(connect)
            block, memory_ns = "", 0
        else:
            # Overlap the ~150 ms connect with the ~30 ms retrieval — the payoff of the gate (§6.7).
            (self._ws, connect_ns), (block, memory_ns) = await asyncio.gather(
                _timed(connect), _timed(memory)
            )
        self._closed = False
        self._truncation_pending = False
        self._sent_seq = 0
        self._error_count = 0
        self._active_response = None
        # The O1 marks belong to ONE session's turn and must not survive a reconnect. On the
        # 2026-08-01 AC-4 run a cold open timed out, and the marks left over from before the
        # retry produced `response.created nan ms` and a 233 ms "total" for a turn whose
        # response.created belonged to a socket that no longer existed. A stale measurement is
        # worse than a missing one: it looks like data.
        self._speech_stopped_ns = None
        self._response_created_ns = None
        self._inbox = (
            asyncio.Queue()
        )  # a cold session starts with an empty stream (§6.2.3)
        self._reader_task = spawn(self._reader(), name="OpenAIRealtimeClient.reader")
        _, send_ns = await _timed(
            self._send(
                {"type": "session.update", "session": self._session_config(block)}
            )
        )
        _log.info(
            "%s",
            _format_open_report(
                ssl_ns=ssl_ns,
                connect_ns=connect_ns,
                memory_ns=memory_ns,
                send_ns=send_ns,
                total_ns=time.monotonic_ns() - started_ns,
                cold=cold,
            ),
        )

    async def aclose(self) -> None:
        """Tear the session down and release the socket (idempotent). A subsequent
        :meth:`events` iteration returns at once rather than raising a connection error."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        # Release any consumer parked on the queue: the reader's `finally` cannot run if it was
        # cancelled before reaching it, and a pump waiting forever is the wedge AVID-171 fixed.
        self._inbox.put_nowait(None)

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Append one captured mic frame to the input buffer (``input_audio_buffer.append``).

        Resampled to :data:`_WIRE_INPUT_RATE` first, because **the API refuses anything below
        24 kHz** (``integer_below_min_value``: "Expected a value >= 24000") while the Pi captures
        at 16 kHz — the rate ADR-007's Silero gate needs, since Silero v5 accepts only 8/16 kHz.
        Both constraints are real and neither side can move, so the conversion lives *here*: a
        vendor's format demand is exactly what an adapter exists to absorb (CLAUDE.md §3). Nothing
        upstream — mic, VAD, AudioService, the port — learns that 24 kHz matters.

        Base64 is the wire encoding for PCM on the Realtime protocol. Non-blocking (P8): the
        resample is pure-stdlib integer work on a 20 ms frame (320 → 480 samples), microseconds,
        and deliberately not numpy — the default runtime stays pydantic-only (ADR-012)."""
        pcm = _resample_pcm16(
            chunk.pcm, source_rate=chunk.sample_rate, target_rate=_WIRE_INPUT_RATE
        )
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield neutral events translated from the server stream. See :meth:`_events`."""
        return self._events()

    async def _reader(self) -> None:
        """Drain the socket at **wire speed**, into :attr:`_inbox` (AVID-182).

        This exists because the obvious shape — a generator doing ``async for raw in ws`` and
        ``yield``ing — reads the socket *at playback speed*. An async generator is suspended at
        its ``yield`` until the consumer asks again, and the consumer is
        ``ConversationService._pump``, which awaits ``sink.play()`` → the ALSA write for every
        audio delta. So while a reply is playing, **nothing reads the socket**: frames queue in
        the vendor buffer and are observed seconds late.

        That is not a tidiness problem, it is a correctness one. ``response.done`` is sent when
        *generation* ends, often seconds before playback finishes, so :attr:`_active_response`
        said "generating" during exactly the window in which a user barges in — and the resulting
        ``response.cancel`` was rejected with ``response_cancel_not_active`` on every bench run.
        The guard was not buggy; it was reading stale news.

        Frame *interpretation* — the error log, the response lifecycle — therefore happens here,
        as frames arrive. Only the neutral events are queued, because those are the ones whose
        delivery is legitimately paced by the consumer.
        """
        from websockets.exceptions import ConnectionClosed

        ws = self._ws
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "error":
                    self._note_error(msg)
                    continue  # a complaint, not a close — SDS §6.2.5
                self._track_response(msg)
                self._note_first_token_timing(msg)
                try:
                    event = _translate(msg)
                except Exception:  # noqa: BLE001 - a surprising frame must not kill the reader
                    # Same failure family as AVID-174: _translate subscripts vendor fields
                    # unguarded, and this task owns the socket. Type only — a malformed audio
                    # delta's body is base64 PCM and does not belong in a log.
                    _log.warning(
                        "unhandled realtime frame type=%r — skipped", msg.get("type")
                    )
                    continue
                if event is None:
                    continue  # an unmodelled delta/ack — deliberately not surfaced
                if isinstance(event, UserTranscript) and self._truncation_pending:
                    event = UserTranscript(text=event.text, is_approximate=True)
                    self._truncation_pending = False
                await self._inbox.put(event)
        except ConnectionClosed as exc:
            if not self._closed:
                # The close *code* is how the GA-shape defect was diagnosed
                # (4000 invalid_request_error.beta_api_shape_disabled, §6.2.2). Discarding it is
                # the same mistake AVID-178 fixed at the inbound end of the wire.
                _log.warning(
                    "realtime socket closed: code=%r reason=%r", exc.code, exc.reason
                )
                await self._inbox.put(SessionClosed(cause="network"))
        finally:
            await self._inbox.put(None)  # the stream is over; release the consumer

    async def _events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield what :meth:`_reader` has queued, in order (AC-1).

        Consumption is deliberately still paced by the caller — audio must be played at the speed
        the speaker accepts it. What changed in AVID-182 is that *reading* no longer is, so the
        session's own state is current even while a long reply drains.
        """
        if self._ws is None:
            return
        if self._reader_task is None or self._reader_task.done():
            # Normally started by open(). Started here too so that setting the socket directly —
            # which the offline frame-level tests do, and which is the only way to drive this
            # path without a network — still produces a live stream.
            self._reader_task = spawn(
                self._reader(), name="OpenAIRealtimeClient.reader"
            )
        while True:
            event = await self._inbox.get()
            if event is None:
                return
            yield event

    def _track_response(self, msg: dict[str, Any]) -> None:
        """Follow the response lifecycle so :meth:`cancel` knows whether anything is in flight.

        Kept out of :func:`_translate`, which is pure and stateless by contract — this is state,
        and it belongs on the client. ``response.done`` covers a *cancelled* response too, so the
        flag clears on every terminal path rather than only the happy one."""
        kind = msg.get("type")
        if kind == "response.created":
            response = msg.get("response") or {}
            self._active_response = str(response.get("id", "")) or "active"
        elif kind == "response.done":
            self._active_response = None

    def _note_first_token_timing(self, msg: dict[str, Any]) -> None:
        """Log where O1 actually goes, once per reply (#106 AC-4, SDS §2.8.1).

        Two bench runs put O1's P50 at 1773 / 1943 ms against an 800 ms budget, with a *warm*
        1422 ms connect on the second — so session open is not the cause and nobody knew what
        was. §2.8.1 itemises the budget but the robot only ever reported the total, and the
        rule that #168 cost a session to learn is that you measure before you choose a knob.

        Three marks, all taken **as the frame arrives on the socket**, which is only honest
        because AVID-182 made the reader drain at wire speed — taken at consumption speed they
        would have measured playback, which is exactly the bug that motivated this.

        * ``input_audio_buffer.speech_stopped`` — the SERVER's own speech-end decision. The right
          zero: it is what ``[ai.turn_detection] silence_duration_ms`` delays, and unlike our
          falling edge it does not move when ``[gate] silence_hold_ms`` changes (AVID-176).
        * ``response.created`` — the server accepted the turn and began work.
        * the first ``response.output_audio.delta`` — first audio exists.

        ``created → first delta`` is the model's time-to-first-token plus one network hop, and it
        is the number that decides whether O1 ≤ 800 ms is reachable **at all** with this model:
        none of it is ours to optimise, and no knob in ``config/pi.toml`` touches it. The
        remainder — O1 minus this total minus the commit delay — is ours.

        INFO, not DEBUG: this is the evidence a milestone criterion turns on, and it has to
        survive an ordinary bench run without anyone remembering to raise a log level.
        """
        now_ns = time.monotonic_ns()
        kind = msg.get("type")
        if kind == "input_audio_buffer.speech_stopped":
            self._speech_stopped_ns = now_ns
            self._response_created_ns = None
            return
        if kind == "response.created":
            self._response_created_ns = now_ns
            return
        if kind != "response.output_audio.delta" or self._speech_stopped_ns is None:
            return
        # First delta of this reply: report, then disarm so the rest of the stream is silent.
        stopped_ns, self._speech_stopped_ns = self._speech_stopped_ns, None
        created_ns = self._response_created_ns
        if created_ns is None:
            # No response.created between the speech-stop and this delta: the delta belongs to a
            # response that began before we started watching. Say so rather than printing a split
            # with a hole in it — `nan ms` is how a missing measurement got into the AC-4 log
            # looking like a measured one.
            _log.info(
                "first token: %.0f ms from the server's own speech-stop "
                "(no response.created in between — split unavailable for this reply)",
                (now_ns - stopped_ns) / _NS_PER_MS,
            )
            return
        _log.info(
            "first token: server speech-stop -> response.created %.0f ms, "
            "-> first audio delta %.0f ms (model TTFT + one hop, NOT ours); "
            "total %.0f ms from the server's own speech-stop",
            (created_ns - stopped_ns) / _NS_PER_MS,
            (now_ns - created_ns) / _NS_PER_MS,
            (now_ns - stopped_ns) / _NS_PER_MS,
        )

    def _note_error(self, msg: dict[str, Any]) -> None:
        """Log one ``error`` server frame. **Never** ends the session (AVID-178, SDS §6.2.5).

        The rule this encodes: *the socket is the authority on whether the session is alive.* An
        error frame is the API complaining about one client event; a **close** frame is the
        session ending, and :meth:`_events` already turns that into
        :class:`~avid.core.realtime.SessionClosed`.

        No error type is treated as fatal, and that is deliberate rather than lazy. A hard-coded
        vendor error-type list is exactly what §6.10's volatility warning says will rot, and every
        fatal condition this project has actually met closed the socket instead — the GA-shape
        migration with ``4000 invalid_request_error.beta_api_shape_disabled``, and a bad key fails
        fast at boot (§3.12.3) without ever reaching a live session.

        Until AVID-178 this frame became ``SessionClosed``, so **every barge-in tore down a working
        socket**: 10 sessions in 80 s of bench, each announcing "one sec, I lost my connection" to
        a user whose connection was fine.
        """
        self._error_count += 1
        _log.warning("%s", _format_error_frame(msg, count=self._error_count))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Barge-in step 4 (§6.2.4): tell the model the user cut ``item_id`` off at
        ``audio_end_ms`` (``conversation.item.truncate``). ``content_index`` is always 0 for us —
        a vendor detail kept off the port. Arms the approximate-transcript flag (§6.2.4 trap 3)."""
        self._truncation_pending = True
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def cancel(self) -> None:
        """Barge-in step 5 (§6.2.4): cancel the in-flight response — **if there is one**.

        Sending ``response.cancel`` with nothing in flight is an error, measured against the live
        API (AVID-178)::

            code    'response_cancel_not_active'
            message 'Cancellation failed: no active response found'

        and it is the *common* case, not a corner one: **generation finishes long before playback
        does.** By the time a user interrupts a reply they are still hearing, the model stopped
        generating seconds ago. The same probe confirmed ``conversation.item.truncate`` is accepted
        with our device-derived ``audio_end_ms``, so step 4 is untouched — only step 5 needed a
        guard.

        ⚠️ **If this guard is wrong the failure is silent.** No cancel is sent, the user hears
        nothing amiss (step 6's mute drops the in-flight deltas anyway), and the model goes on
        believing it said the whole reply — §6.2.4 trap 2, poisoning the conversation context and
        billing output tokens for audio nobody heard. That is why the skip is *logged*: a bench run
        is graded on "every barge-in shows a sent cancel **or** a logged skip", never on the mere
        absence of errors.
        """
        if self._active_response is None:
            _log.debug(
                "no active response to cancel — skipping response.cancel (§6.2.4 step 5)"
            )
            return
        await self._send({"type": "response.cancel"})

    async def send_tool_output(self, call_id: str, output: str) -> None:
        """Return a tool result and prompt the model to speak (§6.6 steps 4–5).

        Two client events, in order: ``conversation.item.create`` carrying the
        ``function_call_output`` (``call_id`` echoed, ``output`` a string), **then**
        ``response.create``. The second is not optional — without it the model silently swallows
        the turn (§6.6's step-5 trap, the number-one Realtime tool-integration bug). Non-blocking
        (P8)."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        await self._send(
            {"type": "response.create"}
        )  # step 5 — or the model just sits (§6.6)

    async def _send(self, payload: dict[str, Any]) -> None:
        """Serialise and send one client event, if the socket is live. Non-blocking (P8).

        **Every client event is stamped with a traceable ``event_id``** (AVID-178). The API echoes
        it back in ``error.event_id``, which is the difference between a log line saying *"something
        you sent was rejected"* and one saying *"our ``response.cancel`` was rejected"* — the whole
        of #178's Stage 2 hypothesis ranking collapses to a fact because of these four lines.

        The id is ``avid_<n>_<type>``, restricted to ``[a-z0-9_]`` so no server-side format
        assumption is under test. It carries **no user content and no key** — only a counter and
        the message type we chose ourselves. Cost on the audio path is one increment and ~30 bytes
        against a ~1280-byte base64 frame; that is noise against P8, and it is stamped on
        ``input_audio_buffer.append`` too *because* a rejected append is exactly the error we
        currently cannot see.
        """
        if self._ws is not None:
            self._sent_seq += 1
            kind = str(payload.get("type", "unknown")).replace(".", "_")
            stamped = {"event_id": f"avid_{self._sent_seq}_{kind}", **payload}
            await self._ws.send(json.dumps(stamped))


# --- CapturingRealtimeClient (#105): record a live session into the replay format ---------


class CapturingRealtimeClient:
    """A recording decorator: wrap any :class:`~avid.core.ports.RealtimeClient` and write the
    neutral events flowing through it into the ``replay`` fixture format (AC-4).

    The **inverse** of :func:`_build_event`: what capture writes, :meth:`ReplayRealtimeClient.from_dir`
    reads back verbatim — so the committed fixtures cannot drift from real API behaviour (SDS §14.3).
    It delegates every port call to the inner client and, as each event is yielded from the inner
    :meth:`events`, appends a manifest record (``delay_ms`` measured on the **injected clock's**
    ``monotonic_ns``, never wall time — SDS §9.1.1). Assistant PCM is buffered and the WAVs +
    ``session.json`` are written once, on :meth:`aclose`, keeping :meth:`events` allocation-only.
    Stdlib only (``json``/``wave``), vendor-free — the ``openai`` client it usually wraps stays the
    only vendor-touching class. Constructed only by ``main --capture`` (P3)."""

    def __init__(self, *, inner: RealtimeClient, clock: Clock, out_dir: Path) -> None:
        self._inner = inner
        self._clock = clock
        self._out_dir = out_dir
        self._records: list[dict[str, Any]] = []
        self._pending_wavs: list[tuple[Path, AudioChunk]] = []
        self._audio_index = 0
        self._last_ns: int | None = None
        self._flushed = False

    async def open(self, *, memory: Awaitable[str] | None = None) -> None:
        await self._inner.open(memory=memory)

    async def aclose(self) -> None:
        """Close the inner session, then flush the recording once (idempotent)."""
        await self._inner.aclose()
        self._flush()

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._inner.send_audio(chunk)

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._inner.truncate(item_id, audio_end_ms)

    async def cancel(self) -> None:
        await self._inner.cancel()

    async def send_tool_output(self, call_id: str, output: str) -> None:
        await self._inner.send_tool_output(call_id, output)

    def events(self) -> AsyncIterator[RealtimeEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[RealtimeEvent]:
        """Pass each inner event straight through, recording it on the way past."""
        async for event in self._inner.events():
            self._record(event)
            yield event

    def _record(self, event: RealtimeEvent) -> None:
        """Append one manifest record for *event*; the exact inverse of :func:`_build_event`.

        ``delay_ms`` is the monotonic gap since the previous event (0 for the first). Assistant PCM
        is buffered for :meth:`_flush` rather than written here, so this stays fast and I/O-free."""
        now = self._clock.monotonic_ns()
        delay_ms = (
            0 if self._last_ns is None else max(0, (now - self._last_ns) // _NS_PER_MS)
        )
        self._last_ns = now
        record: dict[str, Any] = {"delay_ms": int(delay_ms)}
        match event:
            case UserTranscript():
                record["type"] = "user_transcript"
                record["text"] = event.text
                record["is_approximate"] = event.is_approximate
            case AssistantTranscript():
                record["type"] = "assistant_transcript"
                record["text"] = event.text
                record["item_id"] = event.item_id
            case ToolCallRequested():
                record["type"] = "tool_call_requested"
                record["call_id"] = event.call_id
                record["name"] = event.name
                record["arguments"] = event.arguments
            case AssistantAudioChunk():
                wav = f"turn{self._audio_index}.wav"
                self._audio_index += 1
                self._pending_wavs.append((self._out_dir / wav, event.chunk))
                record["type"] = "assistant_audio_chunk"
                record["item_id"] = event.item_id
                record["wav"] = wav
            case TurnDone():
                record["type"] = "turn_done"
                record["usage"] = {
                    "input_tokens": event.usage.input_tokens,
                    "cached_input_tokens": event.usage.cached_input_tokens,
                    "output_tokens": event.usage.output_tokens,
                }
            case SessionClosed():
                record["type"] = "session_closed"
                record["cause"] = event.cause
            case _:  # pragma: no cover - the union is closed; mypy proves this dead
                assert_never(event)
        self._records.append(record)

    def _flush(self) -> None:
        """Write the buffered WAVs and the ``session.json`` manifest (once). Runs at teardown, off
        the hot path — like config load, before/after the event stream, so P8 is not implicated."""
        if self._flushed:
            return
        self._flushed = True
        self._out_dir.mkdir(parents=True, exist_ok=True)
        for path, chunk in self._pending_wavs:
            self._write_wav(path, chunk)
        manifest = {"format": _SUPPORTED_FORMAT, "events": self._records}
        (self._out_dir / _MANIFEST).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _write_wav(path: Path, chunk: AudioChunk) -> None:
        """Write one :class:`AudioChunk` as an S16_LE WAV (stdlib ``wave``), the format
        :func:`_load_wav` reads back — so a captured session round-trips through replay."""
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(chunk.channels)
            handle.setsampwidth(_S16_WIDTH_BYTES)
            handle.setframerate(chunk.sample_rate)
            handle.writeframes(chunk.pcm)
