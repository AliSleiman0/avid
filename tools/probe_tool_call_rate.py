#!/usr/bin/env python
"""Compare `remember_fact` and `set_affect` call rates in ONE live session (#310 AC-1).

#310's evidence is a zero: across six live runs at the M6 gate (~30 turns, real API, on the
rig) ``set_affect`` fired **zero times**, while the model engaged with the emotional content in
words. The wiring was verified — tool declared, enum right, capability clause present, and #309
had already reweighted that clause to lead with the action rather than the constraint. So Tier 2
is dead in practice: HAPPY/SAD/CONFUSED are unreachable and M9's ``HAPPY -> nod`` (#202) has
nothing to fire on.

SDS §6.8 pre-committed the contingency (local inference behind the existing ``AffectTools``
port), but #310 AC-1 puts a gate in front of it, and this script *is* that gate:

    "A single run comparing tool-call rates for remember_fact and set_affect in the same
    session separates 'this tool' from 'tools in speech-to-speech'."

Three candidate causes, and the 2x2 this run produces distinguishes the first from the others:

1. **the modality** — speech-to-speech models are reluctant to call tools mid-turn;
2. **the tool's description** — it reads as optional decoration beside three consequential
   memory tools. ⚠️ Note that ``set_affect``'s *description* still leads with a constraint
   ("Use it only when the tone genuinely changes; leave it alone for ordinary replies") even
   though #309 reweighted the *capability clause* to lead with the action — and §6.5's finding
   is that a negative constraint is followed far more strongly than an encouragement. So the
   suppression #309 removed from one string may still be living in the other;
3. **the enum** — three values, so a model feeling *concerned* declines rather than mis-files,
   which is the exact failure §6.8 predicted for local inference.

``--arm`` runs 2 and 3 as single-variable experiments. **Run baseline first**: if the model
calls no tools at all, cause 1 is live and the arms are pointless.

What this measures, and what it does not
----------------------------------------

**It drives the production path through the port.** The real ``OpenAIRealtimeClient`` is built
exactly as the composition root builds it (``avid/main.py``), with ``compose_instructions`` over
the config's identity + personality + the real ``CAPABILITY_INSTRUCTIONS``, and the real
``TOOL_SCHEMAS``. Nothing is re-declared here. A probe carrying its own hand-written tool schemas
would measure a robot that does not exist.

**It reads config rather than restating it** (CLAUDE.md §7.1) and defaults to ``config/pi.toml``
because that is the *deployed* model — the flagship ``gpt-realtime-2025-08-28``, which is what
#310's evidence was collected against. The model actually used is echoed into the report and the
evidence JSON from the loaded config, never typed into a banner; ``--model`` overrides it for a
single-variable arm and is announced against the value it replaced.

⚠️ **The first run answered AC-1, and the answer was "none of the three."** See
``docs/demos/m310_evidence/`` — ``set_affect`` fired on 4 of 4 turns written to invite it and on
0 of the 8 that were not, alongside ``remember_fact`` at 4 of 4, on **both** the flagship and the
mini. So the tool is not suppressed, the modality is not the obstacle and the enum is not too
narrow, and the ``wide-enum`` / ``strong-description`` arms below were left unrun because they
answer a question this result closed. What remains unexplained is the *difference from the rig*,
which is why this script records the whole session rather than a verdict: the leading suspect is
now no longer a prompt at all.

**The stimulus is real speech**, synthesized with Windows SAPI at 24 kHz mono 16-bit — the same
route ``tools/generate_cue_bank.py`` uses, offline and free. This matters because the hypothesis
under test *is* the audio modality: a text-injected turn would be a different stimulus and could
not falsify cause 1. The adapter resamples to the wire rate itself, so there is no DSP here.

**No hardware.** ``[ai.turn_detection] type = "none"`` (AVID-194) means the server commits
nothing on its own, so ``end_user_turn()`` is the only thing that ends a turn and this script has
deterministic control of the session with no mic, no speaker and no local VAD. Assistant audio is
counted and discarded.

**It cannot pass on silence** (the M4 lesson). A session in which no turn produced an assistant
transcript exits non-zero as INCONCLUSIVE — never as "set_affect never fired". A dead session and
a declining model are the same zero, and telling them apart is the entire job.

**n is 12 turns, 4 per class.** So the report says "fired on 3 of the 4 turns that invited it"
and never dresses that up as a percentage: at this n a rate is a fraction wearing a lab coat.

Usage::

    uv run --frozen --extra openai python tools/probe_tool_call_rate.py --dry-run
    uv run --frozen --extra openai python tools/probe_tool_call_rate.py
    uv run --frozen --extra openai python tools/probe_tool_call_rate.py --arm wide-enum

``--dry-run`` needs no key and no network: it composes the real instructions, prints the tool
schemas it would declare and renders the corpus, so the wiring can be audited before spending a
live session. A live run needs ``OPENAI_API_KEY`` in the environment and the ``openai`` extra
(``websockets`` is not in the default venv). The key is read once by ``load_config`` as a
``SecretStr`` and handed to the adapter only to build the auth header — never logged, never
echoed, never written to the evidence JSON (SECURITY.md).

A dev-only tool: not imported by the application, not run in CI, and outside ``avid/`` so it is
clear of mypy and coverage. ``ruff`` runs repo-wide, so it stays formatted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avid.adapters.realtime import OpenAIRealtimeClient  # noqa: E402
from avid.core.config import load_config  # noqa: E402
from avid.core.hal import AudioChunk  # noqa: E402
from avid.core.personality import compose, compose_instructions  # noqa: E402
from avid.core.realtime import (  # noqa: E402
    AssistantAudioChunk,
    AssistantTranscript,
    SessionClosed,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from avid.services.tools import (  # noqa: E402
    CAPABILITY_INSTRUCTIONS,
    REMEMBER_FACT,
    SET_AFFECT,
    TOOL_SCHEMAS,
)

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _REPO / "config" / "pi.toml"
_DEFAULT_SCRIPT = Path(__file__).resolve().parent / "affect_probe_script.json"
_DEFAULT_OUT = _REPO / "docs" / "demos" / "m310_evidence"

# SAPI's output format, and the format the AudioChunk is tagged with. The adapter resamples to
# the wire rate itself (`_resample_pcm16`), so this is the only rate this script knows.
_SAMPLE_RATE = 24_000
_VOICE = "Microsoft Zira Desktop"
# ~100 ms per append. Small enough to stay well inside any frame-size limit, large enough that a
# 6-second line is ~60 sends rather than ~1500.
_CHUNK_BYTES = _SAMPLE_RATE * 2 // 10
# A live flagship turn answers in a few seconds; 60 s is a hang, not a slow model.
_TURN_TIMEOUT_S = 60.0
# A tool call is answered, and the answer prompts a fresh response that may itself call a tool.
# Bounded so a model in a tool loop cannot spend the session (or the budget) going around.
_MAX_TOOL_ROUNDS = 4

_SAPI_SCRIPT = """
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($env:CUE_VOICE)
$s.Rate = -1
$s.SetOutputToWaveFile($env:CUE_OUT, $fmt)
$s.Speak($env:CUE_TEXT)
$s.Dispose()
"""


# ---------------------------------------------------------------------------- arms


def _arm_schemas(arm: str) -> tuple[dict[str, Any], ...]:
    """Return the tool declarations for *arm* — production's, or a one-variable variant.

    Every arm is a deep copy of the real ``TOOL_SCHEMAS`` with exactly one field changed, so a
    difference in the result has exactly one candidate explanation. Nothing in ``avid/`` is
    edited to run an experiment: a probe that mutates the robot to measure it is measuring the
    mutation.
    """
    schemas = copy.deepcopy(list(TOOL_SCHEMAS))
    if arm == "baseline":
        return tuple(schemas)

    affect = next(s for s in schemas if s["name"] == SET_AFFECT)
    if arm == "wide-enum":
        # Cause 3. ⚠️ These are NOT domain values — `SEMANTIC_AFFECTS` is (HAPPY, SAD, CONFUSED)
        # and the dispatcher would reject the rest. This arm asks one question only: does the
        # model decline because it has no honest option? A positive result here does not ship as
        # is; it argues for widening the domain enum, which is #310 AC-2's decision, not a
        # probe's. Per-value counts are reported so "fires, but only into the new values" is
        # visible rather than averaged away.
        affect["parameters"]["properties"]["affect"]["enum"] = [
            "happy",
            "sad",
            "confused",
            "concerned",
            "excited",
            "neutral",
        ]
        return tuple(schemas)
    if arm == "strong-description":
        # Cause 2. The shipped description LEADS WITH A CONSTRAINT ("Use it only when the tone
        # genuinely changes; leave it alone for ordinary replies") — which is precisely the
        # shape #309 removed from the capability clause, on §6.5's finding that a negative
        # constraint outweighs an encouragement. This arm removes it from the description too
        # and states the consequence of NOT calling, which is the thing the memory tools have
        # and this one does not: something visibly fails to happen.
        affect["description"] = (
            "Set the robot's facial expression. The user is looking at the robot's face while "
            "you speak; if you do not call this, the face stays on its default expression and "
            "does not match your words. Call it whenever your reply carries an emotional tone."
        )
        return tuple(schemas)
    raise ValueError(f"unknown arm {arm!r}")


# ---------------------------------------------------------------------------- speech


def _synth(text: str, path: Path) -> None:
    """Speak *text* into *path* at 24 kHz mono 16-bit via Windows SAPI."""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT],
        check=True,
        env={
            **os.environ,
            "CUE_TEXT": text,
            "CUE_OUT": str(path),
            "CUE_VOICE": _VOICE,
        },
    )


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != _SAMPLE_RATE or handle.getnchannels() != 1:
            raise RuntimeError(
                f"{path.name}: expected {_SAMPLE_RATE} Hz mono, got "
                f"{handle.getframerate()} Hz / {handle.getnchannels()} ch"
            )
        return handle.readframes(handle.getnframes())


# ---------------------------------------------------------------------------- session


@dataclass
class _Session:
    """Everything one live session observed, accumulated by the reader task."""

    turn_done: asyncio.Event = field(default_factory=asyncio.Event)
    assistant: list[str] = field(default_factory=list)
    user: list[str] = field(default_factory=list)
    calls: list[ToolCallRequested] = field(default_factory=list)
    pending: list[ToolCallRequested] = field(default_factory=list)
    audio_bytes: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    closed_cause: str | None = None

    def begin_turn(self) -> None:
        self.turn_done.clear()
        self.assistant.clear()
        self.user.clear()
        self.calls.clear()
        self.pending.clear()


async def _reader(client: OpenAIRealtimeClient, state: _Session) -> None:
    """Drain the port's neutral event stream into *state* until the session closes."""
    async for event in client.events():
        match event:
            case UserTranscript():
                state.user.append(event.text)
            case AssistantTranscript():
                state.assistant.append(event.text)
            case AssistantAudioChunk():
                state.audio_bytes += len(event.chunk.pcm)
            case ToolCallRequested():
                state.calls.append(event)
                state.pending.append(event)
            case TurnDone():
                state.input_tokens += event.usage.input_tokens
                state.cached_input_tokens += event.usage.cached_input_tokens
                state.output_tokens += event.usage.output_tokens
                state.turn_done.set()
            case SessionClosed():
                state.closed_cause = event.cause
                state.turn_done.set()
                return


