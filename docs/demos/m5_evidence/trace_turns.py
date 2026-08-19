"""Diagnostic: trace every audio/conversation event on the real Pi stack (#153 regression hunt).

Same wiring as docs/demos/conversation_pi.py, but the only consumer is a tracer that prints
one line per bus event with a monotonic timestamp and the correlation id. The question it
answers: after turn 1, does `audio.speech_started` still fire? Does the session reopen? Does
mic PCM still reach the client?

Throwaway. Lives in the scratchpad, never committed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

sys.path.insert(0, "/opt/avid")

from avid.adapters.clock import SystemClock  # noqa: E402
from avid.core.config import load_config  # noqa: E402
from avid.core.event_bus import AsyncioEventBus  # noqa: E402
from avid.core.state_manager import StateManager  # noqa: E402
from avid.domain import (  # noqa: E402
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    AudioSpeechStarted,
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Event,
    RobotState,
    SystemDegradedEntered,
    SystemHandlerFailed,
)
from avid.main import (  # noqa: E402
    _build_cue_bank,
    _build_microphone,
    _build_realtime,
    _build_speaker,
    _build_vad,
)
from avid.services import AudioService, ConversationService  # noqa: E402

_T0 = time.monotonic()

_TRACED = (
    AudioSpeechStarted,
    AudioSpeechEnded,
    AudioPlaybackStarted,
    AudioPlaybackFinished,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationSessionLost,
    SystemDegradedEntered,
    SystemHandlerFailed,
)


class _CountingMic:
    """Count frames actually yielded by the real microphone, and report a stream that ends/raises.

    ``AudioService._run`` iterates this. If the generator raises, the owned task dies and *nothing*
    is logged — the service simply goes deaf forever. That silent death is the hypothesis this
    wrapper exists to confirm or kill."""

    def __init__(self, inner):  # type: ignore[no-untyped-def]
        self._inner = inner
        self.frames = 0
        self.ended = None

    def stream(self):  # type: ignore[no-untyped-def]
        return self._stream()

    async def _stream(self):  # type: ignore[no-untyped-def]
        try:
            async for chunk in self._inner.stream():
                self.frames += 1
                yield chunk
            self.ended = "generator returned normally"
        except asyncio.CancelledError:
            self.ended = "cancelled (normal shutdown)"
            raise
        except BaseException as exc:  # noqa: BLE001 - diagnostic
            self.ended = f"RAISED {exc!r}"
            raise


class _CountingVad:
    """Count VAD calls and positive verdicts, so a live-but-deaf loop is distinguishable."""

    def __init__(self, inner):  # type: ignore[no-untyped-def]
        self._inner = inner
        self.calls = 0
        self.speech = 0

    def is_speech(self, frame):  # type: ignore[no-untyped-def]
        self.calls += 1
        verdict = self._inner.is_speech(frame)
        if verdict:
            self.speech += 1
        return verdict


class _CountingClient:
    """Wrap the real RealtimeClient and count/timestamp every port call.

    ``OpenAIRealtimeClient`` has no ``.sent`` list (that is the replay fake's off-port trace), so
    the first version of this tracer printed a meaningless 0. Count the calls instead."""

    def __init__(self, inner):  # type: ignore[no-untyped-def]
        self._inner = inner
        self.sent = 0
        self.opens = 0
        self.closes = 0

    async def open(self, *, memory=None):  # type: ignore[no-untyped-def]
        t = time.monotonic()
        self.opens += 1
        print(f"{t - _T0:8.3f}s  >>> client.open() #{self.opens} ...", flush=True)
        try:
            await self._inner.open(memory=memory)
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(
                f"{time.monotonic() - _T0:8.3f}s  >>> client.open() RAISED {exc!r}",
                flush=True,
            )
            raise
        print(
            f"{time.monotonic() - _T0:8.3f}s  >>> client.open() #{self.opens} done "
            f"in {(time.monotonic() - t) * 1000:.0f} ms",
            flush=True,
        )

    async def aclose(self):  # type: ignore[no-untyped-def]
        self.closes += 1
        print(
            f"{time.monotonic() - _T0:8.3f}s  >>> client.aclose() #{self.closes}",
            flush=True,
        )
        await self._inner.aclose()

    async def send_audio(self, chunk):  # type: ignore[no-untyped-def]
        self.sent += 1
        await self._inner.send_audio(chunk)

    async def truncate(self, item_id, audio_end_ms):  # type: ignore[no-untyped-def]
        print(
            f"{time.monotonic() - _T0:8.3f}s  >>> truncate({item_id}, {audio_end_ms})",
            flush=True,
        )
        await self._inner.truncate(item_id, audio_end_ms)

    async def cancel(self):  # type: ignore[no-untyped-def]
        print(f"{time.monotonic() - _T0:8.3f}s  >>> cancel()", flush=True)
        await self._inner.cancel()

    async def send_tool_output(self, call_id, output):  # type: ignore[no-untyped-def]
        await self._inner.send_tool_output(call_id, output)

    def events(self):  # type: ignore[no-untyped-def]
        return self._inner.events()


class _NoMemory:
    async def remember_fact(self, text, kind, importance, *, correlation_id=None):  # type: ignore[no-untyped-def]
        return 0

    async def recall(self, query, *, k=5, correlation_id=None):  # type: ignore[no-untyped-def]
        return ()

    async def forget(self, query, *, correlation_id=None):  # type: ignore[no-untyped-def]
        return 0

    async def top_facts(self):  # type: ignore[no-untyped-def]
        return ()


def _tracer(state: StateManager, client, audio: AudioService):  # type: ignore[no-untyped-def]
    async def handle(event: Event) -> None:
        detail = ""
        if isinstance(event, AudioSpeechEnded):
            detail = f" duration_ms={event.duration_ms}"
        elif isinstance(event, AudioSpeechStarted):
            detail = f" ring_buffer_ms={event.ring_buffer_ms}"
        elif isinstance(event, AudioPlaybackFinished):
            detail = f" played_ms={event.played_ms} truncated={event.truncated}"
        elif isinstance(event, ConversationUserTranscribed):
            detail = f" text={event.text[:60]!r}"
        elif isinstance(event, ConversationAssistantResponded):
            detail = f" text={event.text[:60]!r}"
        elif isinstance(event, SystemHandlerFailed):
            detail = f" handler={event.handler} reason={event.reason}"
        sent = client.sent
        print(
            f"{time.monotonic() - _T0:8.3f}s  {type(event).__name__:<32}"
            f" corr={str(event.correlation_id)[:8]} state={state.state.name:<9}"
            f" qsize={audio._mic_out.qsize():<4} sent={sent}{detail}",
            flush=True,
        )

    return handle


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "/etc/robot/config.toml")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

    clock = SystemClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)
    speaker = _build_speaker(config)
    mic = _CountingMic(_build_microphone(config))
    vad = _CountingVad(_build_vad(config))
    audio = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=config.gate.ring_buffer_ms,
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        silence_hold_ms=config.gate.silence_hold_ms,
        barge_in_margin_db=config.gate.barge_in_margin_db,
        highpass_hz=config.gate.highpass_hz,
        highpass_order=config.gate.highpass_order,
        echo_tail_ms=config.gate.echo_tail_ms,
        capture_stall_s=config.gate.capture_stall_s,
        loopback=False,
    )
    client = _CountingClient(_build_realtime(config, clock=clock))
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=audio,
        cues=_build_cue_bank(config, speaker=speaker),
        memory=_NoMemory(),
        session_idle_close_s=config.gate.session_idle_close_s,
        memory_inject_timeout_s=config.gate.memory_inject_timeout_s,
        think_timeout_s=config.gate.think_timeout_s,
        server_turn_detection=config.ai.turn_detection.server_is_an_authority,
        thinking_delay_ms=config.cues.thinking_delay_ms,
    )

    for sub in conversation.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    handle = _tracer(state, client, audio)
    for cls in _TRACED:
        bus.subscribe(cls, handle, name=f"trace.{cls.__name__}")

    print(
        "TRACE: speak whenever you like. Columns: t, event, corr, state, "
        "mic-queue depth, mic frames sent to the API.",
        flush=True,
    )

    async def heartbeat() -> None:
        """Every second: is the mic loop alive, is it reading frames, is the VAD hearing anything?

        This is the instrument that does not depend on anyone speaking. A dead ``_run`` task shows
        up as ``TASK DEAD`` with its exception; a live-but-starved one shows frames frozen."""
        last_frames = -1
        while True:
            await asyncio.sleep(1.0)
            task = audio._task
            dead = ""
            if task is not None and task.done():
                exc = task.exception() if not task.cancelled() else "cancelled"
                dead = f"  *** MIC TASK DEAD: {exc!r} ***"
            stalled = "  <-- FROZEN" if mic.frames == last_frames else ""
            print(
                f"{time.monotonic() - _T0:8.3f}s  .hb  mic_frames={mic.frames:<6}"
                f" vad_calls={vad.calls:<6} vad_speech={vad.speech:<5}"
                f" qsize={audio._mic_out.qsize():<4} sent={client.sent:<5}"
                f" state={state.state.name:<9} mic_ended={mic.ended}{stalled}{dead}",
                flush=True,
            )
            last_frames = mic.frames

    async with bus:
        await audio.start()
        await conversation.start()
        hb = asyncio.create_task(heartbeat())
        try:
            await asyncio.sleep(seconds)
        finally:
            hb.cancel()
            await conversation.stop()
            await audio.stop()
    print("TRACE: done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
