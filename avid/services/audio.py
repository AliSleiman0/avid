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
owns the ``audio.speech_started`` turn origin with its ``→ LISTENING`` move including barge-in
(:meth:`_begin_speech`) and, since AVID-158, the ``LISTENING → THINKING`` turn-end edge
(:meth:`_end_speech`). **The whole turn arc is therefore driven from this service's own local
facts** — no vendor event, no network round-trip, nothing that can arrive late or not at all.
That is the point: the edge used to hang off ``conversation.user_transcribed``, which is a
separate transcription pass and lands *after* the assistant is already speaking.

**The M4 loopback survives behind a flag, and it is the one path that still buffers.** Before
``ConversationService`` existed, a completed utterance was echoed straight back to the speaker
(:meth:`_loopback`) to prove the round-trip; that path is retained under ``loopback=True`` for the
#91 on-Pi *transport* gate (``docs/demos/audio_pi.py``), where there is no AI client and the echo
is the whole downstream path — an echo needs the whole clip, so that mode accumulates
``_utterance`` and plays it at the falling edge. The loopback publishes the playback facts but
deliberately drives **no** transition of its own, so at M4 the machine sits in THINKING while
the echo plays and is carried out of it by the next turn's rising edge (AVID-158). That is a
mode-independent consequence of the falling edge, not a special case: :meth:`_capture` remains
the one place the two modes are allowed to diverge.

