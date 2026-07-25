"""Extraction eval harness — remember_fact fire-rate over 30 transcripts (#125, SDS §14.7, O2).

A **Tier-5** tool (§14.7): scored and tracked over time, **never pass/fail**, because extraction
quality is non-deterministic and *"a flaky red build teaches you to ignore red builds."* So this
**always exits 0** — the number is the product, not a gate. It is invoked explicitly
(``python tools/eval_extraction.py``), never collected by the default ``pytest`` run, and lives
outside ``avid/`` so it is clear of mypy/coverage default scope, like ``tools/eval_recall.py``.

Unlike retrieval, extraction is **instruction-following**, so scoring it needs a *real model call* —
you cannot re-derive "would the model have called ``remember_fact``?" offline. This harness therefore
opens a **live** Realtime session seeded with the **shipped** ``TOOL_SCHEMAS`` +
``CAPABILITY_INSTRUCTIONS`` (imported from :mod:`avid.services.tools`, so the eval can never drift
from what production ships), drives each transcript as a text-only turn, and records whether a
``remember_fact`` call fired. It is **network- and key-gated** (``OPENAI_API_KEY``): with no key it
prints a skip notice and exits 0, so it is safe to invoke anywhere.

**Vendor-boundary note.** The shipped ``avid/`` package stays vendor-sealed (R-10) — only
``OpenAIRealtimeClient`` touches the Realtime wire protocol. This *eval tool* speaks it directly (a
local ``import websockets``, vendor JSON), a deliberate, documented exception for a standalone
nightly script under ``tools/`` (the same category as a research harness); it is not imported by
``avid/`` and reuses the shipped schemas/instructions rather than re-declaring them, which is the
drift-safety that actually matters. The pure pieces here — loading, the fire/expect predicate, the
report — are unit-tested offline in ``tests/test_eval_extraction.py``; the live call is not.

Run:  ``OPENAI_API_KEY=… python tools/eval_extraction.py``  (optionally ``--path …`` / ``--model …``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avid.services.tools import (
    CAPABILITY_INSTRUCTIONS,
    REMEMBER_FACT,
    TOOL_SCHEMAS,
)

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "eval" / "extraction.json"
)
# The pinned Realtime snapshot the robot ships with (SDS §6.10 / [ai] model). A rolled snapshot is
# a config edit here, exactly as it is in the app (§6.10 volatility) — never a code change.
_DEFAULT_MODEL = "gpt-realtime-mini-2025-12-15"
_REALTIME_URL = "wss://api.openai.com/v1/realtime"


@dataclass(frozen=True, slots=True)
class EvalTranscript:
    id: str
    transcript: str
    expect_fire: bool
    expect_kind: str | None
    category: str


@dataclass(frozen=True, slots=True)
class EvalSet:
    transcripts: tuple[EvalTranscript, ...]


@dataclass(frozen=True, slots=True)
class Report:
    hits: int
    total: int
    per_category: dict[str, tuple[int, int]]  # category -> (hits, total)

    @property
    def accuracy(self) -> float:
        return self.hits / self.total if self.total else 0.0


def load_eval_set(path: Path) -> EvalSet:
    """Parse the JSON eval set into typed values. The ``_meta`` block is documentation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    transcripts = tuple(
        EvalTranscript(
            id=t["id"],
            transcript=t["transcript"],
            expect_fire=t["expect_fire"],
            expect_kind=t["expect_kind"],
            category=t["category"],
        )
        for t in raw["transcripts"]
    )
    return EvalSet(transcripts=transcripts)


def is_hit(expect_fire: bool, fired: bool) -> bool:
    """An item hits when the model's ``remember_fact`` firing matches what the transcript expects:
    a positive that fired, or a negative that stayed quiet."""
    return fired == expect_fire


def is_remember_fact_call(message: dict[str, Any]) -> bool:
    """Whether one parsed Realtime server message is a finalized ``remember_fact`` tool call.

    The model finalizes a tool call on ``response.output_item.done`` with an item of type
    ``function_call`` (SDS §6.6) — the same frame ``OpenAIRealtimeClient._translate`` maps. Pure, so
    the fire-detection logic is unit-tested with canned frames, no socket."""
    if message.get("type") != "response.output_item.done":
        return False
    item = message.get("item") or {}
    return item.get("type") == "function_call" and item.get("name") == REMEMBER_FACT