async def _await_response(state: _Session, *, what: str) -> bool:
    """Wait for one ``response.done``. False on timeout — recorded, never silently retried."""
    try:
        await asyncio.wait_for(state.turn_done.wait(), timeout=_TURN_TIMEOUT_S)
    except TimeoutError:
        print(f"    ⚠️  no response within {_TURN_TIMEOUT_S:.0f}s ({what})")
        return False
    return True


async def _run_turn(
    client: OpenAIRealtimeClient, state: _Session, pcm: bytes
) -> dict[str, Any]:
    """Speak one utterance, answer any tool calls, and return what the turn produced."""
    state.begin_turn()
    started = time.monotonic()
    for offset in range(0, len(pcm), _CHUNK_BYTES):
        await client.send_audio(
            AudioChunk(
                pcm=pcm[offset : offset + _CHUNK_BYTES],
                sample_rate=_SAMPLE_RATE,
                channels=1,
            )
        )
    await client.end_user_turn()
    responded = await _await_response(state, what="first response")

    # §6.6 steps 4-5: return each result and let the adapter chase it with `response.create`
    # (it does that itself, and waits out #284's active-response race). Unanswered tool calls
    # leave the model sitting silently, and every later turn would then be measuring a wedged
    # session rather than a disinclined model — the confound this whole probe exists to avoid.
    rounds = 0
    while state.pending and rounds < _MAX_TOOL_ROUNDS and state.closed_cause is None:
        call = state.pending.pop(0)
        rounds += 1
        state.turn_done.clear()
        await client.send_tool_output(call.call_id, '{"ok": true}')
        responded = await _await_response(state, what=f"after {call.name}") or responded

    return {
        "heard": " ".join(state.user).strip(),
        "said": " ".join(state.assistant).strip(),
        "tools": [
            {"name": c.name, "arguments": c.arguments} for c in list(state.calls)
        ],
        "responded": responded,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------------------- report


def _report(run: dict[str, Any]) -> int:
    """Print every criterion, then the verdict. Returns the exit code.

    Order is deliberate (CLAUDE.md §7.1): every number is printed before any verdict is decided,
    so a conclusion can never hide a result that disagrees with it.
    """
    turns = run["turns"]
    classes = ("set_affect", "remember_fact", "neither")
    invited: Counter[str] = Counter(t["invites"] for t in turns)

    print("\n=== per turn ===")
    for turn in turns:
        names = [t["name"] for t in turn["tools"]] or ["-"]
        print(
            f"  {turn['id']:>2}. invites={turn['invites']:<13} tools={','.join(names)}"
        )
        print(f"      heard: {turn['heard'] or '(nothing transcribed)'}")
        print(f"      said : {turn['said'] or '(nothing said)'}")

    fired: dict[str, Counter[str]] = {
        name: Counter() for name in (SET_AFFECT, REMEMBER_FACT)
    }
    total_calls: Counter[str] = Counter()
    for turn in turns:
        for call in turn["tools"]:
            total_calls[call["name"]] += 1
            if call["name"] in fired:
                fired[call["name"]][turn["invites"]] += 1

    print("\n=== tool calls, by the class of turn that produced them ===")
    print(f"  {'tool':<16}" + "".join(f"{c:>16}" for c in classes) + f"{'total':>8}")
    for name in (REMEMBER_FACT, SET_AFFECT):
        cells = "".join(f"{fired[name][c]:>16}" for c in classes)
        print(f"  {name:<16}{cells}{total_calls[name]:>8}")
    other = {n: c for n, c in total_calls.items() if n not in fired}
    print("  denominator     " + "".join(f"{invited[c]:>16}" for c in classes))
    if other:
        print(f"  other tools called: {other}")

    silent = sum(1 for t in turns if not t["said"])
    heard_none = sum(1 for t in turns if not t["heard"])
    print("\n=== was this a live session at all? ===")
    print(f"  turns run                     : {len(turns)}")
    print(f"  turns with an assistant reply : {len(turns) - silent}")
    print(f"  turns the model transcribed   : {len(turns) - heard_none}")
    print(f"  assistant audio received      : {run['audio_bytes'] / 1000:.0f} kB")
    print(
        f"  tokens in/cached/out          : {run['input_tokens']}/"
        f"{run['cached_input_tokens']}/{run['output_tokens']}"
    )
    if run["closed_cause"]:
        print(f"  ⚠️  session closed early       : {run['closed_cause']}")

    remember = total_calls[REMEMBER_FACT]
    affect = total_calls[SET_AFFECT]
    print("\n=== AC-1 ===")
    print(f"  arm   : {run['arm']}")
    overridden = run["model"] != run["config_model"]
    print(
        f"  model : {run['model']}"
        + (
            f"  (--model override; {run['config']} says {run['config_model']})"
            if overridden
            else f"  (from {run['config']})"
        )
    )
    print(
        f"  set_affect fired on {fired[SET_AFFECT]['set_affect']} of the "
        f"{invited['set_affect']} turns written to invite it; "
        f"remember_fact on {fired[REMEMBER_FACT]['remember_fact']} of {invited['remember_fact']}."
    )

    # A dead session and a declining model are the same zero. This is the M4 lesson, and it is
    # the one verdict that must never be rounded up.
    if len(turns) - silent == 0:
        print(
            "\n  INCONCLUSIVE — not one turn produced an assistant reply. This run measured a "
            "broken session, not a reluctant model, and proves nothing about either tool."
        )
        return 2

    if remember == 0 and affect == 0:
        print(
            "\n  CAUSE 1 (the modality) — the model called NO tools at all in a live "
            "speech-to-speech session that was otherwise answering normally. set_affect is not "
            "special; tool use is. The arms would tell us nothing, and §6.8's local inference is "
            "the right answer for the affect half."
        )
    elif remember > 0 and affect == 0:
        print(
            "\n  NOT THE MODALITY — remember_fact fired in the same session, on the same audio "
            "path, under the same instructions, and set_affect did not. Cause 1 is ruled out; it "
            "is this tool. Run --arm wide-enum and --arm strong-description to separate cause 3 "
            "from cause 2 before writing any inference code (AC-2)."
        )
    elif affect > 0:
        print(
            "\n  set_affect DID fire under this configuration. The M6 observation does not "
            "reproduce here, so the difference is in what changed — model, corpus, session "
            "length or arm — and that, not local inference, is the next thing to chase. Do not "
            "close #310 on this alone: AC-4 grades it live, with a person."
        )
    return 0


# ---------------------------------------------------------------------------- main


async def _run(
    args: argparse.Namespace, corpus: list[dict[str, Any]]
) -> dict[str, Any]:
    config = load_config(args.config)
    if config.openai_api_key is None:
        raise SystemExit(
            "OPENAI_API_KEY is not in the environment — a live run needs it (SECURITY.md: read "
            "once, as SecretStr, by load_config). Use --dry-run to audit the wiring without one."
        )
    schemas = _arm_schemas(args.arm)
    model = args.model or config.ai.model
    client = OpenAIRealtimeClient(
        api_key=config.openai_api_key.get_secret_value(),
        model=model,
        voice=config.ai.voice,
        instructions=compose_instructions(
            identity=config.ai.instructions,
            personality=compose(config.personality),
            capabilities=CAPABILITY_INSTRUCTIONS,
        ),
        max_output_tokens=config.ai.max_output_tokens,
        turn_detection=config.ai.turn_detection.model_dump(),
        transcription_model=config.ai.transcription_model,
        transcription_language=config.ai.transcription_language,
        tools=schemas,
    )

    print(f"model {model} · arm {args.arm} · {len(corpus)} turns")
    if args.model:
        print(
            f"  ⚠️  --model overrides [ai] model = {config.ai.model!r} from {args.config}"
        )
    if config.ai.turn_detection.type != "none":
        print(
            f"  ⚠️  [ai.turn_detection] type = {config.ai.turn_detection.type!r}, not 'none'. "
            "The server VAD may commit inside an utterance and answer a fragment; turns below "
            "are not cleanly controlled by this script."
        )

    state = _Session()
    turns: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="avid-310-") as tmp:
        wavs: list[tuple[dict[str, Any], bytes]] = []
        for item in corpus:
            path = Path(tmp) / f"u{item['id']}.wav"
            _synth(item["say"], path)
            wavs.append((item, _read_pcm(path)))

        await client.open()
        reader = asyncio.create_task(_reader(client, state))
        try:
            for item, pcm in wavs:
                secs = len(pcm) / (_SAMPLE_RATE * 2)
                print(f"\n[{item['id']:>2}/{len(corpus)}] ({secs:.1f}s) {item['say']}")
                result = await _run_turn(client, state, pcm)
                names = [t["name"] for t in result["tools"]] or ["-"]
                print(f"    -> {result['elapsed_s']}s  tools: {','.join(names)}")
                print(f"    -> {result['said'][:160] or '(silence)'}")
                turns.append({**item, **result})
                if state.closed_cause is not None:
                    print(
                        f"    ⚠️  session closed ({state.closed_cause}) — stopping early"
                    )
                    break
        finally:
            await client.aclose()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    return {
        "issue": 310,
        "criterion": "AC-1",
        "arm": args.arm,
        "config": str(args.config),
        # Echoed from the loaded config, never typed here — a literal in a report is drift with
        # a delay fuse (CLAUDE.md §7.1).
        "model": model,
        "config_model": config.ai.model,
        "voice": config.ai.voice,
        "transcription_model": config.ai.transcription_model,
        "turn_detection": config.ai.turn_detection.type,
        "corpus": str(args.script),
        "turns": turns,
        "audio_bytes": state.audio_bytes,
        "input_tokens": state.input_tokens,
        "cached_input_tokens": state.cached_input_tokens,
        "output_tokens": state.output_tokens,
        "closed_cause": state.closed_cause,
    }


