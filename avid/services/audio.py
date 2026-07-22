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

**The assistant-audio seam is the ``TurnSink`` port, not the bus** (#103, §9.1.4).
``AudioService`` *implements* :class:`~avid.core.ports.TurnSink`: captured mic PCM flows **up**
to ``ConversationService`` via :meth:`mic`, and the assistant's PCM flows **down** via
:meth:`play` / :meth:`end_response` / :meth:`interrupt`. Those are **direct calls, never bus
events** (PCM does not belong on an at-most-once bus) — which is why this service is *not* a
``conversation.*`` subscriber (:meth:`subscriptions` returns ``()``); the §3.6.1 inventory row
was corrected accordingly. On the playback side it drives the ``THINKING → SPEAKING`` edge
(:meth:`play`) and the ``SPEAKING → IDLE`` edge (:meth:`end_response`); on the capture side it
still owns the ``audio.speech_started`` turn origin and the barge-in ``SPEAKING → LISTENING``
move (:meth:`_begin_speech`).

**The M4 loopback survives behind a flag.** Before ``ConversationService`` existed, a completed
utterance was echoed straight back to the speaker (:meth:`_loopback`) to prove the round-trip;
that path is retained under ``loopback=True`` for the #91 on-Pi *transport* gate
(``docs/demos/audio_pi.py``), where there is no AI client and the echo is the whole downstream
path. With ``loopback=False`` (the running robot) :meth:`_end_speech` instead hands the captured
utterance up the ``TurnSink`` seam. The loopback publishes the playback facts but deliberately
does **not** drive the state arc (nothing reaches THINKING without a real turn).

Purity of the hot path (P8, AC-6): :meth:`~avid.core.ports.VoiceActivityDetector.is_speech`
is synchronous and sub-ms by contract (SDS §9.3), so it is called **inline** — no
executor, no loop hop. The blocking device reads/writes live in the adapter threads
(``AlsaMicrophone``/``AlsaSpeaker``), never here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
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
    """Consume the mic, gate on VAD, keep the pre-roll, publish ``audio.*``; be the ``TurnSink``.

    Satisfies **two** structural contracts: the :class:`~avid.core.ports.Service` shape
    (``name``/``start``/``stop``/``subscriptions``) whose ``start``/``stop`` manage its owned mic
    loop, and the :class:`~avid.core.ports.TurnSink` port (``mic``/``play``/``end_response``/
    ``interrupt``) that ``ConversationService`` pushes a turn's audio through (§9.1.4). The two
    ``stop``\\ s would collide, so the port's barge-in method is named :meth:`interrupt` — a
    lifecycle ``stop`` (cancel the loop) is a different act from a barge-in (cut playback, report
    what actually played). Constructed once, in the composition root (#89/#103); everything it
    touches is a port or the injected ``StateManager`` (P2, P3).
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
        loopback: bool = False,
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

        # True = M4 echo (the #91 transport gate); False = the M5 TurnSink seam (the running
        # robot). Named ``_loopback_mode`` so it does not shadow the :meth:`_loopback` method.
        self._loopback_mode = loopback

        # Per-turn capture state, reset by _reset_capture(). ``_speaking`` is the edge detector;
        # ``_turn_id`` is the correlation_id this service *mints* at a turn origin (SDS §9.1.1)
        # and every downstream event of the turn propagates — including the assistant playback,
        # so it outlives _end_speech in seam mode.
        self._speaking = False
        self._turn_id: UUID | None = None
        self._utterance = bytearray()
        self._speech_ms = 0
        self._silence_run_ms = 0
        self._playback_seq = 0

        # Captured mic PCM handed up the TurnSink seam (drained by :meth:`mic`), and the
        # in-flight assistant playback (its item, the ms actually emitted, and the turn it
        # belongs to — kept separate from _turn_id so a barge-in keeps the *interrupted*
        # playback's own correlation).
        self._mic_out: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self._playing_item: str | None = None
        self._playing_ms = 0
        self._playing_corr: UUID | None = None

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
        """Declare, do not register (SDS §9.2) — and there is nothing to declare, ever.

        ``AudioService`` subscribes to **nothing**. The §3.6.1 inventory once listed
        ``conversation.*`` here, but that was drift (#103, AC-3): the assistant's audio does not
        arrive on the bus — it is pushed directly through the :class:`~avid.core.ports.TurnSink`
        port this service implements (:meth:`play` / :meth:`end_response`, §9.1.4). PCM on an
        at-most-once bus would be a bug, not a feature. So the shape is honoured with an empty
        tuple, and the composition root registers nothing for this service.
        """
        return ()

    # --- the TurnSink seam (#103, §9.1.4) ------------------------------------------------

    def mic(self) -> AsyncIterator[AudioChunk]:
        """Captured mic PCM, up (``TurnSink``). See :meth:`_mic_up`."""
        return self._mic_up()

    async def _mic_up(self) -> AsyncIterator[AudioChunk]:
        """Yield each captured utterance as :meth:`_end_speech` hands it over — a **live,
        unbounded** stream (a real mic never ends), unlike ``FakeTurnSink``'s finite script.
        ``ConversationService`` drains this for the session's life and cancels it on close."""
        while True:
            yield await self._mic_out.get()

    async def play(self, chunk: AudioChunk, *, item_id: str) -> None:
        """Play one assistant PCM delta to the speaker (``TurnSink`` down, §9.1.4).

        The **first** delta of a response opens playback: publish ``audio.playback_started`` and
        drive ``THINKING → SPEAKING`` (the playback belongs to the turn whose ``correlation_id``
        this service minted at ``speech_started``, so it is stamped with that, captured now so a
        later barge-in keeps it). Subsequent deltas just accumulate ``played_ms`` and stream. The
        ``Speaker.play`` await means that by :meth:`end_response` the audio has actually gone out.
        """
        if self._playing_item is None:
            self._playing_item = item_id
            self._playing_ms = 0
            self._playing_corr = self._turn_id
            corr = self._playback_corr()
            await self._bus.publish(
                AudioPlaybackStarted(
                    **envelope(clock=self._clock, correlation_id=corr, source=_SOURCE),
                    item_id=item_id,
                )
            )
            await self._state.transition(
                Trigger.AUDIO_PLAYBACK_STARTED, correlation_id=corr
            )
        self._playing_ms += _pcm_ms(
            chunk.pcm, sample_rate=chunk.sample_rate, channels=chunk.channels
        )
        await self._speaker.play(chunk)

    async def end_response(self) -> None:
        """Normal end of the response's audio (``TurnSink``, §9.1.4).

        Publishes ``audio.playback_finished`` (``truncated=False``) with the ms actually emitted
        and drives ``SPEAKING → IDLE``. A no-op when nothing is playing (a turn with no audio).
        Distinct from :meth:`interrupt`, whose barge-in ends in LISTENING, not IDLE."""
        if self._playing_item is None:
            return
        await self._finish_playback(truncated=False)
        await self._state.transition(
            Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=self._playback_corr()
        )
        self._clear_playback()

    async def interrupt(self) -> int:
        """Barge-in: cut playback immediately and report the ms the speaker **actually** emitted
        (``TurnSink``, SDS §6.2.4). Idempotent — 0 when nothing is playing.

        Stops the speaker (on the port so this is instant), publishes the truncated
        ``audio.playback_finished`` fact, and returns ``played_ms`` — the honest ``audio_end_ms``
        the model-side truncate (#104) needs. It does **not** drive a transition: the
        ``SPEAKING → LISTENING`` move is the ``speech_started`` origin's (see :meth:`_begin_speech`).
        """
        await self._speaker.stop()
        played_ms = self._playing_ms
        if self._playing_item is not None:
            await self._finish_playback(truncated=True)
            self._clear_playback()
        return played_ms

    async def _finish_playback(self, *, truncated: bool) -> None:
        """Publish ``audio.playback_finished`` for the in-flight item (caller clears state)."""
        assert self._playing_item is not None  # guarded by every caller
        await self._bus.publish(
            AudioPlaybackFinished(
                **envelope(
                    clock=self._clock,
                    correlation_id=self._playback_corr(),
                    source=_SOURCE,
                ),
                item_id=self._playing_item,
                played_ms=self._playing_ms,
                truncated=truncated,
            )
        )

    def _playback_corr(self) -> UUID:
        """The correlation_id of the in-flight playback (the turn that opened it)."""
        assert self._playing_corr is not None  # set when playback opened
        return self._playing_corr

    def _clear_playback(self) -> None:
        """Reset the playback lifecycle for the next response."""
        self._playing_item = None
        self._playing_ms = 0
        self._playing_corr = None

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
        :meth:`interrupt` is instant and also emits the truncated ``audio.playback_finished``
        fact for the interrupted response. Off the SPEAKING path, clear any stale playback (a
        response left un-finalized by a lost session gets no ``end_response``).
        """
        self._speaking = True
        self._turn_id = uuid4()
        pre = self._preroll.drain()  # includes this first speech frame (appended above)
        self._utterance = bytearray(pre)
        self._speech_ms = 0
        self._silence_run_ms = 0
        ring_buffer_ms = len(pre) // self._bytes_per_ms

        if self._state.state is RobotState.SPEAKING:
            await (
                self.interrupt()
            )  # barge-in: stops the speaker, finalizes the old playback
        else:
            self._clear_playback()  # drop any playback the previous turn never finished

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
        """Falling edge (debounced): the user's turn is over. Publish ``audio.speech_ended``,
        then either echo (loopback) or hand the utterance up the ``TurnSink`` seam.

        ``duration_ms`` is the speech length — the sum of the *speech* frames, excluding the
        trailing silence hangover that triggered the end. In seam mode the captured utterance is
        put on the mic-up queue and the **capture** state is reset while ``_turn_id`` is kept
        alive — the assistant playback that follows is the same turn (SDS §3.12.2). In loopback
        mode the whole turn (id included) is reset once the echo has played.
        """
        turn_id = self._turn_id
        assert turn_id is not None  # set on the rising edge that reached here
        await self._bus.publish(
            AudioSpeechEnded(
                **envelope(clock=self._clock, correlation_id=turn_id, source=_SOURCE),
                duration_ms=self._speech_ms,
            )
        )
        if self._loopback_mode:
            await self._loopback(turn_id)
            self._reset_turn()
        else:
            await self._mic_out.put(
                AudioChunk(
                    pcm=bytes(self._utterance),
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                )
            )
            self._reset_capture()

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

    def _reset_capture(self) -> None:
        """Clear the speech-capture state so the next silence starts fresh, **keeping**
        ``_turn_id`` — in seam mode the assistant playback still to come is the same turn."""
        self._speaking = False
        self._utterance = bytearray()
        self._speech_ms = 0
        self._silence_run_ms = 0

    def _reset_turn(self) -> None:
        """Full reset including ``_turn_id`` — the loopback turn is wholly done once echoed."""
        self._reset_capture()
        self._turn_id = None
