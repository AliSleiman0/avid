"""TurnSink adapter — the fake that *is* the simulator for the audio seam (#100).

The :class:`~avid.core.ports.TurnSink` port is how a turn's PCM crosses
``ConversationService ↔ AudioService`` as a **direct call, never a bus event** (§9.1.4).
Its *real* implementation lands with the ``AudioService`` seam (#103), where the sink bridges
the live :class:`~avid.core.ports.Microphone` and :class:`~avid.core.ports.Speaker`; this
module ships the fake that stands in until then and forever after in the simulator (P6, SDS
§3.9.2).

:class:`FakeTurnSink` keeps the port's three promises with no hardware: :meth:`mic` replays a
**scripted** list of captured frames (the reproducible timeline ``ConversationService`` tests
drive turns through, exactly like ``FakeMicrophone``/``FakeVoiceActivityDetector``);
:meth:`play` records each assistant delta; :meth:`end_response` counts a normal completion; and
:meth:`interrupt` returns a **scriptable** ``played_ms`` — the honest ``audio_end_ms`` a barge-in
test asserts against (§6.2.4). Stdlib only.

The :attr:`played`, :attr:`interrupts`, :attr:`responses_ended` and :attr:`mic_sent` attributes
are advertised as plain attributes, deliberately **off** the ``TurnSink`` port (no application
reads them back, exactly as ``FakeSpeaker.played``/``stops`` are off the ``Speaker`` port) — they
are the contract suite's observation points. Constructed only by the composition root or a test fixture (P3);
everything else depends on the port (P2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from avid.core.hal import AudioChunk


class FakeTurnSink:
    """The :class:`~avid.core.ports.TurnSink` fake (P6): scripted mic, recorded playback.

    ``script`` is the mic timeline :meth:`mic` replays; ``played_ms`` is what :meth:`interrupt`
    reports the speaker emitted (a fixed, injected figure — the point is that a test *chooses*
    it, so the barge-in ``audio_end_ms`` path is exercised with a known value). The public
    :attr:`played` list, :attr:`stops` / :attr:`responses_ended` counters and :attr:`mic_sent`
    counter are the assertable, off-port trace.
    """

    def __init__(
        self,
        *,
        script: Sequence[AudioChunk] = (),
        played_ms: int = 0,
    ) -> None:
        self._script = tuple(script)
        self._played_ms = played_ms
        # Advertised, off the port (the contract's observation points, not an app need).
        self.played: list[tuple[str, AudioChunk]] = []
        self.interrupts = 0
        self.responses_ended = 0
        self.mic_sent = 0

    def mic(self) -> AsyncIterator[AudioChunk]:
        """Replay the scripted mic frames, one at a time (up). See :meth:`_mic`."""
        return self._mic()

    async def _mic(self) -> AsyncIterator[AudioChunk]:
        """Yield each scripted frame with a real loop-yield between them (P8), so a consumer's
        cancel/``break`` propagates cleanly. Ends when the script is exhausted — a fake turn is
        finite, unlike a live mic."""
        for chunk in self._script:
            await asyncio.sleep(0)
            self.mic_sent += 1
            yield chunk

    async def play(self, chunk: AudioChunk, *, item_id: str) -> None:
        """Record one assistant delta against its ``item_id`` (down). ``await``\\ s so the call
        is a genuine loop yield / cancellation point (P8), like ``FakeSpeaker.play``."""
        self.played.append((item_id, chunk))
        await asyncio.sleep(0)

    async def end_response(self) -> None:
        """Count one normal end-of-response (the real sink finalizes playback here). ``await``\\ s
        so it is a real loop yield (P8); ``responses_ended`` is the off-port trace."""
        self.responses_ended += 1
        await asyncio.sleep(0)

    async def interrupt(self) -> int:
        """Barge-in: count the interrupt and return the scripted ``played_ms`` (§6.2.4).
        Idempotent — safe to call with nothing playing; still reports the injected figure."""
        self.interrupts += 1
        return self._played_ms