def _dry_run(args: argparse.Namespace, corpus: list[dict[str, Any]]) -> int:
    """Audit the wiring with no key and no network: what would this session declare?"""
    config = load_config(args.config)
    instructions = compose_instructions(
        identity=config.ai.instructions,
        personality=compose(config.personality),
        capabilities=CAPABILITY_INSTRUCTIONS,
    )
    schemas = _arm_schemas(args.arm)
    print(f"config              : {args.config}")
    print(f"model               : {args.model or config.ai.model}")
    if args.model:
        print(f"  (override; {args.config} says {config.ai.model})")
    print(f"voice               : {config.ai.voice}")
    print(f"turn_detection.type : {config.ai.turn_detection.type}")
    print(f"arm                 : {args.arm}")
    print(f"\ninstructions ({len(instructions)} chars):\n{instructions}")
    print(f"\ntools declared      : {[s['name'] for s in schemas]}")
    affect = next(s for s in schemas if s["name"] == SET_AFFECT)
    print(
        f"set_affect enum     : {affect['parameters']['properties']['affect']['enum']}"
    )
    print(f"set_affect descr    : {affect['description']}")
    print(f"\ncorpus ({len(corpus)} utterances):")
    for item in corpus:
        print(f"  {item['id']:>2}. [{item['invites']:<13}] {item['say']}")
    print("\nDry run: nothing was sent and no key was needed.")
    return 0


