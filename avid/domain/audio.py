"""The ``audio.*`` events and the pre-roll ring buffer — pure audio-domain primitives (#86).

Two kinds of thing, both stdlib-only and used first by ``AudioService`` (#87):

* **Four events** (SDS §9.1.3, SDS:1975–1982) — the facts the audio loop publishes:
  speech began/ended and playback began/finished. Each is a frozen/slotted/kw-only
  :class:`~avid.domain.events.Event` subclass carrying a validated
  ``<domain>.<past_tense_verb>`` name (P4), exactly like ``affect.changed`` next door.
* **A 300 ms pre-roll ring buffer** (:class:`AudioPreRoll`, SDS §6.3, SDS:1082) — keeps
  the last ~300 ms of captured PCM so that when local VAD fires, the leading phonemes
  it has already missed can be replayed into the freshly-opened session.

Pure by construction (P1): no I/O, no clock, no async, stdlib only. In particular the
ring buffer speaks in raw ``bytes`` framed by an injected ``bytes_per_ms`` and never
names :class:`~avid.core.hal.AudioChunk` — that is a ``core`` type the domain may not
import. The caller (which knows the capture format: 24 kHz mono 16-bit ⇒ 48 bytes/ms)
converts milliseconds to bytes; ALSA specifics stay out of the domain.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import ClassVar

from avid.domain.events import Event


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
