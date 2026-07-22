"""RealtimeClient adapters — the ``replay`` fake that *is* the simulator (#101).

The :class:`~avid.core.ports.RealtimeClient` port is the vendor blast radius (R-10,
CLAUDE.md §3): ``ConversationService`` (#102) depends only on it, and its two adapters
translate a Realtime session into the neutral :class:`~avid.core.realtime.RealtimeEvent`
stream. This module ships the **fake** — :class:`ReplayRealtimeClient`, which plays a
*recorded* session back deterministically (SDS §14.3): recorded once, replayed forever,
with **no key, no network, no cost**. It is what lets the whole M5 conversation arc — the
service (#102), the audio seam (#103), barge-in (#104) — be built and CI-gated entirely on
a laptop (SDS §14.5). The real ``openai`` WSS client lands with #105.

**Vendor-free by construction** (AC-6): this module imports only the stdlib and inward
(``core``/``domain``). It never imports ``openai`` — the recording is already in *our*
vocabulary, so replay needs nothing from the vendor.

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

import json
import wave
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from avid.core.hal import AudioChunk
from avid.core.ports import Clock
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    RealtimeEvent,
    SessionClosed,
    TurnDone,
    UserTranscript,
)
from avid.domain import TokenUsage

_MANIFEST = "session.json"
_SUPPORTED_FORMAT = 1


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

    async def open(self) -> None:
        """Open a fresh session — cold, no resume (SDS §6.2.3). Rewinds so a re-open replays
        from the top, clearing any prior :meth:`aclose`."""
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