**With ``loopback=False`` (the running robot) capture is streamed, not buffered** (#153, §6.3).
The gate's own words are *"replay the 300 ms pre-speech ring buffer → stream live"*: the drained
pre-roll is handed up the ``TurnSink`` the instant the rising edge fires, and every frame after it
goes up as it is captured, **including the trailing silence** — the server's own
``[ai.turn_detection] silence_duration_ms`` cannot fire on audio it never receives, so cutting the
stream at the falling edge would leave the turn uncommitted forever. Buffering the whole utterance
and flushing it at ``_end_speech`` (what this service did until #153) put three delays in series
where the design has one — our ``silence_hold_ms`` hold, the blob upload, then the server hunting
the same silence *inside* the blob — and cost M5 its O1 objective: P50 1350 ms measured against a
800 ms budget, with a floor of 1004 ms. :meth:`_capture` is the one place the two modes diverge.

**Streamed live, except while the robot itself is talking** (AVID-159, §6.2.4). The uplink is
half-duplex: nothing crosses the seam from the moment a reply starts playing until
``[gate] echo_tail_ms`` after it ends, because the mic hears the speaker and streaming that echo up
made the model hear *itself* — the server's turn detection saw near-continuous audio and stopped
committing turns, killing the conversation with the socket still open. Barge-in survives the gate on
**loudness**: a rising edge inside that window is the user only if it clears
:class:`~avid.domain.EchoFloor` by ``[gate] barge_in_margin_db``. It has to be loudness, because the
robot's voice is speech too and the VAD is right to say so. The margin is consulted *only* inside
that window, so ordinary turn-taking is untouched by it.

**Two tasks, one playback episode** (AVID-174). This service is driven from *both* sides at once:
:meth:`AudioService.play` and :meth:`AudioService.end_response` are called on
``ConversationService``'s Realtime pump, while :meth:`AudioService.interrupt` fires from this
service's own mic loop the instant a barge-in clears the margin. They share ``_playing_item`` /
``_playing_ms`` / ``_playing_corr``, and nothing serialises them — a lock would have to span the
speaker write and would park the mic loop behind it, which is the starvation AVID-153/159 just
fixed. So the episode is protected two other ways instead: :meth:`AudioService._take_playback`
closes it **synchronously** (no ``await`` inside, so exactly one caller can ever take it, which is
what makes ``end_response`` and ``interrupt`` mutually exclusive finalizers), and a **playback
epoch** lets :meth:`AudioService.play` notice that the episode it was writing to ended while its
write was in flight and discard the result rather than write it back. Before that, a barge-in
landing mid-write killed the pump with an ``AssertionError`` and the conversation with it.

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
from dataclasses import dataclass
from uuid import UUID, uuid4

from avid.core.envelope import envelope
from avid.core.event_bus import Subscription
from avid.core.hal import SAMPLE_WIDTH_BYTES, AudioChunk, pcm_duration_ms
from avid.core.ports import (
    Clock,
    EventBus,
    Microphone,
    Speaker,
    VoiceActivityDetector,
)
from avid.core.state_manager import StateManager
from avid.core.tasks import spawn
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    EchoFloor,
    Trigger,
    rms_dbfs,
)
from avid.domain.audio import SILENCE_DBFS

_log = logging.getLogger(__name__)

# The component name stamped on the events this module publishes (SDS §9.1.3).
_SOURCE = "AudioService"

# Depth of the mic-up queue, in frames (#153). Streaming per frame means the queue only stays
# short while somebody drains it — and nobody does between sessions, or while the robot is
# DEGRADED and every ``open()`` is failing. Unbounded, that leaks captured audio forever; bounded,
# the worst case is a loud, finite drop. ≈10 s at the 20 ms ``[microphone] chunk_ms`` the configs
# ship — long enough that the ~200 ms session-open backlog never comes near it.
_MIC_QUEUE_FRAMES = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class _Episode:
    """A closed playback episode, taken by value at the moment it ended (AVID-174).

    Service-local, not a domain value — it never crosses a port or the bus, so it stays here.
    Its whole purpose is to be read *after* an ``await``: the fields were sampled inside
    :meth:`AudioService._take_playback`'s synchronous section, so a caller publishing from a
    snapshot cannot observe the half-cleared state a concurrent close would otherwise expose.
    """

    item_id: str
    played_ms: int
    corr: UUID


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
        # Required, not defaulted (AVID-180). These carried defaults of 6.0/150 until the bench
        # tried to *tune* the margin and found the gate harness had never passed them: the
        # defaults matched the shipped config, so the drop was invisible until the moment the
        # value was supposed to change. Every echo-gate line ever recorded said "margin 6.0 dB"
        # whatever the config held. Required turns each omission into a mypy error at the call
        # site instead of a wrong number in a gate report.
        barge_in_margin_db: float,
        echo_tail_ms: int,
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
        self._bytes_per_ms = max(1, sample_rate * channels * SAMPLE_WIDTH_BYTES // 1000)
        self._preroll = AudioPreRoll(
            capacity_ms=ring_buffer_ms, bytes_per_ms=self._bytes_per_ms
        )
        # How long a run of silence must last before a turn is declared over — the
        # debounce that stops per-frame flapping (AC-2). Server-VAD's silence_duration_ms.
        self._silence_hold_ms = silence_hold_ms

        # The echo gate (AVID-159, §6.2.4). ``_echo_floor`` tracks what the mic hears — ambience
        # normally, the robot's own voice while it speaks — so the margin is measured against the
        # room rather than an absolute level, and the acoustic coupling cancels out.
        # ``_uplink_shut_until_ns`` is the tail after a *normal* reply, during which the DAC is
        # still draining. The suppression counters are per playback episode and exist to be
        # logged: a margin so high the robot is deaf while speaking is otherwise indistinguishable
        # from one that works (§3.5.2 — loud drops are a tuning signal, silent ones are a
        # debugging catastrophe), and #106's AC-3 needs the number that log line carries.
        self._echo_floor = EchoFloor()
        self._barge_in_margin_db = barge_in_margin_db
        self._echo_tail_ns = echo_tail_ms * 1_000_000
        self._uplink_shut_until_ns = 0
        self._suppressed_frames = 0
        self._loudest_suppressed_dbfs = SILENCE_DBFS

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
        # playback's own correlation). ``_dropping`` makes the overflow warning one per episode
        # rather than one per frame — at 50 frames/s the honest signal would otherwise be noise.
        self._mic_out: asyncio.Queue[AudioChunk] = asyncio.Queue(
            maxsize=_MIC_QUEUE_FRAMES
        )
        self._dropping = False
        self._playing_item: str | None = None
        self._playing_ms = 0
        self._playing_corr: UUID | None = None
        # Bumped on every episode close (:meth:`_take_playback`). ``play`` captures it before
        # awaiting the speaker and re-checks after: a write that outlives its episode must not
        # write back into the next one (AVID-174). The async twin of ``AlsaSpeaker``'s ``_stopped``
        # flag — the barge-in never waits for the writer, the writer notices it lost.
        self._playback_epoch = 0

        # The owned mic-consume task (SDS §9.2): None until start(), cleared by stop().
        self._task: asyncio.Task[None] | None = None

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Launch the mic-consume loop as an owned task. Idempotent."""
        if self._task is None:
            self._task = spawn(self._run(), name="AudioService.mic_loop")

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
        """Yield each captured **frame** as :meth:`_emit` hands it over — a live, endless stream
        (a real mic never ends), unlike ``FakeTurnSink``'s finite script. ``ConversationService``
        drains this for the session's life and cancels it on close.

        Frames, not utterances (#153): the port always specified ``mic()`` as mirroring
        :meth:`~avid.core.ports.Microphone.stream`, and the fake always honoured it — this side
        was the odd one out until §6.3's "stream live" was actually implemented."""
        while True:
            yield await self._mic_out.get()

    async def play(self, chunk: AudioChunk, *, item_id: str) -> None:
        """Play one assistant PCM delta to the speaker (``TurnSink`` down, §9.1.4).

        The **first** delta of a response opens playback: publish ``audio.playback_started`` and
        drive ``THINKING → SPEAKING`` (the playback belongs to the turn whose ``correlation_id``
        this service minted at ``speech_started``, so it is stamped with that, captured now so a
        later barge-in keeps it). Subsequent deltas just accumulate ``played_ms`` and stream.

        ``played_ms`` accumulates **what** :meth:`~avid.core.ports.Speaker.play` **returned** —
        the ms the device accepted — never the length of the buffer we handed down. Those
        differ: ALSA returns ``-EPIPE`` after an underrun having played nothing, so a service
        that recomputed this from ``chunk.pcm`` would publish ``audio.playback_finished`` for
        audio the room never heard (AVID-91). A shortfall is logged against the turn.
        """
        epoch = self._playback_epoch
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
            if self._playback_epoch != epoch:
                return  # a barge-in ended the episode while we were announcing it
        else:
            corr = self._playback_corr()
        submitted_ms = pcm_duration_ms(
            chunk.pcm, sample_rate=chunk.sample_rate, channels=chunk.channels
        )
        accepted_ms = await self._speaker.play(chunk)
        if self._playback_epoch != epoch:
            # A barge-in took the episode while this write was in flight (AVID-174). Everything
            # below belongs to a playback that no longer exists: `_playing_ms` would leak these ms
            # into the *next* reply's total — and so into the `audio_end_ms` the model is told the
            # user heard — and the shortfall branch would dereference the cleared episode, which is
            # the AssertionError that killed the pump task on the bench.
            _log.debug(
                "barge-in truncated the write: %d of %d ms for item %s [correlation_id=%s]",
                accepted_ms,
                submitted_ms,
                item_id,
                corr,
            )
            return
        self._playing_ms += accepted_ms
        if accepted_ms < submitted_ms:
            # Still a live episode, so a shortfall here really is the device dropping audio
            # (AVID-91) rather than a barge-in cutting the write, and stays a WARNING. The one
            # thing this split gives up is spotting a genuine device drop that coincides exactly
            # with a barge-in; a permanent WARNING on every interruption is the worse trade.
            _log.warning(
                "speaker accepted %d of %d ms for item %s [correlation_id=%s]",
                accepted_ms,
                submitted_ms,
                item_id,
                corr,
            )

    async def end_response(self) -> None:
        """Normal end of the response's audio (``TurnSink``, §9.1.4).

        Publishes ``audio.playback_finished`` (``truncated=False``) with the ms actually emitted
        and drives ``SPEAKING → IDLE``. A no-op when nothing is playing (a turn with no audio).
        Distinct from :meth:`interrupt`, whose barge-in ends in LISTENING, not IDLE."""
        episode = self._take_playback()
        if episode is None:
            return
        # The tail is armed in the same synchronous breath as the close, so there is no window in
        # which the uplink is open over a still-draining DAC, and it can never be applied to a
        # *later* episode a concurrent play() opened meanwhile (AVID-174). A barge-in that took
        # the episode first returns above and still gets no tail — §6.2.4's rule, now true under
        # a race as well as without one.
        self._uplink_shut_until_ns = self._clock.monotonic_ns() + self._echo_tail_ns
        await self._publish_finished(episode, truncated=False)
        await self._state.transition(
            Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=episode.corr
        )
        # The reply is over as far as the model is concerned, but not as far as the room is: the
        # DAC is still clocking out up to a playback-buffer depth of it (§6.2.4). Hold the uplink
        # shut over that tail, or its last ~100 ms goes to the model as user audio (AVID-159).
        self._uplink_shut_until_ns = self._clock.monotonic_ns() + self._echo_tail_ns

    async def interrupt(self) -> int:
        """Barge-in: cut playback immediately and report the ms the speaker **accepted**
        (``TurnSink``, SDS §6.2.4). Idempotent — 0 when nothing is playing.

        Stops the speaker (on the port so this is instant), publishes the truncated
        ``audio.playback_finished`` fact, and returns ``played_ms`` — the ``audio_end_ms``
        the model-side truncate (#104) needs. *Accepted*, not *emitted*: the figure is summed
        from :meth:`~avid.core.ports.Speaker.play`'s returns, so it is exact about audio the
        device refused and still optimistic by up to one playback-buffer depth (~107 ms at
        24 kHz) about audio the DAC had not yet clocked out (SDS §6.2.4). Strictly better than
        the buffer length it replaced, and bounded. It does **not** drive a transition: the
        ``SPEAKING → LISTENING`` move is the ``speech_started`` origin's (see :meth:`_begin_speech`).
        """
        await self._speaker.stop()
        episode = self._take_playback()
        if episode is None:
            return 0
        await self._publish_finished(episode, truncated=True)
        return episode.played_ms

    async def _publish_finished(self, episode: _Episode, *, truncated: bool) -> None:
        """Publish ``audio.playback_finished`` from a **snapshot**, never from ``self``.

        Taking the episode by value is what makes this safe to await: the fields it reports were
        read inside :meth:`_take_playback`'s synchronous section, so nothing here can observe a
        half-cleared episode or race a concurrent close (AVID-174)."""
        await self._bus.publish(
            AudioPlaybackFinished(
                **envelope(
                    clock=self._clock,
                    correlation_id=episode.corr,
                    source=_SOURCE,
                ),
                item_id=episode.item_id,
                played_ms=episode.played_ms,
                truncated=truncated,
            )
        )

    def _playback_corr(self) -> UUID:
        """The correlation_id of the in-flight playback (the turn that opened it).

        Only ever called **synchronously at open**, where a missing ``_turn_id`` is a real bug
        (playback with no turn) rather than a race. Since AVID-174 nothing calls this after an
        await — the callers carry a captured ``corr`` or an :class:`_Episode` instead."""
        assert self._playing_corr is not None  # set when playback opened
        return self._playing_corr

    def _take_playback(self) -> _Episode | None:
        """Close the in-flight episode and hand its snapshot to the caller. ``None`` if none.

        **Synchronous by design, and that is the whole mechanism** (AVID-174). In single-threaded
        asyncio a run of code containing no ``await`` *is* a critical section — the cheapest and
        strongest lock available — so exactly one caller can ever take a given episode. That is
        what makes ``end_response`` and ``interrupt`` mutually exclusive finalizers: without it
        both could pass their ``_playing_item is not None`` guard and publish a second
        ``audio.playback_finished`` for the same item, which would have ConversationService send
        ``truncate`` + ``cancel`` for a response that ended normally, and would run the echo-gate
        report twice — silently halving the suppression counts #106's AC-3 is calibrated from.

        A lock was the obvious alternative and is the wrong one here: to help at all it would have
        to span ``await speaker.play()`` (a 100-200 ms delta write), adding a full write of
        barge-in latency against §6.2.4's "immediate" — and its contender would be the *mic loop*,
        so it would park frame consumption behind the speaker and re-create the starvation
        AVID-153/159 just fixed.
        """
        if self._playing_item is None:
            return None
        episode = _Episode(
            item_id=self._playing_item,
            played_ms=self._playing_ms,
            corr=self._playback_corr(),
        )
        self._report_echo_gate(episode.corr)
        self._playing_item = None
        self._playing_ms = 0
        self._playing_corr = None
        self._playback_epoch += 1
        return episode

    # --- the mic loop --------------------------------------------------------------------

    async def _run(self) -> None:
        """Consume the mic forever, judging and buffering every frame (AC-1).

        Runs until :meth:`stop` cancels it. ``is_speech`` is inline (sync, sub-ms — P8);
        the pre-roll is fed on every frame so a turn can replay the phonemes it missed.

        Two questions per frame, and they are different questions (AVID-159): the VAD answers
        *is this speech*, and — only when the robot is the one talking — the level answers
        *whose*. Silero already rejects transients (§6.3: 0 false opens in 3000 frames of knocks,
        claps and doors), so the margin never has to; it only separates two genuine voices.
        """
        async for chunk in self._mic.stream():
            speech = self._vad.is_speech(chunk)
            self._preroll.append(chunk.pcm)
            frame_ms = pcm_duration_ms(
                chunk.pcm, sample_rate=chunk.sample_rate, channels=chunk.channels
            )
            frame_dbfs = rms_dbfs(chunk.pcm)
            if speech:
                if not self._speaking:
                    if not self._admits_barge_in(frame_dbfs):
                        continue  # our own speaker, not the user — see _admits_barge_in
                    await self._begin_speech()  # rising edge — replays the pre-roll
                else:
                    # Mid-utterance barge-in (AVID-161). The reply started *while* they were
                    # already talking, so there is no rising edge left to carry them through
                    # the check above — and without this the escape hatch is unreachable in
                    # exactly the case that needs it: the robot talks over you, the uplink is
                    # shut, and your words stop reaching the model until you give up and start
                    # again. Judged per frame here, so the interrupt lands as soon as they are
                    # loud enough, and ``interrupt`` re-opens the uplink for the rest of it.
                    if self._uplink_shut() and self._admits_barge_in(frame_dbfs):
                        await self.interrupt()
                        self._uplink_shut_until_ns = 0  # the rest of the turn is theirs
                    self._capture(chunk.pcm)  # subsequent speech frame
                self._speech_ms += frame_ms
                self._silence_run_ms = 0
            elif self._speaking:
                # Trailing silence is still part of the captured clip; count it toward the
                # debounce and end the turn once it has lasted long enough (AC-2). It is
                # *captured* too, and in seam mode that means streamed: the server's own VAD
                # closes the turn on silence it hears, so withholding these frames would leave
                # the turn open forever (#153).
                self._capture(chunk.pcm)
                self._silence_run_ms += frame_ms
                if self._silence_run_ms >= self._silence_hold_ms:
                    await self._end_speech()
            else:
                # Idle silence — the pre-roll rolls and nothing is published, but this is the
                # room's own level and it is what the echo floor is for. Not observed during a
                # turn (the branches above): the user's voice would drag the floor up under them.
                self._echo_floor.observe(frame_dbfs)

    def _capture(self, pcm: bytes) -> None:
        """Route one captured frame — the **only** place the two modes diverge (#153).

        Loopback buffers it for the echo :meth:`_end_speech` plays; the seam streams it up the
        ``TurnSink`` now, per §6.3's "stream live". Synchronous by design: this sits in the mic
        loop's hot path, so it allocates and returns rather than awaiting (P8)."""
        if self._loopback_mode:
            self._utterance += pcm
        else:
            self._emit(pcm)

    def _uplink_shut(self) -> bool:
        """Is the robot's own voice reaching the mic right now (AVID-159, §6.3)?

        True while a response is playing, and for ``[gate] echo_tail_ms`` after one ends
        *normally* — ``end_response`` returns as soon as the last delta is handed over, but the
        DAC is still clocking out up to a playback-buffer depth of it (§6.2.4). A barge-in sets
        no tail: ``Speaker.stop`` closes the handle so ALSA drops the buffer, and the user is
        mid-utterance, so a tail there would clip the very words that interrupted.
        """
        if self._playing_item is not None:
            return True
        return self._clock.monotonic_ns() < self._uplink_shut_until_ns

    def _admits_barge_in(self, frame_dbfs: float) -> bool:
        """Is this rising edge the **user**, or our own speaker (AVID-159, SDS §6.2.4)?

        Only ever asked while :meth:`_uplink_shut` — so **normal turn-taking is never tested
        against the margin at all** and is bit-for-bit unaffected by this gate. That bound is
        deliberate: the discriminator is crude, and it should only run where nothing better
        exists.

        Asked at the rising edge, and — since AVID-161 — on every frame of an utterance the
        robot started talking over, because such a user has no rising edge left to be judged on.

        A rejected frame is fed to the floor precisely *because* it is the robot: while the
        assistant speaks, what the mic hears is the echo, so those frames **are** the
        calibration. A frame that clears the margin is judged to be the user and is deliberately
        **not** observed — folding it in would raise the bar under the speaker mid-sentence.
        """
        if not self._uplink_shut():
            return True  # the robot is silent; every edge is the user's
        if self._echo_floor.exceeds(frame_dbfs, margin_db=self._barge_in_margin_db):
            return True
        self._echo_floor.observe(frame_dbfs)
        self._suppressed_frames += 1
        self._loudest_suppressed_dbfs = max(self._loudest_suppressed_dbfs, frame_dbfs)
        return False

    def _report_echo_gate(self, corr: UUID) -> None:
        """One line per reply: the calibration datum #106's AC-3 requires (AVID-159).

        *corr* is passed in rather than read back off ``self`` so the call ordering inside
        :meth:`_take_playback` stops being load-bearing (AVID-174).

        Emitted on **every** playback episode, so every bench run is a calibration run and there
        is no separate mode anyone has to remember to enable. ``floor`` is what the mic heard
        while the robot spoke; ``loudest suppressed`` is the closest any rejected frame came to
        clearing the margin.

        Read together with a genuine barge-in's level, these say whether the two populations
        separate at all. **If they do not, no margin can be tuned into working** and the honest
        answers are #163 (echo cancellation) or full half-duplex — the point of printing it is so
        nobody spends a bench session turning a knob that was never going to help.
        """
        _log.info(
            "echo gate: floor %.1f dBFS, loudest suppressed frame %.1f dBFS "
            "(%d suppressed), margin %.1f dB [correlation_id=%s]",
            self._echo_floor.dbfs,
            self._loudest_suppressed_dbfs,
            self._suppressed_frames,
            self._barge_in_margin_db,
            corr,
        )
        self._suppressed_frames = 0
        self._loudest_suppressed_dbfs = SILENCE_DBFS

    def _emit(self, pcm: bytes) -> None:
        """Hand one captured frame up the ``TurnSink`` queue immediately (§6.3, §9.1.4).

        Non-blocking, always: the mic loop must never park on a consumer (P8), and the queue's
        consumer only exists while a Realtime session is open — between sessions, and for the
        ~200 ms an ``open()`` takes, nothing drains it. A full queue therefore means audio is
        going nowhere (a failed open, a degraded robot), and the policy is **drop-oldest**: the
        freshest audio is the audio worth keeping, and the bus's own rule applies — silent drops
        are a debugging catastrophe, loud drops are a tuning signal (§3.5.2). Warned once per
        overflow episode, not once per frame.

        **The uplink is half-duplex** (AVID-159, §6.3). Nothing crosses the seam while the robot
        is the one making noise: streaming the echo up made the model hear itself, and the
        server's turn detection then saw near-continuous audio and stopped committing turns
        altogether — the conversation died with the socket still open. Dropped here rather than
        at the capture, so the M4 loopback (which never goes through this path) is untouched."""
        if self._uplink_shut() or not pcm:
            return
        chunk = AudioChunk(
            pcm=pcm, sample_rate=self._sample_rate, channels=self._channels
        )
        try:
            self._mic_out.put_nowait(chunk)
        except asyncio.QueueFull:
            # Sole producer, so the get_nowait below always frees exactly the room we need.
            self._mic_out.get_nowait()
            self._mic_out.put_nowait(chunk)
            if not self._dropping:
                self._dropping = True
                _log.warning(
                    "mic-up queue full at %d frames — dropping oldest captured audio; "
                    "nothing is draining the TurnSink [correlation_id=%s]",
                    _MIC_QUEUE_FRAMES,
                    self._turn_id,
                )
        else:
            self._dropping = False

    async def _begin_speech(self) -> None:
        """Rising edge: a turn begins. Mint its id, replay the pre-roll, publish, transition.

        **A turn origin** (SDS §9.1.1): this is where a fresh ``correlation_id`` is minted;
        every downstream event of the turn propagates it. The drained pre-roll is the turn's
        first captured audio — it holds the leading phonemes the gate would otherwise miss, and
        §6.3 is explicit that it is *replayed* ahead of the live stream. It goes through
        :meth:`_capture` like any other frame, which seeds the loopback's buffer or opens the
        seam's stream depending on the mode. It already contains the frame that fired this edge
        (``_run`` appends before judging), so that frame is never emitted twice.

        Barge-in (AC-5): if assistant audio is in flight, cut playback **before** the
        transition, so ``→ LISTENING`` lands on a silent speaker — :meth:`interrupt` is instant
        and also emits the truncated ``audio.playback_finished`` fact for the interrupted
        response (which is what tells ``ConversationService`` to truncate the model, §6.2.4).
        """
        self._speaking = True
        self._turn_id = uuid4()
        pre = self._preroll.drain()  # includes this first speech frame (appended above)
        self._utterance = bytearray()
        self._speech_ms = 0
        self._silence_run_ms = 0
        ring_buffer_ms = len(pre) // self._bytes_per_ms

        # Gated on the speaker THIS service owns, not on the state machine's view of it
        # (AVID-158). ``_playing_item`` is the authoritative "assistant audio is in flight" fact;
        # ``RobotState`` is a derived view that lags it whenever the model's audio overlaps the
        # user's speech — measured in the bench trace, where a reply to an earlier commit began
        # 0.9 s before our falling edge fired. Gating on ``state is SPEAKING`` also silently
        # dropped the in-flight playback's ``audio.playback_finished``, breaking the four-fact
        # turn arc (§9.1.3) for exactly the responses a barge-in cut short.
        #
        # **Before the capture, not after** (AVID-159). This edge has already been judged to be
        # the user, so the uplink is theirs from here: cutting playback clears ``_playing_item``
        # and dropping the echo tail re-opens the seam, both of which must happen while there is
        # still a pre-roll to replay. The other order silently swallows the leading phonemes of
        # the very barge-in the margin just admitted — the exact loss §6.3's ring buffer exists
        # to prevent, reintroduced by its own gate.
        if self._playing_item is not None:
            await self.interrupt()  # stops the speaker, finalizes the old playback
        self._uplink_shut_until_ns = 0

        self._capture(pre)  # §6.3: replay the ring buffer, then stream live

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
        drive ``LISTENING → THINKING``, then echo the clip (loopback) or stop capturing (seam).

        ``duration_ms`` is the speech length — the sum of the *speech* frames, excluding the
        trailing silence hangover that triggered the end. In seam mode there is **nothing to hand
        over here** (#153): every frame, silence included, already went up the ``TurnSink`` as it
        was captured, so this only resets the **capture** state while ``_turn_id`` is kept alive —
        the assistant playback that follows is the same turn (SDS §3.12.2). In loopback mode the
        buffered clip is echoed and the whole turn (id included) is reset.

        The transition is a **direct awaited call**, like the three other edges this service
        drives: losing a state change is a correctness bug and the bus is at-most-once, so it
        cannot ride ``audio.speech_ended``'s own subscriber queue (SDS §9.1.4, AVID-158).
        """
        turn_id = self._turn_id
        assert turn_id is not None  # set on the rising edge that reached here
        await self._bus.publish(
            AudioSpeechEnded(
                **envelope(clock=self._clock, correlation_id=turn_id, source=_SOURCE),
                duration_ms=self._speech_ms,
            )
        )
        # Into THINKING: the turn ends when *our* gate says the user stopped, never when the
        # model's transcript arrives (AVID-158 — see the table in ``domain/state.py``).
        await self._state.transition(Trigger.AUDIO_SPEECH_ENDED, correlation_id=turn_id)
        if self._loopback_mode:
            await self._loopback(turn_id)
            self._reset_turn()
        else:
            self._reset_capture()

    async def _loopback(self, turn_id: UUID) -> None:
        """The M4 AI-client stand-in (AC-4): echo the captured utterance back to the speaker.

        Publishes ``audio.playback_started`` / ``audio.playback_finished`` around a single
        ``Speaker.play`` of the whole utterance, propagating *turn_id* so one grep on it
        reconstructs the turn including its echo. ``played_ms`` is **what the speaker
        returned** — the ms the device accepted — not the length of the PCM we submitted;
        the two differ exactly when audio is being dropped, which is the failure this gate
        exists to catch (AVID-91). ``truncated`` is always ``False`` at M4 (no barge-in
        truncation path yet).

        Deliberately does **not** drive the state machine: the ``SPEAKING → IDLE`` half of the
        arc belongs to a real conversation turn, and there is no ConversationService at M4 to
        supply one. Since AVID-158 the falling edge has already carried the machine to THINKING
        by the time this runs, so the robot sits in **THINKING** while the echo plays and the
        next turn's rising edge carries it back to LISTENING — the loopback is self-recovering
        across turns for the first time, where it used to dead-end. Barge-in against genuine
        playback is still exercised by a unit test rather than reached through the loopback. In
        M5 this whole method is replaced by the Realtime response stream.
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
        played_ms = await self._speaker.play(
            AudioChunk(pcm=pcm, sample_rate=self._sample_rate, channels=self._channels)
        )
        submitted_ms = pcm_duration_ms(
            pcm, sample_rate=self._sample_rate, channels=self._channels
        )
        if played_ms < submitted_ms:
            _log.warning(
                "loopback %s: speaker accepted %d of %d ms [correlation_id=%s]",
                item_id,
                played_ms,
                submitted_ms,
                turn_id,
            )
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
