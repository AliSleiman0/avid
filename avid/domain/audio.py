"""The ``audio.*`` events, the pre-roll ring buffer, and the echo gate's arithmetic —
pure audio-domain primitives (#86).

Three kinds of thing, all stdlib-only and used first by ``AudioService`` (#87):

* **Four events** (SDS §9.1.3, SDS:1975–1982) — the facts the audio loop publishes:
  speech began/ended and playback began/finished. Each is a frozen/slotted/kw-only
  :class:`~avid.domain.events.Event` subclass carrying a validated
  ``<domain>.<past_tense_verb>`` name (P4), exactly like ``affect.changed`` next door.
* **A 300 ms pre-roll ring buffer** (:class:`AudioPreRoll`, SDS §6.3, SDS:1082) — keeps
  the last ~300 ms of captured PCM so that when local VAD fires, the leading phonemes
  it has already missed can be replayed into the freshly-opened session.
* **A level meter and an adaptive floor** (:func:`rms_dbfs`, :class:`EchoFloor`, SDS §6.2.4,
  AVID-159) — how the service tells *the user interrupting* from *its own speaker*, which
  a VAD provably cannot do: the robot's voice is speech too.

Pure by construction (P1): no I/O, no clock, no async, stdlib only. In particular the
ring buffer speaks in raw ``bytes`` framed by an injected ``bytes_per_ms`` and never
names :class:`~avid.core.hal.AudioChunk` — that is a ``core`` type the domain may not
import. The caller (which knows the capture format: 24 kHz mono 16-bit ⇒ 48 bytes/ms)
converts milliseconds to bytes; ALSA specifics stay out of the domain.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass
from typing import ClassVar

from avid.domain.events import Event

# Peak magnitude of a signed 16-bit sample: the 0 dBFS reference. A full-scale *square* wave
# therefore reads 0 dBFS and a full-scale sine −3.01 dBFS, which is the usual convention.
_FULL_SCALE = 32768.0

# The floor returned for digital silence, and the clamp on :func:`rms_dbfs`. Finite rather than
# ``-inf`` so every caller can do ordinary arithmetic on the result without special-casing.
SILENCE_DBFS = -120.0

# How fast :class:`EchoFloor` tracks the room, as an EMA weight per observed frame. At the
# shipped 20 ms ``[microphone] chunk_ms`` this reaches ~95% of a step in ~0.4 s — comfortably
# quicker than the 269 ms the bench measured between playback starting and the echo reaching the
# mic, and slow enough that no single loud frame can drag the floor up over the user.
_FLOOR_ATTACK = 0.15


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioSpeechStarted(Event):
    """Local VAD detected the start of user speech (SDS §9.1.3, SDS:1975).

    **A turn origin.** This is one of exactly two events that *mint* a
    ``correlation_id`` — the other is ``behavior.trigger_fired`` (SDS:1980). The mint
    itself (a fresh ``uuid4()``) is the publisher's act: ``AudioService`` stamps it via
    ``core.envelope.envelope(correlation_id=uuid4(), ...)`` (#87). Every downstream event
    of the turn then *propagates* that id, never re-mints it — one grep on it
    reconstructs the whole turn (SDS §3.12.2). The domain only holds the field; it does
    not mint (there is nothing to mint from here, and no clock).

    ``ring_buffer_ms`` reports how much pre-roll audio (:class:`AudioPreRoll`) was
    replayed into the opening session. Queue policy on the bus is DROP_OLDEST (#81).
    """

    name: ClassVar[str] = "audio.speech_started"

    ring_buffer_ms: int  # pre-roll replayed into the session when speech started


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioSpeechEnded(Event):
    """Local VAD detected the end of user speech (SDS §9.1.3, SDS:1976).

    Downstream of a turn origin, so ``correlation_id`` is *propagated* from the
    ``audio.speech_started`` that opened the turn, never re-minted. ``duration_ms`` is
    how long the speech lasted. Queue policy DROP_OLDEST.
    """

    name: ClassVar[str] = "audio.speech_ended"

    duration_ms: int  # length of the detected speech segment


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioPlaybackStarted(Event):
    """The speaker began emitting a synthesized response item (SDS §9.1.3, SDS:1977).

    ``item_id`` identifies the Realtime response item being played, so a later
    ``audio.playback_finished`` — and M5 barge-in truncation — can refer to the same
    item. Queue policy DROP_OLDEST.
    """

    name: ClassVar[str] = "audio.playback_started"

    item_id: str  # the response item now playing


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioPlaybackFinished(Event):
    """The speaker finished (or was cut off) emitting a response item (SDS §9.1.3, SDS:1978).

    ``played_ms`` is defined as **what the speaker actually emitted, not what was
    received** (SDS:1982) — the two differ by the whole playback-buffer depth, and it is
    the measurement M5 barge-in is built on: getting it wrong makes the model believe it
    said things the user never heard. ``truncated`` says whether playback was cut short
    by barge-in; at M4 there is no truncation path yet, so it is always ``False``.
    Queue policy DROP_OLDEST.
    """

    name: ClassVar[str] = "audio.playback_finished"

    item_id: str  # the response item that finished
    played_ms: int  # milliseconds the speaker actually emitted (not what was received)
    truncated: bool  # cut short by barge-in? always False at M4 (no truncation yet)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioCaptureStalled(Event):
    """The microphone stopped yielding frames (SDS §9.1.3, #347).

    A fact about the *instrument*, and the system had no way to state it before. ``AlsaMicrophone``
    carries no counters, and if ``stream()`` simply stops producing — device unplugged, ALSA card
    renumbered, a short-read loop that never recovers — the ``async for`` in ``AudioService`` parks
    forever and **nothing anywhere notices**. A robot that has gone deaf looks exactly like a room
    that has gone quiet.

    That distinction is not cosmetic. §10.5 counts an unanswered proactive turn as an *ignore*, and
    three ignores disable the trigger — so without this event a deaf robot silently switches its own
    proactivity off and records it as the user rejecting the feature, which is the precise
    conclusion R-08 exists to measure. ``BehaviorService`` subscribes for exactly that reason.

    Edge-triggered: published once when capture goes quiet, not repeated while it stays quiet.
    ``silent_ms`` is measured on the **monotonic** clock, like every other duration in this domain.
    Queue policy DROP_OLDEST.
    """

    name: ClassVar[str] = "audio.capture_stalled"

    silent_ms: int  # how long the mic had been silent when the watchdog gave up on it


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioCaptureResumed(Event):
    """Frames are arriving again after an :class:`AudioCaptureStalled` (SDS §9.1.3, #347).

    The closing bracket, and the reason a subscriber can hold a simple latch rather than a timer.

    ⚠️ Note which way the failure leans if one of these two is ever dropped — the bus is
    at-most-once (§3.5). Losing a *resume* leaves a listener believing capture is still down, which
    under-counts ignores: proactivity stays on when it might have been switched off. Losing a
    *stall* counts an ignore that should not have counted, which is exactly today's behaviour and so
    no regression. Both errors are survivable and the more likely one is the harmless one; that is
    the property that makes an at-most-once event acceptable here at all.

    Queue policy DROP_OLDEST.
    """

    name: ClassVar[str] = "audio.capture_resumed"

    stalled_ms: int  # how long the gap lasted, for the log that has to explain it


class AudioPreRoll:
    """The 300 ms pre-roll ring buffer (SDS §6.3, SDS:1082): keep the last ~``capacity_ms``
    of captured PCM so a VAD-gated session can replay the phonemes it already missed.

    Without it every utterance loses its first word: the session opens ~200 ms *after*
    speech starts, so the gate is unusable unless the pre-speech audio is replayed first.

    **Format-agnostic and pure.** It counts bytes against an injected ``bytes_per_ms``
    (24 kHz mono 16-bit ⇒ 48) and never names :class:`~avid.core.hal.AudioChunk` — a
    ``core`` type the domain may not import (P1). No clock, no I/O; the caller converts
    milliseconds to bytes because only the caller knows the capture format.

    Eviction is by whole appended frames, oldest first, whenever the buffer would exceed
    capacity — except that the most recent frame is always kept, so a single frame larger
    than ``capacity_ms`` is retained rather than dropped to nothing.
    """

    def __init__(self, *, capacity_ms: int, bytes_per_ms: int) -> None:
        self._capacity_bytes = capacity_ms * bytes_per_ms
        self._bytes_per_ms = bytes_per_ms
        self._frames: deque[bytes] = deque()
        self._buffered_bytes = 0

    def append(self, pcm: bytes) -> None:
        """Add a captured frame, evicting the oldest frames until back within capacity.

        The newest frame is never evicted — a frame on its own larger than the whole
        capacity is kept, so the buffer is never emptied by a single oversized append.
        """
        self._frames.append(pcm)
        self._buffered_bytes += len(pcm)
        while self._buffered_bytes > self._capacity_bytes and len(self._frames) > 1:
            self._buffered_bytes -= len(self._frames.popleft())

    def drain(self) -> bytes:
        """Return all buffered audio oldest-first as one blob, then clear the buffer.

        This is the replay step: on ``audio.speech_started`` the pre-roll is drained into
        the opening session ahead of the live stream, and emptied so the next silence
        starts fresh.
        """
        blob = b"".join(self._frames)
        self._frames.clear()
        self._buffered_bytes = 0
        return blob

    @property
    def buffered_ms(self) -> int:
        """How much audio is currently held, in milliseconds (floored)."""
        return self._buffered_bytes // self._bytes_per_ms


def rms_dbfs(pcm: bytes) -> float:
    """RMS level of S16_LE *pcm*, in dBFS. Digital silence returns :data:`SILENCE_DBFS`.

    The measurement the echo gate runs on (AVID-159). Loudness is the **only** discriminator
    available between the user and the robot's own speaker: both are speech, and
    ``VoiceActivityDetector.is_speech`` answers a deliberate yes/no with no probability crossing
    the port (SDS §9.3), so the classifier cannot help and the level has to.

    Odd trailing bytes are ignored rather than raising — a half sample is not a level, and a
    truncated frame is a device's business, not a reason to kill the mic loop. Sample order is
    native-endian, the same assumption ``adapters/realtime._resample`` already makes; every
    target here is little-endian, and an energy measure does not justify byte-swap machinery.
    """
    samples = array("h")
    usable = len(pcm) - (len(pcm) % samples.itemsize)
    samples.frombytes(pcm[:usable])
    if not samples:
        return SILENCE_DBFS
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(math.sqrt(mean_square) / _FULL_SCALE))


def _clamp_i16(value: float) -> int:
    """Round to the nearest signed 16-bit sample, saturating rather than wrapping.

    A high-pass overshoots on a step, so a filtered sample can land outside the range its input
    came from. Saturating is what an audio path should do with that; wrapping would turn a loud
    transient into a full-scale sign flip, and letting ``array("h")`` raise would kill the mic
    loop over one sample."""
    return int(max(-_FULL_SCALE, min(_FULL_SCALE - 1.0, round(value))))


class HighPass:
    """A cascaded one-pole high-pass filter over S16_LE PCM (AVID-283).

    **Why this exists.** Level was measured broadband, and broadband energy is not the same
    question as *is this the user*. Measured on the rig in an empty, silent room: the mic read
    **−18.4 dBFS**, of which 46% sat below 100 Hz with a dominant 50 Hz component — mains hum.
    In the band speech actually occupies (300–3400 Hz) the same room read **−49.1 dBFS**. So the
    gate was handed a floor 25–30 dB louder than the thing it was trying to detect, and a robot
    that stops hearing you because a charger is plugged in nearby is not shippable.

    **Why stdlib arithmetic and not scipy.** This is ``domain/`` — P1 forbids third-party imports
    beyond pydantic, enforced by both ``.importlinter`` and the purity test. That is a feature
    here rather than a constraint to work around: the filter stays a pure, unit-testable value
    with no I/O and no clock, exactly like :func:`rms_dbfs` beside it.

    **Why cascaded, and why the order is not decoration.** A single pole rolls off at only
    6 dB/octave, which at a 150 Hz cutoff buys about 10 dB at 50 Hz — well short of the ~25 dB
    the measurement says is needed. Each additional section adds another 6 dB/octave at the
    hum while costing well under a dB across the speech band, so the order is what converts this
    from a gesture into a fix. Choose it against a recording, not by taste.

    ``sample_rate`` is injected rather than assumed, following :class:`AudioPreRoll`'s
    ``bytes_per_ms``: nothing in ``domain/`` may import :class:`~avid.core.hal.AudioChunk`, so a
    rate-aware primitive here has to be told the rate.

    State (one ``(x[n-1], y[n-1])`` pair per section) persists **across frames**, which is the
    whole reason this is a class and not a function. A stateless per-frame filter would restart
    every 20 ms and let a fresh step through on each one — reintroducing at the frame rate the
    low-frequency energy it exists to remove.
    """

    def __init__(self, *, cutoff_hz: float, sample_rate: int, order: int = 1) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        if not 0.0 < cutoff_hz < sample_rate / 2.0:
            raise ValueError(
                f"cutoff_hz must be between 0 and the {sample_rate / 2.0:g} Hz Nyquist "
                f"frequency, got {cutoff_hz}"
            )
        if order < 1:
            raise ValueError(f"order must be at least 1, got {order}")
        # One-pole RC high-pass: y[n] = a * (y[n-1] + x[n] - x[n-1]), with a = RC/(RC + dt),
        # RC = 1/(2*pi*fc) and dt = 1/fs. Written as the closed form so no division by dt is
        # needed and the coefficient is computed once, not per sample.
        self._alpha = 1.0 / (1.0 + 2.0 * math.pi * cutoff_hz / sample_rate)
        self._cutoff_hz = cutoff_hz
        self._sample_rate = sample_rate
        self._order = order
        self._state: list[tuple[float, float]] = [(0.0, 0.0)] * order

    @property
    def cutoff_hz(self) -> float:
        """The −3 dB corner of a single section, in Hz."""
        return self._cutoff_hz

    @property
    def order(self) -> int:
        """How many one-pole sections are cascaded."""
        return self._order

    def apply(self, pcm: bytes) -> bytes:
        """Filter one frame of S16_LE *pcm*, returning S16_LE *pcm*.

        Bytes in, bytes out, so this composes directly with :func:`rms_dbfs` and with anything
        else that speaks the microphone's own format. An odd trailing byte is dropped, for the
        same reason :func:`rms_dbfs` ignores one: half a sample is not a sample.
        """
        samples = array("h")
        usable = len(pcm) - (len(pcm) % samples.itemsize)
        samples.frombytes(pcm[:usable])
        if not samples:
            return b""

        signal = [float(sample) for sample in samples]
        alpha = self._alpha
        for section, (x_prev, y_prev) in enumerate(self._state):
            for index, x in enumerate(signal):
                y_prev = alpha * (y_prev + x - x_prev)
                x_prev = x
                signal[index] = y_prev
            self._state[section] = (x_prev, y_prev)

        return array("h", [_clamp_i16(value) for value in signal]).tobytes()


class EchoFloor:
    """A running estimate of how loud the microphone hears the room (SDS §6.2.4, AVID-159).

    While the robot is speaking, what the mic hears **is** the echo — so tracking that level
    *is* the acoustic-coupling calibration, and speaker volume, mic gain, room and rig geometry
    all cancel out of the comparison. That is the whole reason this is adaptive rather than a
    configured ``playback_level x coupling`` constant: the constant would have to be re-measured
    every time the desk changed, and silently produce a deaf or a self-interrupting robot when
    nobody remembered to.

    It is deliberately **not** told whether playback is live. The caller feeds it every frame, so
    it self-seeds on ambient room noise long before the first reply, and decays back to ambience
    when the robot stops. The caller's only obligation is the one rule this class cannot enforce:
    **do not feed it frames you have already judged to be the user** (see :meth:`exceeds`), or
    their voice drags the floor up underneath them and the rest of the utterance is gated out.
    """

    def __init__(self, *, attack: float = _FLOOR_ATTACK) -> None:
        self._attack = attack
        self._dbfs = SILENCE_DBFS
        self._seeded = False

    @property
    def dbfs(self) -> float:
        """The current floor estimate, in dBFS (:data:`SILENCE_DBFS` before the first frame)."""
        return self._dbfs

    def observe(self, frame_dbfs: float) -> None:
        """Fold one frame's level into the estimate.

        The first frame is adopted outright rather than blended: starting from
        :data:`SILENCE_DBFS` and easing toward a real room would spend a few hundred
        milliseconds reporting a floor far below anything actually present.
        """
        if not self._seeded:
            self._dbfs = frame_dbfs
            self._seeded = True
            return
        self._dbfs += self._attack * (frame_dbfs - self._dbfs)

    def exceeds(self, frame_dbfs: float, *, margin_db: float) -> bool:
        """Is *frame_dbfs* at least *margin_db* above the floor — i.e. louder than the echo?

        A ``True`` here is the caller's cue to treat the frame as the user and to stop feeding it
        to :meth:`observe`. A very large *margin_db* makes this permanently ``False``, which is
        exactly full half-duplex: the documented fallback if the levels turn out not to separate
        on real hardware, reachable by config alone (SDS §6.3).
        """
        return frame_dbfs >= self._dbfs + margin_db