def score(evalset: EvalSet, fired: dict[str, bool]) -> Report:
    """Score the run: overall accuracy plus a per-category breakdown. ``fired`` maps transcript id →
    whether ``remember_fact`` fired. A missing id counts as "did not fire" (a live turn that errored)."""
    hits = 0
    cat_hits: Counter[str] = Counter()
    cat_total: Counter[str] = Counter()
    for t in evalset.transcripts:
        cat_total[t.category] += 1
        if is_hit(t.expect_fire, fired.get(t.id, False)):
            hits += 1
            cat_hits[t.category] += 1
    per_category = {c: (cat_hits[c], cat_total[c]) for c in sorted(cat_total)}
    return Report(hits=hits, total=len(evalset.transcripts), per_category=per_category)


def format_report(report: Report) -> str:
    """Render overall accuracy and the per-category table for the console."""
    lines = [
        f"remember_fact accuracy = {report.accuracy:.2f} ({report.hits}/{report.total})",
        "",
        f"{'category':<20}accuracy",
    ]
    for cat, (hits, total) in report.per_category.items():
        rate = hits / total if total else 0.0
        lines.append(f"{cat:<20}{rate:.2f} ({hits}/{total})")
    return "\n".join(lines)


def _session_update() -> dict[str, Any]:
    """The ``session.update`` payload seeding the shipped tools + §7.6 capability instructions.

    Text-only: extraction is about *whether the model decides to call the tool*, so the eval drives
    text turns rather than synthesizing speech — the instruction-following surface is identical."""
    return {
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "instructions": CAPABILITY_INSTRUCTIONS,
            "tools": list(TOOL_SCHEMAS),
            "tool_choice": "auto",
        },
    }


def _user_turn(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two client events that drive one text turn: create the user message, then ask for a
    response. Mirrors the §6.6 flow without audio."""
    create = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }
    return create, {"type": "response.create"}


async def _run_transcript(  # pragma: no cover - live network, exercised only at the M7 gate
    ws: Any, item: EvalTranscript
) -> bool:
    """Drive one transcript through the live session and return whether ``remember_fact`` fired.

    Sends the user text + ``response.create``, then reads server frames until ``response.done``,
    watching for a finalized ``remember_fact`` call. Conservative: any transport error counts as
    "did not fire", so a flake never inflates the score."""
    create, respond = _user_turn(item.transcript)
    await ws.send(json.dumps(create))
    await ws.send(json.dumps(respond))
    fired = False
    async for raw in ws:
        message = json.loads(raw)
        if is_remember_fact_call(message):
            fired = True
        if message.get("type") == "response.done":
            break
    return fired


async def _run_live(  # pragma: no cover - live network, exercised only at the M7 gate
    evalset: EvalSet, *, api_key: str, model: str
) -> dict[str, bool]:
    """Open one live Realtime session, seed the shipped tools/instructions, and run every transcript.

    The lazy ``websockets`` import keeps this the only place the eval tool touches the vendor wire —
    ``avid/`` stays sealed. One cold session for the whole set (§6.2.2): the tools/instructions are
    the cached prefix, unchanged across turns."""
    import websockets

    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    fired: dict[str, bool] = {}
    async with websockets.connect(
        f"{_REALTIME_URL}?model={model}", additional_headers=headers
    ) as ws:
        await ws.send(json.dumps(_session_update()))
        for item in evalset.transcripts:
            try:
                fired[item.id] = await _run_transcript(ws, item)
            except Exception as exc:  # noqa: BLE001 - a flake must not sink the run
                print(f"  ! {item.id}: turn errored ({exc}); counting as no-fire")
                fired[item.id] = False
    return fired


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_extraction",
        description=(
            "Score remember_fact fire-rate over the M7 extraction eval set (SDS §14.7, O2). "
            "Tier 5: tracked over time, never pass/fail — always exits 0. Needs OPENAI_API_KEY."
        ),
    )
    parser.add_argument(
        "--path", type=Path, default=_DEFAULT_PATH, help="eval JSON file"
    )
    parser.add_argument(
        "--model", default=_DEFAULT_MODEL, help="Realtime model snapshot"
    )
    args = parser.parse_args(argv)

    evalset = load_eval_set(args.path)
    print(f"eval set: {len(evalset.transcripts)} transcripts")

    api_key = os.environ.get("OPENAI_API_KEY")
    if (
        not api_key
    ):  # pragma: no cover - trivial guard, but stated so the tool is safe anywhere
        print("OPENAI_API_KEY not set — skipping the live run (Tier 5, needs a key).")
        return 0

    fired = asyncio.run(_run_live(evalset, api_key=api_key, model=args.model))
    report = score(evalset, fired)
    print(f"model: {args.model}")
    print()
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
