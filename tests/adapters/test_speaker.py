"""``AlsaSpeaker``'s underrun-recovery and format-tracking branches, off the Pi (AVID-91).

These are the paths the contract suite cannot reach. ``tests/contract/test_speaker.py`` asserts
what *every* speaker promises and runs its "real" leg only on the Pi; what it cannot do is make
a healthy device fail on demand. Yet the failure is the whole point: ALSA returns ``-EPIPE``
after an underrun having played nothing, and the adapter used to discard that, dropping every
other utterance in silence while the M4 gate printed ``PASS``.

So the device is stubbed. Per the house idiom (``test_realtime_capture.py``) the stub is
**hand-written, not a mock** — even here, where ``unittest.mock`` would be permitted (SDS §14.3):
a scripted ``_StubPcm`` records what it was asked to write and answers with the outcomes the real
library produces, and ``_StubSpeaker`` overrides the single ``_open_blocking`` seam. The one
thing a stub cannot fake off-Pi is ``alsaaudio.ALSAAudioError`` — which is why the adapter names
its recovery exception through an instance attribute the stub reassigns, rather than catching a
module-level import.

``pyalsaaudio``'s ``write()`` has four outcomes and they vary by build and mode: a full count, a
short count, a negative errno, a zero, or a raised error. All five are exercised below, because
the on-Pi verification can confirm which one *that* build produces but the adapter has to be
right either way.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import pytest

from avid.adapters.speaker import AlsaSpeaker
from avid.core.hal import SAMPLE_WIDTH_BYTES, AudioChunk

_RATE, _CHANNELS = 24000, 1
_PERIOD_FRAMES = _RATE // 50  # 480 frames = one ~20 ms period at the playback rate
_LOGGER = "avid.adapters.speaker"

# One scripted write outcome: None accepts every frame, an int is returned verbatim (a short
# count, 0, or a negative errno like -32/-EPIPE), an Exception is raised. A plain alias, not a
# PEP 695 `type` statement — the Pi runs system Python 3.11 and no 3.12+ syntax ships (ADR-008).
_Outcome = int | Exception | None


class _StubError(Exception):
    """Stands in for ``alsaaudio.ALSAAudioError``, which cannot be imported off the Pi."""


class _StubPcm:
    """A scripted ALSA playback handle: answers ``write`` from a shared outcome queue.

    The queue is shared with the :class:`_StubSpeaker` that opened it, so a script survives
    the close-and-reopen the recovery path performs — which is exactly what a test of "the
    retry succeeds" needs to express.
    """

    def __init__(
        self, script: list[_Outcome], *, channels: int, on_write: object
    ) -> None:
        self._script = script
        self._frame_bytes = channels * SAMPLE_WIDTH_BYTES
        self._on_write = on_write
        self.writes: list[bytes] = []
        self.closed = 0

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if callable(self._on_write):
            self._on_write()
        outcome: _Outcome = self._script.pop(0) if self._script else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return len(data) // self._frame_bytes
        return outcome

    def close(self) -> None:
        self.closed += 1


class _StubSpeaker(AlsaSpeaker):
    """``AlsaSpeaker`` with its one device seam (``_open_blocking``) replaced by a stub.

    Records every open as ``(rate, channels)`` — the assertion that proves the adapter honours
    a chunk's declared format instead of the configured nominal one.
    """

    def __init__(
        self,
        *,
        script: list[_Outcome] | None = None,
        sample_rate: int = _RATE,
        channels: int = _CHANNELS,
        on_write: object = None,
    ) -> None:
        super().__init__(device="stub", sample_rate=sample_rate, channels=channels)
        self._alsa_error = _StubError  # the seam: no monkeypatching, no mock
        self._script: list[_Outcome] = list(script or [])
        self._on_write = on_write
        self.opened: list[tuple[int, int]] = []
        self.handles: list[_StubPcm] = []

    def _open_blocking(self, rate: int, channels: int) -> _StubPcm:
        self.opened.append((rate, channels))
        handle = _StubPcm(self._script, channels=channels, on_write=self._on_write)
        self.handles.append(handle)
        return handle


def _chunk(*, ms: int = 20, rate: int = _RATE, channels: int = _CHANNELS) -> AudioChunk:
    """One ``ms``-long S16_LE chunk at the given format (20 ms = exactly one period)."""
    length = rate * channels * SAMPLE_WIDTH_BYTES * ms // 1000
    return AudioChunk(pcm=b"\x11" * length, sample_rate=rate, channels=channels)


def _write_wav(path: Path, *, rate: int, channels: int, ms: int) -> Path:
    frames = rate * channels * SAMPLE_WIDTH_BYTES * ms // 1000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * frames)
    return path


# --- underrun recovery: the defect-1 branch ---------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(-32, id="negative_errno"),  # -EPIPE, what the Pi actually measured
        pytest.param(0, id="zero_written"),
        pytest.param(_StubError("underrun"), id="raised_error"),
    ],
)
async def test_an_underrun_is_recovered_by_reopening_and_retrying_once(
    failure: _Outcome,
) -> None:
    """All three shapes of "nothing was played" mean the same thing and recover the same way.

    The device is closed and reopened — ``pyalsaaudio`` exposes no ``prepare()``, and the Pi
    evidence is that a fresh handle never drops — and the period is written again, so the
    caller sees the full 20 ms rather than silence."""
    speaker = _StubSpeaker(script=[failure])

    assert await speaker.play(_chunk(ms=20)) == 20
    assert speaker.opened == [(_RATE, _CHANNELS), (_RATE, _CHANNELS)]
    assert speaker.handles[0].closed == 1
    assert len(speaker.handles[1].writes) == 1


async def test_a_period_that_fails_twice_is_counted_as_lost_never_raised() -> None:
    """A dropped 20 ms must not kill a turn: the loss is logged and reported, not raised.

    The write returns short — that is the whole contract. A caller holding the correlation id
    turns it into a warning against the turn; the adapter refuses to make it an exception."""
    speaker = _StubSpeaker(script=[-32, -32])

    # No pytest.raises: the assertion IS that this returns rather than raising.
    played = await speaker.play(_chunk(ms=20))

    assert played == 0
    assert speaker.opened == [(_RATE, _CHANNELS), (_RATE, _CHANNELS)]


async def test_only_the_failed_period_is_lost_not_the_whole_utterance() -> None:
    """Period-slicing earns its keep: one bad period costs 20 ms, not the utterance.

    Three periods, the middle one unrecoverable. A single giant write could only have
    reported all-or-nothing."""
    speaker = _StubSpeaker(script=[None, -32, -32, None])

    assert await speaker.play(_chunk(ms=60)) == 40


async def test_a_short_write_counts_only_the_frames_that_landed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partial write is neither success nor underrun: report what landed, and say so."""
    speaker = _StubSpeaker(script=[_PERIOD_FRAMES // 2])

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await speaker.play(_chunk(ms=20)) == 10
    assert "took 240 of 480 frames" in caplog.text


async def test_a_healthy_write_neither_reopens_nor_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The recovery path must stay off the happy path — one open, one write, no warning."""
    speaker = _StubSpeaker()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await speaker.play(_chunk(ms=40)) == 40
    assert speaker.opened == [(_RATE, _CHANNELS)]
    assert len(speaker.handles[0].writes) == 2  # 40 ms = two ~20 ms periods
    assert caplog.text == ""


# --- format tracking: the defect-2 branch ------------------------------------------------


async def test_a_chunks_rate_is_honoured_and_reopens_the_device() -> None:
    """The chunk declares 16 kHz, so the device opens at 16 kHz — not the nominal 24 kHz.

    This is defect 2 in one assertion: the old adapter opened at the configured rate and
    played whatever it was handed, so a 16 kHz echo came back 1.5x fast and a fifth high."""
    speaker = _StubSpeaker(sample_rate=_RATE)

    assert await speaker.play(_chunk(ms=20, rate=24000)) == 20
    assert await speaker.play(_chunk(ms=20, rate=16000)) == 20

    assert speaker.opened == [(24000, 1), (16000, 1)]
    assert speaker.handles[0].closed == 1  # the 24 kHz handle was closed, not reused


async def test_an_unchanged_format_reuses_the_handle() -> None:
    """Reopening is for format changes only — streaming deltas must not thrash the device."""
    speaker = _StubSpeaker()

    for _ in range(3):
        await speaker.play(_chunk(ms=20))

    assert speaker.opened == [(_RATE, _CHANNELS)]


async def test_a_deviation_from_the_nominal_format_is_logged_exactly_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one-line grep that would have caught defect 2 in a log instead of in a room.

    Once per adapter lifetime: the M4 loopback deviates on *every* turn, so logging each
    time would bury the signal it exists to raise."""
    speaker = _StubSpeaker(sample_rate=24000)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for rate in (16000, 24000, 16000):
            await speaker.play(_chunk(ms=20, rate=rate))

    notes = [r for r in caplog.records if "nominal format" in r.message]
    assert len(notes) == 1
    assert notes[0].levelno == logging.INFO


async def test_play_file_opens_at_the_files_own_rate(tmp_path: Path) -> None:
    """A canned cue is played at the rate it was authored at, through the shared handle."""
    speaker = _StubSpeaker(sample_rate=24000)
    clip = _write_wav(tmp_path / "cue.wav", rate=16000, channels=1, ms=40)

    assert await speaker.play_file(clip) == 40
    assert speaker.opened == [(16000, 1)]


# --- barge-in: the interrupt flag, the lock, and the stale close --------------------------


async def test_stop_between_periods_truncates_and_reports_only_what_landed() -> None:
    """Barge-in cuts at a period boundary, and the return says how much really played.

    The flag is set from inside the first write — standing in for a ``stop()`` landing while
    the worker thread is mid-utterance — so the loop exits before period two."""
    speaker: _StubSpeaker

    def interrupt() -> None:
        speaker._stopped.set()

    speaker = _StubSpeaker(on_write=interrupt)

    assert await speaker.play(_chunk(ms=100)) == 20  # one period of five


async def test_stop_closes_the_handle_and_a_later_play_reopens() -> None:
    """``stop`` drops the device's buffered audio; the next utterance gets a fresh handle."""
    speaker = _StubSpeaker()
    await speaker.play(_chunk(ms=20))

    await speaker.stop()

    assert speaker.handles[0].closed == 1
    await speaker.play(_chunk(ms=20))
    assert speaker.opened == [(_RATE, _CHANNELS), (_RATE, _CHANNELS)]


async def test_a_stale_close_does_not_kill_the_utterance_that_replaced_it() -> None:
    """``stop`` signals synchronously but closes on a thread hop — so the close can land
    *after* the next utterance has already started, and must not take that one down.

    Asserted behaviourally: if the stale close had killed the handle, the third play would
    have had to reopen. It does not."""
    speaker = _StubSpeaker()
    await speaker.play(_chunk(ms=20))
    speaker._stopped.set()  # a stop() has signalled but not yet reached its thread
    await speaker.play(_chunk(ms=20))  # the replacement utterance clears the flag

    speaker._close_blocking()  # the stop()'s close, arriving late

    await speaker.play(_chunk(ms=20))
    assert speaker.opened == [(_RATE, _CHANNELS)]
    assert speaker.handles[0].closed == 0
