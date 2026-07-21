"""The degraded-mode WAV cue bank: a named cue onto the speaker, or a quiet log (AVID-80).

Real :class:`FakeSpeaker`, no mocks (``unittest.mock`` is banned outside ``tests/adapters/``,
SDS §14.3): the fake *is* the speaker, and it actually opens and slices the shipped WAVs, so
these tests double as a check that the committed bank exists and is correctly formatted.

The assertions cover the four ACs: the manifest is exhaustive (AC-2); every mapped file ships
at 24 kHz mono 16-bit (AC-1); a named cue resolves to its file and plays with no streaming and
no network (AC-3); and a missing directory or file degrades to a correlation-id log rather than
a crash (AC-4).
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path
from uuid import uuid4

import pytest

from avid.adapters.speaker import FakeSpeaker
from avid.domain import Cue
from avid.services.cue_bank import CUE_FILES, CueBank

# The committed bank, resolved relative to the repo (tests/services/ -> repo root).
_ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "cues"

# The playback format the whole bank must be in (SDS §6.2.4): 24 kHz mono 16-bit.
_SAMPLE_RATE = 24_000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2

_LOGGER = "avid.services.cue_bank"

# A short spoken cue for the one test that plays a clip end to end (FakeSpeaker paces through
# the file in real time, so a short one keeps the suite snappy).
_SHORT_CUE = Cue.ACKNOWLEDGE


def test_manifest_covers_every_cue() -> None:
    """AC-2: the typed manifest maps every ``Cue`` — no name can be played without a file."""
    assert set(CUE_FILES) == set(Cue)


def test_every_cue_file_ships_at_24k_mono_16bit() -> None:
    """AC-1: all ~20 files are committed and in the playback format.

    Opening each with stdlib ``wave`` proves both that it exists on disk and that it carries
    the right rate/channels/width — the format ``Speaker`` plays without resampling."""
    for cue, filename in CUE_FILES.items():
        path = _ASSET_DIR / filename
        assert path.is_file(), f"missing asset for {cue.name}: {path}"
        with wave.open(str(path), "rb") as handle:
            assert handle.getframerate() == _SAMPLE_RATE, cue.name
            assert handle.getnchannels() == _CHANNELS, cue.name
            assert handle.getsampwidth() == _SAMPLE_WIDTH_BYTES, cue.name
            assert handle.getnframes() > 0, cue.name


async def test_named_cue_resolves_and_plays_through_the_fake() -> None:
    """AC-3: a named cue resolves to its shipped file and plays via ``play_file`` — no streamed
    ``play`` chunk, no bus, no Realtime session (the bank holds none of those by construction)."""
    speaker = FakeSpeaker()
    bank = CueBank(speaker=speaker, asset_dir=_ASSET_DIR)

    await bank.play(_SHORT_CUE, correlation_id=uuid4())

    assert speaker.files_played == [_ASSET_DIR / CUE_FILES[_SHORT_CUE]]
    assert speaker.played == []  # degraded WAV path only — nothing streamed


async def test_missing_asset_dir_logs_with_correlation_id_and_does_not_play(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-4: no configured bank degrades to a log carrying the turn's id — never a crash."""
    speaker = FakeSpeaker()
    bank = CueBank(speaker=speaker, asset_dir=None)
    corr = uuid4()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await bank.play(Cue.ONE_MOMENT, correlation_id=corr)

    assert speaker.files_played == []
    assert "ONE_MOMENT" in caplog.text
    assert str(corr) in caplog.text


async def test_missing_file_is_swallowed_and_logged_with_correlation_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-4: a configured dir whose file is absent logs with the id and does not raise — the
    bank is best-effort perceived quality, not a correctness obligation."""
    speaker = FakeSpeaker()
    bank = CueBank(speaker=speaker, asset_dir=tmp_path)  # empty dir: no WAVs
    corr = uuid4()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await bank.play(Cue.CONNECTION_TROUBLE, correlation_id=corr)  # must not raise

    assert speaker.files_played == [tmp_path / CUE_FILES[Cue.CONNECTION_TROUBLE]]
    assert "CONNECTION_TROUBLE" in caplog.text
    assert str(corr) in caplog.text
