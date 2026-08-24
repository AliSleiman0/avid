#!/usr/bin/env python
"""The soak load generator — give the endurance window something to measure (#389, SDS §12.6).

O5's software half is *"leaks, wedges, unbounded growth, reconnection decay"*, and none of those
appear in a robot that is doing nothing. **The first M11 window measured exactly that**: wedged in
`THINKING` from minute 3, 99.89% uptime, every graded criterion passing. #452 fixed the wedge and
#457 gave the soak a criterion that can *see* one — but an unattended robot still does nothing, by
design: §10.4's presence rule vetoed four of the last five proactive turns. A window over an idle
process produces a flat RSS curve, and a flat curve on an idle process is the **absence of a test**,
not a passing one.

This plays recorded speech into the room on a schedule, so the robot runs real turns:

    soak_load.py --mode validate --corpus assets/load    # off-robot: will these clips trip the gate?
    soak_load.py --mode play     --corpus /opt/avid/assets/load --max-turns 500 --max-usd 15

⚠️ **The room is the load path, and that is a measurement, not an assumption.** Proven on the rig
2026-08-24 12:05:27: a WAV played from a second process drove `speech_started -> THINKING -> a real
Realtime session (3948 ms cold) -> a spoken reply -> IDLE`. Nothing about the robot was modified to
make that happen — no relaxed config, no spoofed presence.

⚠️ **THE CAP HERE IS THE ONLY CAP THAT EXISTS.** §10.4's policy gate — quiet hours, the global
cooldown, the daily budget, the presence rule — has exactly one call site, and it is the *proactive*
path. Nothing in this repo rate-limits a **reactive** turn, because until now the only thing that
could start one was a human being in the room. A generator can drive thousands of paid turns in a
weekend and no rule anywhere will stop it. There is no backstop behind the two caps below.

⚠️ **Spend is read, never modelled.** `/metrics` reports `cost_usd` (true) *and*
`projected_monthly_usd` (a model: cost-per-turn x 20 turns/day x 30 days, which ignores the observed
rate entirely). A generator at 500 turns/day leaves the projection looking healthy while real spend
runs 25x the model, so this caps on `cost_usd` and nothing else.

⚠️ **It will suppress the robot's own proactive turns, and that is not a regression.** §10.4's rule 4
vetoes when ambient speech exceeds 60 s in a 5-minute window with no session opened — which is
precisely what this manufactures. Expect `triggers_fired` to collapse and `proactive_log` to fill
with `ambient_speech` / `state` reasons for the length of any load window. The soak's LOAD criterion
says so in its own report, because a reader who does not know this will diagnose a defect.

Held to CLAUDE.md §7.1 like every other instrument here:

* **It records what it actually did**, one line per utterance, so the report can state the work the
  window contained rather than the work it was configured for.
* **It never raises.** A refused device, an unreachable robot, a turn that never lands — each is a
  recorded datum and the loop continues. A generator that dies at 3 a.m. leaves a window that looks
  idle, which is the failure it exists to prevent.
* **It says which cap stopped it.** A run that stops silently is indistinguishable from one that
  crashed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.core.config import load_config  # noqa: E402
from avid.domain.audio import rms_dbfs  # noqa: E402

# ── reporting (the shared shape with soak_pi.py / motion_pi.py) ──────────────────────────────

_ASCII = {
    "—": "--",
    "–": "-",
    "→": "->",
    "≥": ">=",
    "≤": "<=",
    "§": "S",
    "⚠️": "!!",
    "⚠": "!!",
    "·": ".",
    "’": "'",
}


def _say(text: str = "") -> None:
    """Print *text*, folded to something every console can render.

    ``flush=True`` unlike ``soak_pi``'s: this writes a live line per utterance over hours, and a
    buffered generator looks identical to a hung one in `journalctl`.
    """
    for source, plain in _ASCII.items():
        text = text.replace(source, plain)
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


_Verdict = Literal["pass", "fail", "recorded", "inconclusive"]


@dataclass
class _Criterion:
    """One reported line, held rather than printed so every criterion reports before any verdict."""

    ac: str
    name: str
    verdict: _Verdict
    detail: str = ""
    rows: list[str] = field(default_factory=list)


# ── the corpus ───────────────────────────────────────────────────────────────────────────────

_MANIFEST = "manifest.json"

# What a clip must clear to be worth playing. Both are about the gate, not about taste.
#
# Silero decides per 512-sample window at 16 kHz (~32 ms) against `[gate] threshold`, so a clip
# needs a run of voiced windows rather than one lucky frame. 15 windows is ~0.5 s of speech — well
# under the 1.4-3.7 s of the shipped cue clips, and well over the 60 ms blip that opened a phantom
# session on the bench (AVID-171).
_MIN_SPEECH_WINDOWS = 15
# Below this the clip is unlikely to clear a real room. The rig measured an empty room at -36 dBFS
# with AGC off; the probe that proved this load path measured played speech arriving at -22.3 dBFS
# peak. This is a floor on the FILE, which is a necessary and not a sufficient condition — the room
# is what decides, and `--mode validate` says so.
_MIN_PEAK_DBFS = -30.0


@dataclass(frozen=True, slots=True)
class _Utterance:
    """One thing to say, and what it is for."""

    key: str
    path: Path
    text: str
    expects_write: bool


def _read_corpus(corpus: Path) -> list[_Utterance]:
    """Load the manifest and resolve each clip beside it.

    Raises on a malformed corpus, deliberately and unlike everything else in this file: a broken
    corpus is a *setup* error, caught before a 72-hour run starts, not a datum recorded during one.
    ``ReplayRealtimeClient.from_dir`` draws the same line for the same reason.
    """
    manifest = json.loads((corpus / _MANIFEST).read_text(encoding="utf-8"))
    utterances: list[_Utterance] = []
    for entry in manifest["utterances"]:
        path = corpus / entry["file"]
        if not path.is_file():
            raise ValueError(f"{entry['file']!r} is in {_MANIFEST} but not on disk")
        utterances.append(
            _Utterance(
                key=str(entry["key"]),
                path=path,
                text=str(entry["text"]),
                expects_write=bool(entry.get("expects_write", False)),
            )
        )
    if not utterances:
        raise ValueError(f"{corpus / _MANIFEST} declares no utterances")
    return utterances


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """``(pcm, rate, channels, sampwidth)`` for a WAV, without loading a codec."""
    with wave.open(str(path), "rb") as wav:
        return (
            wav.readframes(wav.getnframes()),
            wav.getframerate(),
            wav.getnchannels(),
            wav.getsampwidth(),
        )


def _peak_dbfs(pcm: bytes) -> float:
    """Peak level of S16_LE *pcm* in dBFS. Digital silence is -inf."""
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        peak = max(peak, abs(sample))
    return -math.inf if peak == 0 else 20.0 * math.log10(peak / 32768.0)


def _speech_windows(pcm: bytes, *, sample_rate: int, threshold: float) -> int | None:
    """How many frames the **real** Silero adapter calls speech. ``None`` when it is unavailable.

    The real adapter, never a reimplementation — a validator that modelled the VAD would be
    grading its own model. ``None`` rather than 0 off-Pi, because zero speech frames is exactly
    what an unusable clip looks like and the two must not share an answer (#380).
    """
    try:
        from avid.adapters.vad import SileroVad
        from avid.core.hal import AudioChunk
    except ImportError:  # pragma: no cover - bench tool, off-Pi
        return None
    try:
        vad = SileroVad(sample_rate=sample_rate, threshold=threshold)
        frame_bytes = sample_rate * 2 * 20 // 1000  # the mic's own 20 ms chunking
        speech = 0
        for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = AudioChunk(
                pcm=pcm[start : start + frame_bytes],
                sample_rate=sample_rate,
                channels=1,
            )
            if vad.is_speech(chunk):
                speech += 1
        return speech
    except Exception as exc:  # noqa: BLE001 - a bench tool reports, it does not crash
        # ⚠️ Distinct from the ImportError above, and the distinction is the point. That one means
        # the VAD is ABSENT; this one means it was present and REFUSED the clip. Reporting both as
        # "unavailable" is what made this validator's first run describe a missing dependency that
        # was in fact installed.
        _say(f"  (the VAD refused this clip: {type(exc).__name__}: {exc})")
        return None


def _validate(args: argparse.Namespace) -> list[_Criterion]:
    """Grade the corpus against the gate that will hear it, before a window depends on it."""
    config = load_config(args.config)
    threshold = config.gate.threshold
    # ⚠️ The rate that matters is the MICROPHONE's, not the clip's. Silero supports 16 kHz and
    # 8 kHz only, the robot captures at `[microphone] sample_rate`, and the repo's resampler
    # deliberately **refuses to downsample** without an anti-alias filter. So a clip recorded at
    # any other rate cannot be judged by the gate it will actually meet, and resampling it here
    # would be inventing the measurement.
    mic_rate = config.microphone.sample_rate
    corpus = Path(args.corpus)
    utterances = _read_corpus(corpus)

    rows: list[str] = []
    unusable: list[str] = []
    unmeasured = 0
    for utterance in utterances:
        pcm, rate, channels, width = _read_wav(utterance.path)
        seconds = len(pcm) / float(rate * channels * width)
        peak = _peak_dbfs(pcm) if width == 2 else float("nan")
        level = rms_dbfs(pcm) if width == 2 else float("nan")

        playable = width == 2 and channels == 1
        judgeable = playable and rate == mic_rate
        windows = (
            _speech_windows(pcm, sample_rate=rate, threshold=threshold)
            if judgeable
            else None
        )
        if judgeable and windows is None:
            unmeasured += 1

        verdict = "ok"
        if not playable:
            verdict = "NOT MONO 16-BIT"
            unusable.append(utterance.key)
        elif rate != mic_rate:
            # A corpus defect, named as one. Reporting this as "the VAD was unavailable" is how
            # the first run of this validator described something other than what happened: both
            # onnxruntime and the model were present, and it was handed a rate no Silero build
            # supports.
            verdict = f"WRONG RATE (mic is {mic_rate})"
            unusable.append(utterance.key)
        elif peak < _MIN_PEAK_DBFS:
            verdict = "TOO QUIET"
            unusable.append(utterance.key)
        elif windows is not None and windows < _MIN_SPEECH_WINDOWS:
            verdict = "NOT SPEECH ENOUGH"
            unusable.append(utterance.key)
        rows.append(
            f"{utterance.key:<14} {seconds:>5.2f}s  {rate} Hz x{channels}  "
            f"peak {peak:>6.1f}  rms {level:>6.1f}  "
            f"silero {'--' if windows is None else windows:>4}  {verdict}"
        )

    criteria = [
        _Criterion(
            "CORPUS",
            f"{len(utterances)} utterance(s) declared and present",
            "pass",
            f"read from {corpus / _MANIFEST}",
            rows=rows,
        )
    ]
    if unmeasured:
        criteria.append(
            _Criterion(
                "VAD",
                "every clip was judged by the real Silero adapter",
                "inconclusive",
                f"{unmeasured} clip(s) at the right rate could not be measured — onnxruntime or "
                f"the Silero model is absent here. ABSENT, not zero: a clip nothing judged is not "
                f"a clip that passed. Re-run this on the Pi before trusting the corpus. (A clip "
                f"at the WRONG rate is a different finding and fails GATE instead.)",
            )
        )
    criteria.append(
        _Criterion(
            "GATE",
            f"every clip clears the gate (>= {_MIN_SPEECH_WINDOWS} speech frames, "
            f"peak >= {_MIN_PEAK_DBFS:g} dBFS)",
            "fail" if unusable else "pass",
            f"unusable: {', '.join(unusable)}"
            if unusable
            else f"threshold {threshold} read from {args.config}",
        )
    )
    criteria.append(
        _Criterion(
            "ROOM",
            "the file is not the room (reported, not graded)",
            "recorded",
            "⚠️ These levels are of the FILE. What decides is what arrives at the mic after the "
            "amp, the room and the distance — measured once at peak -22.3 dBFS against a -40 dBFS "
            "floor. A corpus that passes here can still be inaudible in a different room, so the "
            "first load run is always a bounded dry run.",
        )
    )
    return criteria


# ── the robot, seen from outside ─────────────────────────────────────────────────────────────


def _get_json(url: str, timeout: float) -> dict[str, Any] | None:
    """One reading, or ``None``. Never raises — an unreachable robot IS the measurement."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            parsed = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass
class _Meter:
    """Turns and dollars **this generator caused**, immune to the robot restarting.

    ⚠️ ``/metrics`` counters are **process-scoped**: they reset when the service restarts, and
    ``Restart=always`` means that will happen during a long window. Summing readings would
    double-count and taking the last would forget everything before the restart — so this
    accumulates **rises**, and treats a fall as a new process, exactly as ``soak_pi``'s
    ``_transition_total`` does for transitions.

    ⚠️ **The first reading is a baseline and contributes nothing.** It counted before, which meant
    the generator inherited every turn the robot had already taken as if it had driven them
    itself. On the rig this capped a run at *zero utterances* — ``max-turns (2) reached — turns
    8`` — because a human had been talking to the robot beforehand. Over a 72-hour window the
    failure is worse than it looks: one evening's conversation silently stops the generator for
    the remaining three days, and the window goes back to measuring an idle process. That is the
    exact void it exists to prevent, reached by the instrument rather than the robot.

    The caps mean *"this run may drive at most N turns / $X"*. Spend the owner incurred by
    talking is not this run's spend, and the run must not be charged for it.
    """

    turns: int = 0
    usd: float = 0.0
    _last_turns: int | None = None
    _last_usd: float | None = None

    def observe(self, turns: int | None, usd: float | None) -> None:
        if turns is not None:
            if self._last_turns is None:
                pass  # baseline: whatever the robot had already done is not ours
            elif turns >= self._last_turns:
                self.turns += turns - self._last_turns
            else:
                self.turns += turns  # restarted: the whole new count is new work
            self._last_turns = turns
        if usd is not None:
            if self._last_usd is None:
                pass  # baseline, as above
            elif usd >= self._last_usd:
                self.usd += usd - self._last_usd
            else:
                self.usd += usd
            self._last_usd = usd


def _log_line(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record. Never raises — losing the log must not stop the load.

    Same shape and discipline as ``interventions.jsonl``: one object per line, append-only, ``at``
    an int epoch. ``soak_pi``'s reader validates that field and nothing else.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:  # pragma: no cover - disk full / permissions
        _say(f"  !! could not write the load log: {exc}")


def _quiet_now(config: Any, at: float) -> bool:
    """Is *at* inside the configured quiet window?

    ⚠️ Read from config, never restated — and honoured **here**, because §10.4's gate will not do
    it for us. Quiet hours veto proactive turns only; a generator playing audio at 3 a.m. is a
    reactive turn and nothing in the robot would refuse it.
    """
    local = time.localtime(at)
    minutes = local.tm_hour * 60 + local.tm_min
    # `QuietHours.covers` delegates to the domain's own midnight-aware predicate. Calling that
    # predicate directly here would be a second implementation of the wrap, which the config's own
    # docstring calls "exactly one too many".
    return bool(config.behavior.quiet_hours.covers(minutes))


def _play(path: Path, device: str, rate: int) -> str | None:
    """Play one clip. Returns an error string, or ``None`` on success. Never raises.

    Uses the real ``AlsaSpeaker`` rather than raw ``alsaaudio`` — it already owns the -EPIPE retry
    and the short-write accounting that a bench reimplementation would get wrong. ``hal_pi.py`` is
    the precedent for constructing one outside the composition root.

    ⚠️ Playback only. This must never open the microphone: the robot holds
    ``plughw:CARD=Device,DEV=0``, which has no ``dsnoop``, so a second opener gets EBUSY — and the
    robot losing its mic mid-window would be this tool destroying the thing it exists to feed.
    """
    try:
        import asyncio

        from avid.adapters.speaker import AlsaSpeaker

        speaker = AlsaSpeaker(device=device, sample_rate=rate, channels=1)

        async def _run() -> None:
            try:
                await speaker.play_file(path)
            finally:
                # `stop()` is the teardown — it closes the handle. There is no `aclose` here, and
                # leaving the device open would hold it against the robot's own next cue.
                await speaker.stop()

        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - a failed play is a datum, not a crash
        return f"{type(exc).__name__}: {exc}"
    return None


# The states a robot can be spoken to from, and the states a finished turn settles into.
#
# ⚠️ **SLEEPING belongs here, and leaving it out made this generator useless at the only time it
# runs.** §3.10.3 has `(SLEEPING, audio.speech_started) -> LISTENING`: speech wakes the robot,
# which is the whole of M8's nap. The robot sleeps ten minutes after the room empties — and an
# empty room is precisely when an unattended window runs. Waiting for IDLE would have skipped
# **every utterance overnight** and produced a window measuring an idle robot: the void window
# again, caused this time by the tool built to prevent it. Found by looking at the rig and seeing
# it asleep at 18:01, before the first dry run rather than after a silent one.
_SPEAKABLE = frozenset({"IDLE", "SLEEPING"})


def _wait_for(
    base: str,
    timeout: float,
    want: frozenset[str],
    deadline_s: float,
    poll_s: float,
) -> tuple[bool, str | None]:
    """Poll ``/state`` until it reads one of *want*. Returns ``(reached, last_state)``.

    ⚠️ *deadline_s* must allow at least one poll. A zero deadline reports "not ready" without ever
    asking, which is indistinguishable from a busy robot — two of this file's own tests passed
    that way before it was noticed.
    """
    end = time.monotonic() + deadline_s
    last: str | None = None
    while time.monotonic() < end:
        reading = _get_json(f"{base}/state", timeout)
        last = None if reading is None else reading.get("state")
        if last in want:
            return True, last
        time.sleep(poll_s)
    return False, last


def _wait_for_turn(
    base: str,
    timeout: float,
    meter: _Meter,
    before: int,
    deadline_s: float,
    poll_s: float,
) -> bool:
    """Wait until the robot's own turn counter rises. Returns whether it did.

    ⚠️ **A state reading cannot answer this, and using one made ``turn_ok`` unfalsifiable.** The
    robot is ``SLEEPING`` before an overnight clip and ``SLEEPING`` after it, so "is it back in a
    settled state" is true whether it ran a turn or heard nothing at all. The first rig dry run
    recorded exactly that — ``turn_ok: true, latency_s: 5.28`` — against a robot whose journal
    held *no entries* for the surrounding eight minutes. A field that reads `true` when nothing
    happened is a report describing something other than the run (CLAUDE.md §7.1).

    The turn counter is the quantity ``LOAD`` grades, so this asks the robot the same question the
    gate will ask afterwards. Observing through *meter* rather than beside it also keeps the caps
    current while the turn runs, which matters when one clip triggers several turns.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        reading = _get_json(f"{base}/metrics", timeout)
        values = reading.get("metrics", {}) if isinstance(reading, dict) else {}
        meter.observe(values.get("turns"), values.get("cost_usd"))
        if meter.turns > before:
            return True
        time.sleep(poll_s)
    return False


def _run_load(args: argparse.Namespace) -> int:
    """Play the corpus on a schedule until a cap, a deadline or an operator stops it."""
    config = load_config(args.config)
    corpus = Path(args.corpus)
    utterances = _read_corpus(corpus)
    log = Path(args.load_log)
    base = f"http://{args.host}:{args.port}"
    device = config.speaker.device
    rate = config.speaker.sample_rate

    _say(
        f"load: {len(utterances)} utterance(s) from {corpus}, every {args.interval}s into {base}"
    )
    _say(
        f"caps: <= {args.max_turns} turns, <= ${args.max_usd:.2f}, "
        f"{'ignoring' if args.ignore_quiet_hours else 'honouring'} quiet hours; "
        f"log {log}"
    )
    _say("(stop with Ctrl-C or `systemctl stop soak-load`)")

    meter = _Meter()
    deadline = time.monotonic() + args.for_seconds if args.for_seconds else None
    index = 0
    stopped: str | None = None
    try:
        while deadline is None or time.monotonic() < deadline:
            now = time.time()
            metrics = _get_json(f"{base}/metrics", args.timeout) or {}
            values = metrics.get("metrics", {}) if isinstance(metrics, dict) else {}
            meter.observe(values.get("turns"), values.get("cost_usd"))

            # ── the caps, checked BEFORE the spend, never after ───────────────────────────────
            if meter.turns >= args.max_turns:
                stopped = f"max-turns ({args.max_turns})"
            elif meter.usd >= args.max_usd:
                stopped = f"max-usd (${args.max_usd:.2f})"
            if stopped is not None:
                _log_line(
                    log,
                    {
                        "at": int(now),
                        "kind": "capped",
                        "note": stopped,
                        "turns": meter.turns,
                        "cost_usd": round(meter.usd, 5),
                    },
                )
                _say(
                    f"  STOP  {stopped} reached — turns {meter.turns}, ${meter.usd:.4f}"
                )
                break

            utterance = utterances[index % len(utterances)]
            index += 1
            kind, note, turn_ok, latency = "played", utterance.key, False, 0.0

            if not args.ignore_quiet_hours and _quiet_now(config, now):
                kind, note = "skipped_quiet", "inside the configured quiet window"
            else:
                ready, seen = _wait_for(
                    base, args.timeout, _SPEAKABLE, args.settle_s, args.poll_s
                )
                if not ready:
                    kind, note = "skipped_busy", f"robot was {seen or 'unreachable'}"
                else:
                    started = time.monotonic()
                    turns_before = meter.turns
                    failure = _play(utterance.path, device, rate)
                    if failure is not None:
                        kind, note = "play_failed", failure
                    else:
                        turn_ok = _wait_for_turn(
                            base,
                            args.timeout,
                            meter,
                            turns_before,
                            args.turn_timeout_s,
                            args.poll_s,
                        )
                        latency = time.monotonic() - started
                        if not turn_ok:
                            kind = "no_turn"
                            note = (
                                f"the robot metered no turn within "
                                f"{args.turn_timeout_s:g}s -- it did not hear the clip"
                            )

            _log_line(
                log,
                {
                    "at": int(now),
                    "kind": kind,
                    "note": note,
                    "clip": utterance.path.name,
                    "text": utterance.text,
                    "turn_ok": turn_ok,
                    "latency_s": round(latency, 2),
                    "turns": meter.turns,
                    "cost_usd": round(meter.usd, 5),
                },
            )
            _say(
                f"  {time.strftime('%Y-%m-%d %H:%M:%S')}  {kind:<13} {utterance.key:<14} "
                f"turns={meter.turns} ${meter.usd:.4f}  {note if kind != 'played' else ''}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        _say("stopped")
    return 0


def _report(criteria: list[_Criterion]) -> int:
    """Print every criterion, then the verdict. Returns the exit code."""
    _say("\n" + "=" * 78)
    _say("soak load corpus — #389 / O5, SDS §12.6")
    _say("=" * 78)
    marks = {
        "pass": "PASS",
        "fail": "FAIL",
        "recorded": "····",
        "inconclusive": "INCONCL",
    }
    for criterion in criteria:
        _say(f"[{marks[criterion.verdict]}] {criterion.ac}  {criterion.name}")
        if criterion.detail:
            _say(f"        {criterion.detail}")
        for row in criterion.rows:
            _say(f"          - {row}")

    failed = [c for c in criteria if c.verdict == "fail"]
    inconclusive = [c for c in criteria if c.verdict == "inconclusive"]
    _say("-" * 78)
    _say(
        f"{len([c for c in criteria if c.verdict == 'pass'])} passed, {len(failed)} failed, "
        f"{len(inconclusive)} inconclusive."
    )
    if failed or inconclusive:
        _say(
            "\nA corpus that cannot reliably trip the gate produces a window that measures an idle "
            "robot, which is the outcome this whole tool exists to prevent. Fix the clips, not the "
            "thresholds."
        )
    return 1 if (failed or inconclusive) else 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("validate", "play"), required=True)
    parser.add_argument("--config", default="/etc/robot/config.toml")
    parser.add_argument("--corpus", default="/opt/avid/assets/load")
    parser.add_argument(
        "--load-log",
        default="/var/lib/soak/load.jsonl",
        help="what this generator actually played - one JSON object per line, appended. The "
        "soak's LOAD criterion reads it (S12.6).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--interval",
        type=float,
        default=900.0,
        help="seconds between utterances. 900 (4/hour) is ~$13 over 72 hours at the measured "
        "$0.045/turn.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=500,
        help="stop after this many turns. THIS IS THE ONLY CAP THAT EXISTS - S10.4's gate does "
        "not throttle reactive turns.",
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=15.0,
        help="stop at this much measured spend, read from /metrics cost_usd. Never from "
        "projected_monthly_usd, which models 20 turns/day and ignores the real rate.",
    )
    parser.add_argument(
        "--for-seconds",
        type=float,
        default=0.0,
        help="stop after N seconds (0 = until a cap or an operator). The dry-run lever.",
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=60.0,
        help="how long to wait for the robot to be settled (IDLE or SLEEPING) before playing. "
        "Never play over a reply: the clip is judged against the echo floor and discarded. "
        "SLEEPING counts - S3.10.3 wakes the robot on speech, and an empty room is exactly when "
        "an unattended window runs. Must allow at least one poll.",
    )
    parser.add_argument(
        "--turn-timeout-s",
        type=float,
        default=60.0,
        help="how long a turn may take before it is recorded as no_turn.",
    )
    parser.add_argument("--poll-s", type=float, default=1.0)
    parser.add_argument(
        "--ignore-quiet-hours",
        action="store_true",
        help="play through the configured quiet window too. Off by default: the policy gate "
        "applies quiet hours to PROACTIVE turns only, so nothing else would stop this at 3 a.m.",
    )
    args = parser.parse_args()

    if args.mode == "validate":
        return _report(_validate(args))
    return _run_load(args)


if __name__ == "__main__":
    raise SystemExit(main())
