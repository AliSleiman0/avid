"""On-Pi audio bench demo — loopback round-trip latency + VAD gate accuracy (AVID-90).

PMP §5.2's M4 gate has two demonstrable parts: **≤ 200 ms round-trip** and *local VAD gates
speech vs. silence over ~10 min*. #87 shipped ``AudioService`` (mic → VAD gate → loopback echo →
speaker) and #89 wired it into the composition root, so ``avid`` boots with a live audio loop.
This script is the missing exerciser for both gate parts, so #91 is just "run it on the Pi and
tag."

**Two modes:**

- ``--mode loopback`` drives the mic → ``AudioService`` → speaker path through the **real** bus
  and whichever adapters ``--config`` selects — ``Fake*`` on a laptop, ``AlsaMicrophone`` /
  ``AlsaSpeaker`` / ``SileroVad`` on the Pi — and prints the per-turn **round-trip latency** with
  a ``min / median / max / count`` summary, **exiting non-zero if the max blows the budget**.
- ``--mode vad --wav clip.wav --labels clip.labels.json`` replays a labelled recording through
  the config-selected VAD frame by frame and reports **false-open / missed-speech** counts — the
  10-min-VAD gate part.

**The playback-integrity gate (AVID-91).** Turnaround alone is not enough: this harness once
printed ``PASS: all 3 turns within the 200 ms turnaround budget`` **while the robot was mute**,
because ``playback_started − speech_ended`` proves only that the *event* path fired. It says
nothing about PCM reaching a DAC. So every run also checks two things about the audio itself:
``played_ms > 0`` — now a device-sourced figure, since ``Speaker.play`` returns the ms it
accepted — and, behind a real speaker, that the playback's **wall-clock elapsed time matches the
ms played**. A mute run reports 6000 ms played in ~0 ms elapsed; a run at the wrong sample rate
reports 6000 ms played in 4010 ms elapsed. Both now fail. A gate that can pass on silence is not
a gate, and a gate that quietly disarms itself is the same bug wearing a hat — hence the loud
``NOT CHECKED`` line when the config selects a fake speaker.

**What "round-trip latency" means here, and why.** ``AudioService`` is *turn-based*: it buffers
a whole utterance and echoes it with one ``speaker.play`` **after** ``audio.speech_ended`` (it is
the M4 loopback stand-in for the M5 AI client, not a per-frame passthrough). So the only honest
number derivable from event ``monotonic_ns`` deltas is the **processing turnaround** —
``playback_started.monotonic_ns − speech_ended.monotonic_ns`` — the latency the software adds
between "the turn is over" and "the echo begins". That is §2.8.1's "our ~150 ms": the VAD
debounce (a deliberate turn-detection delay) and the utterance's own duration are excluded on
purpose. This mirrors ``face_pi.py``, which measures software render latency, not photons on
glass. The *physical* mouth-to-ear ≤ 200 ms is verified by ear on the Pi at the #91 gate; this
demo machine-checks that no blocking work sneaks into the turn-end → playback path.

**Why the loopback is driven, not "run the robot".** At M4 nothing publishes ``conversation.*``,
so the loopback echo is the whole downstream path. On the Pi the config selects the real
``SileroVad`` and a human speaks; on a laptop the config's ``FakeVoiceActivityDetector`` cannot
self-trigger, so — exactly as AVID-74 drove ``set_affect`` by hand — the demo substitutes a
scripted VAD timeline and a fake mic to produce ``--turns`` deterministic turns.

**Pi-or-laptop bench tool — not application code.** Like ``hal_pi.py`` / ``face_pi.py`` it builds
its own object graph and lives in ``docs/``, outside P3's composition root and the ``avid/``
purity greps. It reuses the composition root's ``_build_microphone`` / ``_build_speaker`` /
``_build_vad`` switches rather than re-listing them, and the adapters lazy-import their Pi-only
libs, so this file imports fine off-Pi; only ``--config config/pi.toml`` touches hardware. Run:

    uv run python docs/demos/audio_pi.py --mode loopback --config config/sim.toml   # laptop
    /opt/avid/.venv/bin/python docs/demos/audio_pi.py --mode loopback --config config/pi.toml  # Pi

See ``docs/demos/README.md`` (M4 section) for the full gate runbook (AVID-91).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

# SystemClock is the injectable real Clock (adapters name it, not "RealClock").
from avid.adapters import FakeMicrophone, FakeVoiceActivityDetector, SystemClock
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk
from avid.core.ports import Clock, Microphone, VoiceActivityDetector
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    Event,
    RobotState,
)
from avid.main import _build_microphone, _build_speaker, _build_vad
from avid.services import AudioService

_NS_PER_MS = 1_000_000.0
_LATENCY_BUDGET_MS = 200.0  # PMP §5.2 — the number the M4 milestone is graded on.
_SPEECH_FRAMES = 5  # scripted (laptop) turn length, in mic frames.
_LIVE_TIMEOUT_S = 120.0  # how long to wait for a human to speak the requested phrases.

# How far a turn's wall-clock playback may diverge from the ms the device says it played.
# BOTH bounds are needed, because the two error sources scale differently: the ALSA ring
# buffer is a FIXED depth (~107 ms at 24 kHz with a 2560-frame buffer) that a relative
# bound would call fine on a 6 s echo and a failure on a 300 ms cue, while a wrong sample
# rate is a PROPORTIONAL error (1.5x) that an absolute bound would miss on long audio.
_PLAYBACK_ABS_TOL_MS = 250.0  # buffer depth + a device reopen + scheduling slop
_PLAYBACK_REL_TOL = 0.15


@dataclass(frozen=True, slots=True)
class _Turn:
    """One completed turn: the graded metric plus the evidence that audio really played."""

    turnaround_ms: float  # playback_started − speech_ended (the number M4 is graded on)
    played_ms: int  # what the speaker reported accepting (AVID-91)
    elapsed_ms: float  # playback_finished − playback_started, wall


class _LatencyCollector:
    """Records per-turn turnaround **and playback integrity** from ``audio.*`` events.

    Subscribed (before the bus starts, P3) to ``speech_ended`` / ``playback_started`` /
    ``playback_finished``. For each turn it pairs the origin's ``speech_ended`` with the echo's
    ``playback_started`` by ``correlation_id`` and records ``(started − ended)`` in ms — the
    software turnaround (see the module docstring). ``playback_finished`` marks the turn complete
    and fires the awaitable so the driver never has to sleep-and-hope.

    It also records how long playback actually took and how much the speaker said it played.
    Those two came free — both events were already subscribed — and they are what separates a
    passing gate from a mute one (AVID-91).
    """

    def __init__(self) -> None:
        self._ended_ns: dict[UUID, int] = {}
        self._started_ns: dict[UUID, int] = {}
        self.samples: list[float] = []
        self.turns: list[_Turn] = []
        self._arrived = asyncio.Event()

    async def on_speech_ended(self, event: Event) -> None:
        self._ended_ns[event.correlation_id] = event.monotonic_ns

    async def on_playback_started(self, event: Event) -> None:
        self._started_ns[event.correlation_id] = event.monotonic_ns

    async def on_playback_finished(self, event: Event) -> None:
        cid = event.correlation_id
        ended = self._ended_ns.get(cid)
        started = self._started_ns.get(cid)
        if ended is not None and started is not None:
            # monotonic, never timestamp_ms: wall-clock steps (NTP, boot correction) yield
            # negative latencies that poison the graded metric (SDS §9.1.1).
            turnaround = (started - ended) / _NS_PER_MS
            self.samples.append(turnaround)
            played = event.played_ms if isinstance(event, AudioPlaybackFinished) else 0
            self.turns.append(
                _Turn(
                    turnaround_ms=turnaround,
                    played_ms=played,
                    elapsed_ms=(event.monotonic_ns - started) / _NS_PER_MS,
                )
            )
        self._arrived.set()

    async def wait_for_turns(self, count: int, *, timeout_s: float) -> None:
        """Block until *count* turns have echoed, or fail loudly (never a sleep)."""
        async with asyncio.timeout(timeout_s):
            while len(self.samples) < count:
                self._arrived.clear()
                if len(self.samples) >= count:
                    return
                await self._arrived.wait()


def _scripted_source(
    config: Config, *, turns: int
) -> tuple[Microphone, VoiceActivityDetector]:
    """A fake mic + scripted VAD that produce *turns* deterministic turns (laptop path).

    The VAD walks a ``[speech…, silence…] × turns`` timeline (one verdict per frame), holding
    ``False`` once exhausted; the silence run per turn is one frame longer than
    ``silence_hold_ms`` so each turn closes. The mic streams a fixed non-synth frame (explicit
    ``pcm`` skips ``FakeMicrophone``'s tone loop), and the VAD ignores the PCM — the timeline is
    the whole point.
    """
    chunk_ms = config.microphone.chunk_ms
    silence_frames = config.gate.silence_hold_ms // chunk_ms + 1
    script: list[bool] = []
    for _ in range(turns):
        script.extend([True] * _SPEECH_FRAMES)
        script.extend([False] * silence_frames)
    mic = FakeMicrophone(
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        chunk_ms=chunk_ms,
        pcm=b"\x00\x00",
    )
    vad = FakeVoiceActivityDetector(script=script)
    return mic, vad


async def _run_loopback(
    config: Config, *, turns: int, budget_ms: float, clock: Clock
) -> int:
    """Drive *turns* loopback turns, print the latency table, return 0 iff within budget."""
    # At M4 the LISTENING→IDLE arc is incomplete — it needs the 30 s listen-timeout or M5's
    # ConversationService — so after the first turn the robot stays LISTENING, and a later
    # turn's speech_started is an ignored illegal transition (logged by StateManager). That is
    # expected M4 behaviour and irrelevant to the turnaround we measure, so quiet that one
    # warning to keep the latency table readable.
    logging.getLogger("avid.state").setLevel(logging.ERROR)

    bus = AsyncioEventBus(clock=clock)
    # The audio loop only runs once boot has reached IDLE (the lifecycle drives BOOTING→IDLE
    # before starting services); start there so the first speech_started is a legal transition,
    # exactly as the running robot and the AudioService rig do.
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)
    speaker = _build_speaker(config)

    live = config.adapters.vad != "fake"
    if live:
        mic: Microphone = _build_microphone(config)
        vad: VoiceActivityDetector = _build_vad(config)
        timeout_s = _LIVE_TIMEOUT_S
    else:
        mic, vad = _scripted_source(config, turns=turns)
        silence_frames = config.gate.silence_hold_ms // config.microphone.chunk_ms + 1
        total_frames = turns * (_SPEECH_FRAMES + silence_frames)
        timeout_s = (total_frames + 20) * config.microphone.chunk_ms / 1000.0 + 5.0

    service = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=config.gate.ring_buffer_ms,
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        silence_hold_ms=config.gate.silence_hold_ms,
        # From config like every other knob here (AVID-180) — an omitted one silently reports the
        # default, which is how the echo-gate margin went unmeasured across four bench runs.
        barge_in_margin_db=config.gate.barge_in_margin_db,
        echo_tail_ms=config.gate.echo_tail_ms,
        # The transport gate has no ConversationService to consume the TurnSink seam, so keep the
        # M4 echo: _end_speech loops the captured utterance straight back to the speaker (#103).
        loopback=True,
    )

    collector = _LatencyCollector()
    # Register before the bus starts — subscription is static-at-composition (P3).
    bus.subscribe(
        AudioSpeechEnded, collector.on_speech_ended, name="audio_pi.speech_ended"
    )
    bus.subscribe(
        AudioPlaybackStarted,
        collector.on_playback_started,
        name="audio_pi.playback_started",
    )
    bus.subscribe(
        AudioPlaybackFinished,
        collector.on_playback_finished,
        name="audio_pi.playback_finished",
    )

    async with bus:
        await service.start()
        try:
            if live:
                print(f"Speak {turns} short phrases (pause between each); listening...")
            await collector.wait_for_turns(turns, timeout_s=timeout_s)
        except TimeoutError:
            print(f"TIMEOUT: heard {len(collector.samples)}/{turns} turns")
        finally:
            await service.stop()

    return _report_loopback(
        collector.turns,
        turns=turns,
        budget_ms=budget_ms,
        # Gated on the SPEAKER, not the `live` flag above (which is the VAD's): a real
        # speaker driven by a scripted VAD is exactly how this fix gets benched, and it
        # must still be checked. FakeSpeaker.play returns instantly, so elapsed-vs-played
        # would false-fail there.
        check_playback=config.adapters.speaker != "fake",
    )


def _diverged(turn: _Turn) -> bool:
    """Did this turn's wall-clock playback disagree with the ms the device says it played?

    The tolerance is the looser of an absolute floor and a relative band — see the constants.
    Catches a mute speaker (6000 ms "played" in ~0 ms) and a wrong-rate one (6000 ms in
    4010 ms) alike, because both are the same lie told at different scales."""
    allowed = max(_PLAYBACK_ABS_TOL_MS, _PLAYBACK_REL_TOL * turn.played_ms)
    return abs(turn.elapsed_ms - turn.played_ms) > allowed


def _report_loopback(
    turns_seen: list[_Turn], *, turns: int, budget_ms: float, check_playback: bool
) -> int:
    """Print the per-turn table + summary; return the process exit code.

    Two independent gates. **Turnaround** is the graded PMP §5.2 metric. **Playback
    integrity** is the AVID-91 gate: a turn that played nothing fails always, and — behind a
    real device — a turn whose wall-clock playback disagrees with its reported ms fails too.
    ``check_playback`` is false when the config selected a fake speaker, whose instant
    ``play`` makes elapsed-vs-played meaningless; that case prints its own scope rather than
    silently skipping, because a quietly disarmed check is the very defect being fixed."""
    samples = [turn.turnaround_ms for turn in turns_seen]
    print(f"{'turn':<6} {'round-trip':>14} {'played':>10} {'elapsed':>10}")
    print(f"{'-' * 6} {'-' * 14} {'-' * 10} {'-' * 10}")
    for index, turn in enumerate(turns_seen, start=1):
        print(
            f"{index:<6} {turn.turnaround_ms:>11.2f} ms {turn.played_ms:>7d} ms "
            f"{turn.elapsed_ms:>7.0f} ms"
        )
    print(f"{'-' * 6} {'-' * 14} {'-' * 10} {'-' * 10}")
    if not samples:
        print("FAIL: no turns captured")
        return 1
    worst = max(samples)
    print(
        f"min {min(samples):.2f} ms / median {statistics.median(samples):.2f} ms / "
        f"max {worst:.2f} ms / count {len(samples)}"
    )
    if len(samples) != turns:
        print(f"FAIL: captured {len(samples)}/{turns} turns")
        return 1
    if worst > budget_ms:
        print(f"FAIL: max turnaround {worst:.2f} ms exceeds {budget_ms:.0f} ms budget")
        return 1

    # Playback integrity. The silent-turn half is device-independent: played_ms comes from
    # Speaker.play's return, so zero means the device took nothing (AVID-91).
    silent = [i for i, turn in enumerate(turns_seen, start=1) if turn.played_ms == 0]
    if silent:
        print(f"FAIL: {len(silent)} turn(s) played no audio at all: {silent}")
        return 1
    if check_playback:
        bad = [i for i, turn in enumerate(turns_seen, start=1) if _diverged(turn)]
        if bad:
            print(
                f"FAIL: {len(bad)} turn(s) played for the wrong length of time: {bad} — "
                f"audio is being dropped or played at the wrong sample rate"
            )
            return 1

    print(f"PASS: all {turns} turns within the {budget_ms:.0f} ms turnaround budget")
    if check_playback:
        worst_div = max(
            abs(turn.elapsed_ms - turn.played_ms) / max(1, turn.played_ms)
            for turn in turns_seen
        )
        print(
            f"      playback integrity: {turns}/{turns} turns, "
            f"worst divergence {worst_div * 100:.1f}%   (speaker=alsa)"
        )
    else:
        print(
            '      playback integrity: NOT CHECKED — [adapters] speaker = "fake"; this run '
            "proves\n      nothing about audio reaching a DAC (see docs/demos/README.md, M4)"
        )
    return 0


def _read_wav_frames(path: Path, *, chunk_ms: int) -> tuple[list[bytes], int, int]:
    """Read a 16-bit PCM WAV into ``chunk_ms`` frames (sync — off the event loop, P8)."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path} is not 16-bit PCM (S16_LE)")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    frame_bytes = rate * channels * 2 * chunk_ms // 1000
    frames = [
        raw[offset : offset + frame_bytes]
        for offset in range(0, len(raw) - frame_bytes + 1, frame_bytes)
    ]
    return frames, rate, channels


