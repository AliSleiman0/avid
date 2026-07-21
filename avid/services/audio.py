"""The audio loop — move PCM between devices without ever blocking (AVID-79, SDS §3.6.1).

``AudioService`` is the §3.6.1 mover: it consumes the microphone frame by frame, runs
the local VAD gate on each frame, keeps the 300 ms pre-roll ring buffer, and turns the
result into the four ``audio.*`` facts (``domain/audio.py``, #86). It depends only on the
:class:`~avid.core.ports.Microphone`, :class:`~avid.core.ports.Speaker` and
:class:`~avid.core.ports.VoiceActivityDetector` **Protocols** — never a concrete adapter
(P2) — plus the one shared :class:`~avid.core.state_manager.StateManager` (SDS §3.8.4).

It is the **first service that owns a background task**: :meth:`start` spins up the
mic-consume loop and :meth:`stop` cancels it, which is why this milestone is where the
:class:`~avid.core.ports.Service` Protocol and ``lifecycle.run(services=)`` finally land
(AVID-72/73 deferred them until a service actually owned a task).

**The M4 AI-client leg is a loopback — read this before you go looking for the model.**
There is no ``ConversationService`` until M5, so a completed utterance is echoed straight
back to the speaker (:meth:`_loopback`) to prove the round-trip path. That pass-through
*is* the AI client at M4; in M5 it is replaced by the Realtime response stream and this
service becomes a genuine ``conversation.*`` consumer. Two consequences of that seam:

* :meth:`subscriptions` returns ``()`` — the ``conversation.*`` Event types it will one
  day subscribe to do not exist yet (only the ``Trigger`` enum names them as strings).
* The loopback publishes the two playback facts but does **not** drive the
  ``THINKING → SPEAKING → IDLE`` arc: that arc belongs to a real conversation turn, and
  nothing legitimately reaches THINKING at M4. The service drives the state machine on
  the **speech-start edge only** (:meth:`_begin_speech`), where one
  ``audio.speech_started`` trigger covers both the normal IDLE/SLEEPING → LISTENING move
  and the barge-in SPEAKING → LISTENING one (SDS §3.10.3).

Purity of the hot path (P8, AC-6): :meth:`~avid.core.ports.VoiceActivityDetector.is_speech`
is synchronous and sub-ms by contract (SDS §9.3), so it is called **inline** — no
executor, no loop hop. The blocking device reads/writes live in the adapter threads
(``AlsaMicrophone``/``AlsaSpeaker``), never here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from uuid import UUID, uuid4

from avid.core.envelope import envelope
from avid.core.event_bus import Subscription
from avid.core.hal import AudioChunk
from avid.core.ports import (
    Clock,
    EventBus,
    Microphone,
    Speaker,
    VoiceActivityDetector,
)
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    RobotState,
    Trigger,
)

_log = logging.getLogger(__name__)

# The component name stamped on the events this module publishes (SDS §9.1.3).
_SOURCE = "AudioService"

# S16_LE, 2 bytes/sample/channel — the one PCM format the mic/speaker adapters exchange
# (see microphone.py / speaker.py). AudioChunk carries no bit-depth field, so it is implicit.
_SAMPLE_WIDTH_BYTES = 2


def _pcm_ms(pcm: bytes, *, sample_rate: int, channels: int) -> int:
    """Milliseconds of S16_LE *pcm* — its byte length over the bytes-per-ms of its format.

    Floors. Format is a parameter, not a constant: mic capture is 16 kHz (32 bytes/ms) and
    Realtime playback is 24 kHz (48 bytes/ms), so the same arithmetic serves both — which
    is exactly AC-3's "÷ 48 at 24 kHz mono 16-bit" for real audio and ÷ 32 for the loopback
    echo of 16 kHz capture.
    """
    denom = sample_rate * channels * _SAMPLE_WIDTH_BYTES
    return len(pcm) * 1000 // denom if denom else 0


class AudioService:
    """Consume the mic, gate on VAD, keep the pre-roll, publish ``audio.*`` (SDS §3.6.1).

    Satisfies the :class:`~avid.core.ports.Service` shape (``name``/``start``/``stop``/
    ``subscriptions``) — and, unlike the two reactive services, actually uses ``start``/
    ``stop`` to manage its owned mic loop. Constructed once, in the composition root (#89);
    everything it touches is a port or the injected ``StateManager`` (P2, P3).
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        state: StateManager,
        microphone: Microphone,
        speaker: Speaker,
        vad: VoiceActivityDetector,
        ring_buffer_ms: int,
        sample_rate: int,
        channels: int,
        silence_hold_ms: int,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = state
        self._mic = microphone
        self._speaker = speaker
        self._vad = vad
        # Capture format, injected (P7). Mic-side bytes/ms frames the pre-roll ring buffer
        # in raw bytes — it never names AudioChunk, a core type the domain may not import
        # (#86, P1). 16 kHz mono S16_LE ⇒ 32 bytes/ms.
        self._sample_rate = sample_rate
        self._channels = channels
        self._bytes_per_ms = max(
            1, sample_rate * channels * _SAMPLE_WIDTH_BYTES // 1000
        )
        self._preroll = AudioPreRoll(
            capacity_ms=ring_buffer_ms, bytes_per_ms=self._bytes_per_ms
        )
        # How long a run of silence must last before a turn is declared over — the
        # debounce that stops per-frame flapping (AC-2). Server-VAD's silence_duration_ms.
        self._silence_hold_ms = silence_hold_ms

        # Per-turn state, all reset by _reset_turn(). ``_speaking`` is the edge detector;
        # ``_turn_id`` is the correlation_id this service *mints* at a turn origin (SDS
        # §9.1.1) and every downstream event of the turn propagates.
        self._speaking = False
        self._turn_id: UUID | None = None
        self._utterance = bytearray()
        self._speech_ms = 0
        self._silence_run_ms = 0
        self._playback_seq = 0

        # The owned mic-consume task (SDS §9.2): None until start(), cleared by stop().
        self._task: asyncio.Task[None] | None = None

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Launch the mic-consume loop as an owned task. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="AudioService.mic_loop")

    async def stop(self) -> None:
        """Cancel the mic loop and await it, within the §9.2 5 s budget. Idempotent.

        Cancelling while the loop is parked on the mic stream throws ``CancelledError``
        into the generator's ``await``, so its ``finally`` runs and the device is released
        (``FakeMicrophone.closed`` / the ALSA handle) before this returns.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare, do not register (SDS §9.2) — but there is nothing to declare yet.

        AudioService's inventory row (§3.6.1) subscribes to ``conversation.*``: in M5 it
        plays the assistant's audio deltas that ``ConversationService`` publishes. Those
        Event *types* do not exist at M4 (only the ``Trigger`` enum names them as strings),
        so there is nothing to subscribe to, and the loopback stands in for the AI-client
        leg. The real subscriptions land with those facts. Shaped as the port asks all the
        same, so wiring this service is identical the day they arrive.
        """
        return ()

    # --- the mic loop --------------------------------------------------------------------

    async def _run(self) -> None:
        """Consume the mic forever, judging and buffering every frame (AC-1).

        Runs until :meth:`stop` cancels it. ``is_speech`` is inline (sync, sub-ms — P8);
        the pre-roll is fed on every frame so a turn can replay the phonemes it missed.
        """
        async for chunk in self._mic.stream():
            speech = self._vad.is_speech(chunk)
            self._preroll.append(chunk.pcm)
            frame_ms = _pcm_ms(
                chunk.pcm, sample_rate=chunk.sample_rate, channels=chunk.channels
            )
            if speech:
                if not self._speaking:
                    await (
                        self._begin_speech()
                    )  # rising edge — seeds utterance from pre-roll
                else:
                    self._utterance += chunk.pcm  # subsequent speech frame
                self._speech_ms += frame_ms
                self._silence_run_ms = 0
            elif self._speaking:
                # Trailing silence is still part of the captured clip; count it toward the
                # debounce and end the turn once it has lasted long enough (AC-2).
                self._utterance += chunk.pcm
                self._silence_run_ms += frame_ms
                if self._silence_run_ms >= self._silence_hold_ms:
                    await self._end_speech()
            # else: idle silence — the pre-roll rolls, nothing is published.

    async def _begin_speech(self) -> None:
        """Rising edge: a turn begins. Mint its id, replay the pre-roll, publish, transition.

        **A turn origin** (SDS §9.1.1): this is where a fresh ``correlation_id`` is minted;
        every downstream event of the turn propagates it. The drained pre-roll seeds the
        utterance so the loopback echoes the leading phonemes the gate would otherwise miss.

        Barge-in (AC-5): if the robot is mid-utterance (``SPEAKING``), cut playback
        **before** the transition, so ``SPEAKING → LISTENING`` lands on a silent speaker —
        ``Speaker.stop`` is on the port precisely so this is immediate (SDS §3.10.3).
        """
        self._speaking = True
        self._turn_id = uuid4()
        pre = self._preroll.drain()  # includes this first speech frame (appended above)
        self._utterance = bytearray(pre)
        self._speech_ms = 0
        self._silence_run_ms = 0
        ring_buffer_ms = len(pre) // self._bytes_per_ms

        if self._state.state is RobotState.SPEAKING:
            await self._speaker.stop()

        await self._bus.publish(
            AudioSpeechStarted(
                **envelope(
                    clock=self._clock,
                    correlation_id=self._turn_id,
                    source=_SOURCE,
                ),
                ring_buffer_ms=ring_buffer_ms,
            )
        )
        # Into LISTENING. One trigger covers every legal source — IDLE/SLEEPING (normal)
        # and SPEAKING (barge-in); StateManager logs-and-ignores a stray edge from anywhere
        # else (SDS §3.10.3), which is the right thing rather than a crash.
        await self._state.transition(
            Trigger.AUDIO_SPEECH_STARTED, correlation_id=self._turn_id
        )

    async def _end_speech(self) -> None:
        """Falling edge (debounced): the turn is over. Publish ``audio.speech_ended``,
        then run the loopback, then reset for the next turn.

        ``duration_ms`` is the speech length — the sum of the *speech* frames, excluding
        the trailing silence hangover that triggered the end.
        """
        turn_id = self._turn_id
        assert turn_id is not None  # set on the rising edge that reached here
        await self._bus.publish(
            AudioSpeechEnded(
                **envelope(clock=self._clock, correlation_id=turn_id, source=_SOURCE),
                duration_ms=self._speech_ms,
            )
        )
        await self._loopback(turn_id)
        self._reset_turn()

    async def _loopback(self, turn_id: UUID) -> None:
        """The M4 AI-client stand-in (AC-4): echo the captured utterance back to the speaker.

        Publishes ``audio.playback_started`` / ``audio.playback_finished`` around a single
        ``Speaker.play`` of the whole utterance, propagating *turn_id* so one grep on it
        reconstructs the turn including its echo. ``played_ms`` is what the speaker actually
        emitted (SDS:1982), computed from the played PCM's own format; ``truncated`` is
        always ``False`` at M4 (no barge-in truncation path yet).

        Deliberately does **not** drive the state machine: the ``THINKING → SPEAKING →
        IDLE`` arc is a real conversation turn's, and there is no ConversationService at M4
        to reach THINKING — so the robot stays in LISTENING while the echo plays, and
        barge-in against genuine SPEAKING playback is exercised by a SPEAKING-initialised
        unit test rather than reached through the loopback. In M5 this whole method is
        replaced by the Realtime response stream.
        """
        self._playback_seq += 1
        item_id = f"loopback-{self._playback_seq}"
        pcm = bytes(self._utterance)
        await self._bus.publish(
            AudioPlaybackStarted(
                **envelope(clock=self._clock, correlation_id=turn_id, source=_SOURCE),
                item_id=item_id,
            )
        )
        await self._speaker.play(
            AudioChunk(pcm=pcm, sample_rate=self._sample_rate, channels=self._channels)
        )
        played_ms = _pcm_ms(pcm, sample_rate=self._sample_rate, channels=self._channels)
        await self._bus.publish(
            AudioPlaybackFinished(
                **envelope(clock=self._clock, correlation_id=turn_id, source=_SOURCE),
                item_id=item_id,
                played_ms=played_ms,
                truncated=False,
            )
        )
        _log.debug(
            "loopback %s echoed %d ms [correlation_id=%s]",
            item_id,
            played_ms,
            turn_id,
        )

    def _reset_turn(self) -> None:
        """Clear per-turn state so the next silence starts fresh."""
        self._speaking = False
        self._turn_id = None
        self._utterance = bytearray()
        self._speech_ms = 0
        self._silence_run_ms = 0