def main() -> int:
    # A Windows console defaults to cp1252, which cannot encode "⚠️" — so every warning path in
    # this script (model override, server VAD on, session closed early, response timeout) raised
    # UnicodeEncodeError and killed the run *at the moment it had something to tell us*. Found by
    # running the first arm that trips one. The transcripts need this too: the model's em dashes
    # and curly quotes came back as "�" in the console, which is only cosmetic here but is the
    # same class of bug as a report that misdescribes its run.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="the config whose [ai] section is probed (default: the DEPLOYED config/pi.toml)",
    )
    parser.add_argument("--script", type=Path, default=_DEFAULT_SCRIPT)
    parser.add_argument(
        "--model",
        default=None,
        # An experimental knob, not a second source of truth: the config still supplies every
        # other field, the override is announced against the value it replaced, and the report
        # and evidence JSON carry BOTH — the model actually used and what the config said. The
        # whole point of the flag is single-variable arms (flagship vs mini on one corpus);
        # pointing it at a config with its own [ai] section would vary several things at once.
        help="probe this model instead of the config's [ai] model — for single-variable arms",
    )
    parser.add_argument(
        "--arm",
        choices=("baseline", "wide-enum", "strong-description"),
        default="baseline",
        help="baseline first — the arms are meaningless if no tool fires at all",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help=f"write the evidence JSON here (default: {_DEFAULT_OUT}/<arm>.json)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    corpus = json.loads(args.script.read_text(encoding="utf-8"))["utterances"]

    if args.dry_run:
        return _dry_run(args, corpus)

    if sys.platform != "win32":
        print(
            "The stimulus is synthesized with Windows SAPI, so a live run is Windows-only. "
            "Porting means picking another TTS — not injecting text, which would be a different "
            "stimulus and could not falsify cause 1."
        )
        return 1

    run = asyncio.run(_run(args, corpus))
    # A model override is a different experiment, so it gets a different file. Overwriting
    # baseline.json with a mini run would leave the evidence directory quietly lying about
    # which model produced the numbers in it.
    stem = args.arm + (f"--{args.model}" if args.model else "")
    out = args.json or (_DEFAULT_OUT / f"{stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    code = _report(run)
    print(f"\nwrote {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