def _read_labels(path: Path) -> list[tuple[int, int]]:
    """Read a sidecar JSON list of ``[start_ms, end_ms]`` speech spans."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(int(start), int(end)) for start, end in data]


def _run_vad(config: Config, *, wav: Path, labels: Path) -> int:
    """Replay *wav* through the config-selected VAD and report gate accuracy (AC-3).

    Meaningful with ``SileroVad`` on the Pi; on a laptop the config's fake VAD reports every
    frame as silence (it has no scripted timeline here), so this mode is a Pi metric.
    """
    chunk_ms = config.microphone.chunk_ms
    frames, rate, channels = _read_wav_frames(wav, chunk_ms=chunk_ms)
    spans = _read_labels(labels)
    vad = _build_vad(config)

    false_open = 0
    missed = 0
    speech_frames = 0
    for index, pcm in enumerate(frames):
        midpoint_ms = index * chunk_ms + chunk_ms // 2
        truth = any(start <= midpoint_ms < end for start, end in spans)
        verdict = vad.is_speech(
            AudioChunk(pcm=pcm, sample_rate=rate, channels=channels)
        )
        speech_frames += int(truth)
        if verdict and not truth:
            false_open += 1
        if truth and not verdict:
            missed += 1

    silence_frames = len(frames) - speech_frames
    print(f"frames        {len(frames)} ({chunk_ms} ms each, {rate} Hz)")
    print(f"speech frames {speech_frames}   silence frames {silence_frames}")
    print(f"false-open    {false_open}   ({_rate(false_open, silence_frames)})")
    print(f"missed-speech {missed}   ({_rate(missed, speech_frames)})")
    return 0


def _rate(count: int, total: int) -> str:
    return f"{100.0 * count / total:.1f}%" if total else "n/a"


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``audio_pi`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="audio_pi",
        description="Audio loopback latency + VAD accuracy harness (AVID-90 / M4 gate).",
    )
    parser.add_argument(
        "--config",
        default="config/sim.toml",
        help="Config profile selecting the audio adapters (default: config/sim.toml).",
    )
    parser.add_argument(
        "--mode",
        choices=("loopback", "vad"),
        default="loopback",
        help="loopback: round-trip latency. vad: replay a labelled WAV for gate accuracy.",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=3,
        help="loopback: number of turns to drive/expect (default: 3).",
    )
    parser.add_argument(
        "--budget-ms",
        type=float,
        default=_LATENCY_BUDGET_MS,
        help=f"loopback: max round-trip turnaround (default: {_LATENCY_BUDGET_MS:.0f}).",
    )
    parser.add_argument(
        "--wav", type=Path, help="vad: path to the labelled 16-bit PCM WAV."
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help="vad: sidecar JSON, a list of [start_ms, end_ms] speech spans.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: pick a config profile and a mode; SystemClock so latencies are real."""
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.mode == "vad":
        if args.wav is None or args.labels is None:
            build_parser().error("--mode vad requires --wav and --labels")
        return _run_vad(config, wav=args.wav, labels=args.labels)
    return asyncio.run(
        _run_loopback(
            config, turns=args.turns, budget_ms=args.budget_ms, clock=SystemClock()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
