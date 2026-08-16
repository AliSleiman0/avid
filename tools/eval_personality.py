#!/usr/bin/env python
"""Personality-adherence eval — 20 prompts × 2 configs, LLM-as-judge scores separation (AVID-215).

**This script is the M6 gate criterion.** §6.5 says so outright:

    The M6 gate — "same question, two personality configs, recognisably different responses" — is
    an **eval-suite test (§14.7), not a vibe check.**

And §14.7 names the suite: *"Same 20 prompts × 2 configs → LLM-as-judge scores separation"*.

Tier 5 (§14.7): **nightly, needs a key, never blocking.** These are non-deterministic and a flaky
red build teaches you to ignore red builds — so this lives in ``tools/``, is never collected by
``pytest``, and **always exits 0**. The number is the product, never a verdict.

⚠️ **It scores SEPARATION, never quality.** The question put to the judge is *"were these two
answers produced under different personalities?"*, not *"which is better"*. Judging quality would
make the metric a taste report that drifts with the judge model; separation is the gate's actual
claim. See :meth:`~avid.core.ports.TextModel.judge_separation`.

⚠️ **No threshold is buried in here.** §14.7: *"scored and tracked over time rather than
pass/fail."* This prints the rate and the per-prompt detail; a human reads the number and judges
it. A harness that quietly decided whether a milestone shipped would be the "gate that can pass on
silence" failure wearing a percentage sign.

    # off-Pi, offline — proves the wiring with the fake judge and no key
    uv run --frozen python tools/eval_personality.py --config config/sim.toml --dry-run

    # the real thing, needs OPENAI_API_KEY
    uv run --frozen python tools/eval_personality.py --config config/sim.toml

**Vendor-boundary note.** Responses are generated over the Realtime websocket, exactly as
``tools/eval_extraction.py`` does and for the same reason: the eval must exercise the *shipped*
instruction assembly, so it imports ``compose``/``compose_instructions`` and the real
``CAPABILITY_INSTRUCTIONS`` rather than re-deriving a prompt that could drift from production. The
**judge**, by contrast, runs behind the ``TextModel`` port (§14.7 / AC-4) — no second vendor
surface, and the fake makes this file's own logic testable offline (``tests/test_eval_personality.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avid.adapters.text_model import FakeTextModel, OpenAiTextModel
from avid.core.config import PersonalityConfig, load_config
from avid.core.personality import compose, compose_instructions
from avid.core.ports import TextModel
from avid.services.tools import CAPABILITY_INSTRUCTIONS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "assets" / "eval" / "personality.json"
_PERSONALITY_DIR = _REPO_ROOT / "config" / "personality"
_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# The two shipped configs the gate names (AVID-211). Not a CLI knob: they are the artefacts the
# milestone is graded on, and a run against some other pair would not be the gate's measurement.
_CONFIGS = ("default", "terse")


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    text: str
    category: str


@dataclass(frozen=True, slots=True)
class EvalSet:
    prompts: tuple[Prompt, ...]


@dataclass(frozen=True, slots=True)
class Pair:
    """One prompt's answers under both configs, and the judge's verdict."""

    prompt: Prompt
    first: str
    second: str
    separated: bool


@dataclass(frozen=True, slots=True)
class Report:
    pairs: tuple[Pair, ...]

    @property
    def separated(self) -> int:
        return sum(1 for pair in self.pairs if pair.separated)

    @property
    def total(self) -> int:
        return len(self.pairs)

    @property
    def rate(self) -> float:
        """Separation rate. ``0.0`` for an empty run — never a division that raises mid-report."""
        return self.separated / self.total if self.pairs else 0.0


def load_eval_set(path: Path) -> EvalSet:
    """Read the prompt set. The ``_meta`` block is documentation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return EvalSet(
        prompts=tuple(
            Prompt(id=p["id"], text=p["text"], category=p["category"])
            for p in raw["prompts"]
        )
    )


def load_personality(name: str) -> PersonalityConfig:
    """Load one shipped personality by name (AVID-211's artefacts, not a fixture)."""
    with (_PERSONALITY_DIR / f"{name}.toml").open("rb") as handle:
        return PersonalityConfig.model_validate(tomllib.load(handle))


def instructions_for(name: str, *, identity: str) -> str:
    """The §6.4 static prefix a session would carry under personality *name*.

    Built with the **shipped** composer and the **shipped** capability text, so the eval measures
    the prompt the robot actually sends. Layer 4 is absent on purpose: memory is per-session and
    per-user, and injecting it would make two runs incomparable."""
    return compose_instructions(
        identity=identity,
        personality=compose(load_personality(name)),
        capabilities=CAPABILITY_INSTRUCTIONS,
    )


async def score(
    evalset: EvalSet,
    answers: dict[str, tuple[str, str]],
    judge: TextModel,
) -> Report:
    """Pair each prompt's two answers and ask the judge whether they are separated.

    A prompt missing from *answers* is **dropped**, not counted as separated: a turn that errored
    is an absent measurement, and letting it score would let a broken run inflate the number. It
    is reported as a shortfall in the header instead."""
    pairs: list[Pair] = []
    for prompt in evalset.prompts:
        pair = answers.get(prompt.id)
        if pair is None:
            continue
        first, second = pair
        separated = await judge.judge_separation(
            prompt=prompt.text, first=first, second=second
        )
        pairs.append(
            Pair(prompt=prompt, first=first, second=second, separated=separated)
        )
    return Report(pairs=tuple(pairs))


