"""CapturingRealtimeClient — the ``--capture`` recorder round-trips through replay (#105, AC-4).

The capture format is only trustworthy if what it *writes* is exactly what ``replay`` *reads*.
This suite proves that offline, with no network: wrap a ``ReplayRealtimeClient`` (playing a
committed fixture) in :class:`CapturingRealtimeClient`, drain it, then load the freshly written
directory back through ``ReplayRealtimeClient.from_dir`` — and the neutral event stream must come
back identical (types, item ids, token usage, PCM bytes). That is what stops the committed
fixtures drifting from real API behaviour: they are produced by this recorder against the live
API, and this test guarantees the recorder is faithful.

A ``FakeClock`` drives all timing (no wall clock); a fake inner client means **no mocks** are
needed even though this lives under ``tests/adapters/`` (SDS §14.3).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.realtime import CapturingRealtimeClient, ReplayRealtimeClient
from avid.core.hal import AudioChunk
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    RealtimeEvent,
    ToolCallRequested,
    TurnDone,
)

_SESSIONS = Path(__file__).resolve().parents[2] / "assets" / "sessions"


async def _drain(client: object, clock: FakeClock) -> list[RealtimeEvent]:
    """Collect everything ``events()`` yields, driving the injected clock (never wall time)."""
    collected: list[RealtimeEvent] = []

    async def consume() -> None:
        async for event in client.events():  # type: ignore[attr-defined]
            collected.append(event)

    task = asyncio.create_task(consume())
    for _ in range(100):
        await clock.advance(2.0)
        if task.done():
            break
    await task
    return collected


def _item_ids(events: list[RealtimeEvent]) -> list[str]:
    return [
        e.item_id
        for e in events
        if isinstance(e, AssistantAudioChunk | AssistantTranscript)
    ]


def _usages(events: list[RealtimeEvent]) -> list[object]:
    return [e.usage for e in events if isinstance(e, TurnDone)]


def _pcms(events: list[RealtimeEvent]) -> list[bytes]:
    return [e.chunk.pcm for e in events if isinstance(e, AssistantAudioChunk)]


def _tool_calls(events: list[RealtimeEvent]) -> list[ToolCallRequested]:
    return [e for e in events if isinstance(e, ToolCallRequested)]


@pytest.mark.parametrize("name", ["two_turn", "barge_in", "session_loss", "tool_call"])
async def test_capture_round_trips_through_replay(name: str, tmp_path: Path) -> None:
    """Every committed fixture, captured then reloaded, yields an identical neutral stream.

    The ``tool_call`` fixture proves a ``ToolCallRequested`` survives the capture→replay round
    trip (#124, AC-6) — so ``--capture`` can record a live tool exchange into the fixture format
    and replay can never drift from real API behaviour."""
    src_clock = FakeClock()
    inner = ReplayRealtimeClient.from_dir(_SESSIONS / name, clock=src_clock)
    out_dir = tmp_path / name
    capturing = CapturingRealtimeClient(inner=inner, clock=src_clock, out_dir=out_dir)
    await capturing.open()
    original = await _drain(capturing, src_clock)
    await capturing.aclose()  # flushes session.json + the WAVs

    assert (out_dir / "session.json").exists()

    re_clock = FakeClock()
    reloaded_client = ReplayRealtimeClient.from_dir(out_dir, clock=re_clock)
    await reloaded_client.open()
    reloaded = await _drain(reloaded_client, re_clock)

    assert [type(e) for e in reloaded] == [type(e) for e in original]
    assert _item_ids(reloaded) == _item_ids(original)
    assert _usages(reloaded) == _usages(original)
    assert _pcms(reloaded) == _pcms(original)  # assistant PCM survives byte-for-byte
    # A captured tool call round-trips with call_id/name/arguments intact (#124, AC-6).
    assert _tool_calls(reloaded) == _tool_calls(original)


async def test_captured_manifest_is_format_1(tmp_path: Path) -> None:
    """The written manifest declares the supported format the loader checks (AC-5)."""
    clock = FakeClock()
    inner = ReplayRealtimeClient.from_dir(_SESSIONS / "two_turn", clock=clock)
    out_dir = tmp_path / "cap"
    capturing = CapturingRealtimeClient(inner=inner, clock=clock, out_dir=out_dir)
    await capturing.open()
    await _drain(capturing, clock)
    await capturing.aclose()

    manifest = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    assert manifest["format"] == 1
    assert manifest["events"]  # non-empty
    assert manifest["events"][0]["delay_ms"] == 0  # first event has no preceding gap


async def test_empty_session_captures_an_empty_manifest(tmp_path: Path) -> None:
    """An empty inner session records a valid, empty fixture that reloads to nothing."""
    clock = FakeClock()
    inner = ReplayRealtimeClient(clock=clock, timeline=())
    out_dir = tmp_path / "empty"
    capturing = CapturingRealtimeClient(inner=inner, clock=clock, out_dir=out_dir)
    await capturing.open()
    assert await _drain(capturing, clock) == []
    await capturing.aclose()

    reload_clock = FakeClock()
    replay = ReplayRealtimeClient.from_dir(out_dir, clock=reload_clock)
    await replay.open()
    assert await _drain(replay, reload_clock) == []


async def test_capture_delegates_control_calls_to_the_inner_client(
    tmp_path: Path,
) -> None:
    """send_audio/truncate/cancel pass straight through to the wrapped client (a decorator)."""
    clock = FakeClock()
    inner = ReplayRealtimeClient(clock=clock, timeline=())
    capturing = CapturingRealtimeClient(
        inner=inner, clock=clock, out_dir=tmp_path / "d"
    )
    await capturing.send_audio(
        AudioChunk(pcm=b"\x00\x01", sample_rate=16_000, channels=1)
    )
    await capturing.truncate("item_0", 480)
    await capturing.cancel()
    await capturing.send_tool_output("call_0", '{"facts": []}')
    assert len(inner.sent) == 1
    assert inner.truncations == [("item_0", 480)]
    assert inner.cancels == 1
    assert inner.tool_outputs == [("call_0", '{"facts": []}')]
