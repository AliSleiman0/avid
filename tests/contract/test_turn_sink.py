"""Contract suite for the ``TurnSink`` port (#100, SDS §9.1.4, §6.2.4, §14.4).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). Unlike the device ports (camera/servo/mic/speaker/vad), ``TurnSink``'s
real adapter is **not** Pi hardware — it is the software seam ``AudioService`` grows in #103,
bridging the live :class:`~avid.core.ports.Microphone` and :class:`~avid.core.ports.Speaker`.
So the ``"real"`` case here skips with a *not-yet-built* reason pointing to #103 (the
placeholder-skip pattern AVID-50 laid for the hardware ports), not :func:`skip_off_pi`; #103
flips it to construct the real sink, exactly as #85 turned the VAD placeholder into
``SileroVad``.

The port promises three things — :meth:`~avid.core.ports.TurnSink.mic` (captured PCM up),
:meth:`play` (assistant PCM down), and :meth:`stop` (**barge-in**, returning the ``played_ms``
the speaker actually emitted — §6.2.4). The shared tests touch only those; the
``FakeTurnSink``-specific tail asserts the off-port trace (``played``/``stops``/``mic_sent``)
and the scripted mic timeline / scripted ``played_ms``.
"""

from __future__ import annotations

import asyncio

import pytest

from avid.adapters.turn_sink import FakeTurnSink
from avid.core.hal import AudioChunk
from avid.core.ports import TurnSink

# The two rigs the seam bridges (config/*.toml): mic capture 16 kHz, assistant playback 24 kHz.
_MIC_RATE, _OUT_RATE, _CHANNELS = 16000, 24000, 1

# fake runs everywhere; the real sink is the #103 AudioService seam, skipped until it exists.
_FAKE_REAL_PARAMS = [
    "fake",
    pytest.param(
        "real", marks=pytest.mark.skip(reason="real TurnSink lands with #103")
    ),
]


def _chunk(*, rate: int, ms: int = 20, fill: int = 0x11) -> AudioChunk:
    """One ``ms``-long S16_LE mono chunk of constant-byte PCM at ``rate``."""
    length = rate * _CHANNELS * 2 * ms // 1000
    return AudioChunk(pcm=bytes([fill]) * length, sample_rate=rate, channels=_CHANNELS)


_SCRIPT = [_chunk(rate=_MIC_RATE, fill=i) for i in range(3)]


# --- shared contract: every TurnSink adapter must satisfy it ------------------


@pytest.fixture(params=_FAKE_REAL_PARAMS)
def sink(request: pytest.FixtureRequest) -> TurnSink:
    """Every TurnSink adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case is skipped until the AudioService seam (#103) provides the real sink;
    this suite is the contract it will have to pass."""
    return FakeTurnSink(script=_SCRIPT, played_ms=320)


def test_adapter_satisfies_the_turn_sink_port(sink: TurnSink) -> None:
    assert isinstance(sink, TurnSink)


async def test_mic_yields_audio_chunks(sink: TurnSink) -> None:
    """§9.1.4: the captured-PCM-up direction — a stream of chunks, mirroring Microphone."""
    chunks = [chunk async for chunk in sink.mic()]
    assert chunks
    assert all(isinstance(chunk, AudioChunk) for chunk in chunks)


async def test_play_accepts_a_chunk_and_item_id(sink: TurnSink) -> None:
    """§9.1.4: assistant PCM down returns promptly — a direct call, no loop block (P8)."""
    await asyncio.wait_for(
        sink.play(_chunk(rate=_OUT_RATE), item_id="item_1"), timeout=1.0
    )


async def test_stop_returns_played_ms_and_is_idempotent(sink: TurnSink) -> None:
    """§6.2.4: barge-in returns the ms actually emitted (an int), and is safe to call twice."""
    first = await asyncio.wait_for(sink.stop(), timeout=1.0)
    second = await asyncio.wait_for(sink.stop(), timeout=1.0)
    assert isinstance(first, int)
    assert isinstance(second, int)


# --- FakeTurnSink-specific: the scripted timeline + recorded trace (SDS §3.9.2) ---


async def test_mic_replays_the_script_in_order() -> None:
    """The scripted frames come back one at a time, in order — the reproducible mic timeline
    the ConversationService tests (#102) drive turns through."""
    fake = FakeTurnSink(script=_SCRIPT)
    replayed = [chunk async for chunk in fake.mic()]
    assert replayed == _SCRIPT
    assert fake.mic_sent == len(_SCRIPT)


async def test_empty_script_yields_nothing() -> None:
    """No script → an immediately-exhausted mic stream (a fake turn is finite)."""
    fake = FakeTurnSink()
    assert [chunk async for chunk in fake.mic()] == []


async def test_play_records_item_id_and_chunk() -> None:
    """`played` is the off-port trace: what was asked to play, tagged by response item."""
    fake = FakeTurnSink()
    a = _chunk(rate=_OUT_RATE, fill=0x11)
    b = _chunk(rate=_OUT_RATE, fill=0x22)
    await fake.play(a, item_id="item_1")
    await fake.play(b, item_id="item_1")
    assert fake.played == [("item_1", a), ("item_1", b)]


async def test_stop_reports_the_scripted_played_ms_and_counts() -> None:
    """SDS §6.2.4: stop() returns the injected ``played_ms`` — a test chooses the honest
    ``audio_end_ms`` the barge-in truncate path will carry — and `stops` counts the calls."""
    fake = FakeTurnSink(played_ms=480)
    assert fake.stops == 0
    assert await fake.stop() == 480
    assert await fake.stop() == 480
    assert fake.stops == 2


async def test_stop_defaults_played_ms_to_zero() -> None:
    """No injected figure → 0 ms played, the honest answer when nothing was scripted."""
    assert await FakeTurnSink().stop() == 0