def format_report(report: Report, *, expected: int) -> str:
    """Render the tracked number, its provenance line being the caller's job (AC-7)."""
    lines = [
        f"separation rate = {report.rate:.2f} ({report.separated}/{report.total})",
    ]
    if report.total < expected:
        lines.append(
            f"⚠️ only {report.total} of {expected} prompts produced a pair — "
            f"the rate above describes the pairs that ran, not the set"
        )
    lines.append("")
    lines.append(f"{'id':<5}{'category':<22}{'separated'}")
    for pair in report.pairs:
        mark = "yes" if pair.separated else "NO"
        lines.append(f"{pair.prompt.id:<5}{pair.prompt.category:<22}{mark}")
    return "\n".join(lines)


def _session_update(instructions: str, *, voice: str) -> dict[str, Any]:
    """A minimal ``session.update`` carrying the composed instructions and no tools.

    Tools are omitted deliberately: this measures *personality*, and a run where one config
    happened to call ``recall`` would be comparing a tool round trip against a reply."""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["text"],
            "audio": {"input": {"turn_detection": None}},
        },
    }


def _user_turn(text: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {"type": "response.create"},
    ]


async def _answers_for(
    evalset: EvalSet, *, api_key: str, model: str, instructions: str
) -> dict[str, str]:  # pragma: no cover - live network, exercised only at the M6 gate
    """Run every prompt through one personality in a single session (cached prefix, §6.2.2)."""
    import websockets

    collected: dict[str, str] = {}
    ssl_context = ssl.create_default_context()
    async with websockets.connect(
        f"{_REALTIME_URL}?model={model}",
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ssl=ssl_context,
    ) as socket:
        await socket.send(json.dumps(_session_update(instructions, voice="cedar")))
        for prompt in evalset.prompts:
            try:
                for frame in _user_turn(prompt.text):
                    await socket.send(json.dumps(frame))
                collected[prompt.id] = await _read_reply(socket)
            except Exception as exc:  # noqa: BLE001 - a bench tool reports, never crashes
                print(f"! {prompt.id}: turn errored ({exc}); dropped", file=sys.stderr)
    return collected


async def _read_reply(
    socket: Any,
) -> str:  # pragma: no cover - live network
    """Accumulate one response's text until ``response.done``."""
    text = ""
    async for raw in socket:
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "response.output_text.delta":
            text += str(message.get("delta", ""))
        elif kind == "response.done":
            return text.strip()
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_personality",
        description=(
            "Score how recognisably two personality configs differ (§14.7's M6 row). "
            "Tier 5: tracked over time, never pass/fail — always exits 0. Needs OPENAI_API_KEY."
        ),
    )
    parser.add_argument("--path", type=Path, default=_DEFAULT_PATH, help="prompt set")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="profile supplying the identity layer and the judge model (e.g. config/sim.toml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "skip the live generation and score the COMPOSED PROMPTS against each other with "
            "the fake judge — proves the wiring, measures nothing about the model"
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    evalset = load_eval_set(args.path)
    config = load_config(args.config)
    prefixes = {
        name: instructions_for(name, identity=config.ai.instructions)
        for name in _CONFIGS
    }

    print(f"{args.path} — {len(evalset.prompts)} prompts × {len(_CONFIGS)} configs")
    print(f"configs: {', '.join(_CONFIGS)}")

    judge: TextModel
    api_key = os.environ.get("OPENAI_API_KEY")
    if args.dry_run or not api_key:
        if not args.dry_run:
            print(
                "OPENAI_API_KEY not set — falling back to --dry-run (Tier 5 needs a key)."
            )
        judge = FakeTextModel()
        # Every prompt "answered" with the config's own **layer 2**, not its whole prefix.
        # Layers 1 and 3 are identical across configs by construction (that is the point of the
        # cached prefix), so feeding the full prefix to a lexical judge would drown the only part
        # that differs and report 0/20 — a number that looks like a failing gate and is really an
        # artefact of the instrument. Compare what actually varies.
        layer_two = {name: compose(load_personality(name)) for name in _CONFIGS}
        answers = {
            p.id: (layer_two[_CONFIGS[0]], layer_two[_CONFIGS[1]])
            for p in evalset.prompts
        }
        print("judge: FakeTextModel (lexical overlap, on layer 2 only)")
    else:
        judge = OpenAiTextModel(api_key=api_key, model=config.ai.text_model)
        first = await _answers_for(
            evalset,
            api_key=api_key,
            model=config.ai.model,
            instructions=prefixes[_CONFIGS[0]],
        )
        second = await _answers_for(
            evalset,
            api_key=api_key,
            model=config.ai.model,
            instructions=prefixes[_CONFIGS[1]],
        )
        answers = {
            pid: (first[pid], second[pid]) for pid in first.keys() & second.keys()
        }
        print(f"responses: {config.ai.model}")
        print(f"judge: {config.ai.text_model}")

    report = await score(evalset, answers, judge)

    # Provenance on every run (AC-7): a tracked metric with no provenance is not tracked, and a
    # score read three months from now has to say what produced it.
    print(f"date: {datetime.now(UTC).date().isoformat()}")
    print()
    print(format_report(report, expected=len(evalset.prompts)))
    print()
    if args.dry_run or not api_key:
        print(
            "⚠️ DRY RUN: the number above compares the two COMPOSED PROMPTS with a lexical\n"
            "   judge. It says the wiring works. It says nothing whatever about whether the\n"
            "   MODEL's replies differ, which is the only thing the M6 gate claims."
        )
    else:
        print(
            "Tier 5: the number is the product, never a verdict. Record it in the issue and\n"
            "docs/journal.md with the date and both model names, and compare against the\n"
            "previous run rather than against a threshold."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
