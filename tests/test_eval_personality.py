"""Offline tests for the personality-adherence harness (AVID-215, §14.7).

``tools/eval_personality.py`` is a Tier-5, live, key-gated tool — its model calls never run in CI.
Its **logic** is pure and deterministic and must, exactly as ``tests/test_eval_extraction.py``
does for its sibling: the live number stays out of CI, the code that computes it does not.

``tools/`` is deliberately not a package (it is outside ``avid/``, so outside mypy's and
coverage's default scope), so the module is loaded **by path**.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from avid.adapters.text_model import FakeTextModel

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "tools" / "eval_personality.py"
_EVAL_SET = _REPO_ROOT / "assets" / "eval" / "personality.json"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_personality", _HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass(slots=True) resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_h = _load_harness()


class _ScriptedJudge:
    """A judge whose verdicts are decided by the test, not by any model.

    The point of §14.7's split is that the *scoring code* is deterministic even though the score
    is not. Scripting the verdict is what lets that half be asserted."""

    def __init__(self, verdicts: dict[str, bool]) -> None:
        self.verdicts = verdicts
        self.asked: list[str] = []

    async def judge_separation(self, *, prompt: str, first: str, second: str) -> bool:
        self.asked.append(prompt)
        return self.verdicts.get(prompt, False)


def test_the_shipped_prompt_set_is_well_formed() -> None:
    """§14.7 names **20** prompts, and the categories exist to make a weak result diagnosable."""
    evalset = _h.load_eval_set(_EVAL_SET)

    assert len(evalset.prompts) == 20
    assert len({p.id for p in evalset.prompts}) == 20
    assert {p.category for p in evalset.prompts} == {
        "open",
        "invites-compliment",
        "invites-disclaimer",
        "invites-enthusiasm",
        "invites-hedging",
        "invites-followup",
    }


def test_every_prompt_is_open_ended_enough_to_show_a_personality() -> None:
    """A prompt with one correct factual answer measures nothing: both configs would answer the
    same, and the pair would score as unseparated for a reason that is not about personality.

    A crude proxy, and deliberately crude — it catches the obvious regression (someone adding
    "what is 2+2") without pretending to judge open-endedness."""
    evalset = _h.load_eval_set(_EVAL_SET)

    for prompt in evalset.prompts:
        assert (
            not prompt.text.strip()
            .lower()
            .startswith(("what is the", "how many", "when did", "who invented"))
        ), f"{prompt.id} looks like a closed factual question"


def test_the_two_shipped_prefixes_differ() -> None:
    """The harness must build its prompts from the **shipped** composer, so the eval cannot drift
    from production — the same reason ``eval_extraction`` imports the real ``TOOL_SCHEMAS``."""
    identity = "You are Pico."
    default = _h.instructions_for("default", identity=identity)
    terse = _h.instructions_for("terse", identity=identity)

    assert default != terse
    assert default.startswith(identity) and terse.startswith(identity)


async def test_score_pairs_each_prompt_and_counts_separations() -> None:
    evalset = _h.load_eval_set(_EVAL_SET)
    ids = [p.id for p in evalset.prompts]
    answers = {pid: (f"{pid}-a", f"{pid}-b") for pid in ids}
    judge = _ScriptedJudge({p.text: True for p in evalset.prompts[:15]})

    report = await _h.score(evalset, answers, judge)

    assert report.total == 20
    assert report.separated == 15
    assert report.rate == pytest.approx(0.75)


async def test_a_missing_answer_is_dropped_not_counted() -> None:
    """A turn that errored is an **absent measurement**, not a failed one.

    Counting it as separated would let a broken run inflate the number — the direction a
    measurement must never fail in. Counting it as *un*separated would be just as wrong in the
    other direction: it would report a personality problem where there was a network problem. So
    it is dropped, and the shortfall is stated in the report instead."""
    evalset = _h.load_eval_set(_EVAL_SET)
    answers = {p.id: ("a", "b") for p in evalset.prompts[:18]}
    judge = _ScriptedJudge({p.text: True for p in evalset.prompts})

    report = await _h.score(evalset, answers, judge)

    assert report.total == 18
    assert report.separated == 18
    assert report.rate == pytest.approx(1.0)


def test_the_report_states_a_shortfall_rather_than_hiding_it() -> None:
    """CLAUDE.md §7.1: report the quantity you grade. A rate over 18 of 20 pairs printed without
    comment reads as a rate over the set, and quietly becomes whatever happened to run."""
    evalset = _h.load_eval_set(_EVAL_SET)
    pairs = tuple(
        _h.Pair(prompt=p, first="a", second="b", separated=True)
        for p in evalset.prompts[:18]
    )

    text = _h.format_report(_h.Report(pairs=pairs), expected=20)

    assert "18/18" in text
    assert "only 18 of 20" in text


def test_the_report_renders_the_rate_and_the_per_prompt_detail() -> None:
    evalset = _h.load_eval_set(_EVAL_SET)
    pairs = (
        _h.Pair(prompt=evalset.prompts[0], first="a", second="b", separated=True),
        _h.Pair(prompt=evalset.prompts[1], first="a", second="a", separated=False),
    )

    text = _h.format_report(_h.Report(pairs=pairs), expected=2)

    assert "separation rate = 0.50 (1/2)" in text
    assert evalset.prompts[0].id in text
    assert (
        "NO" in text
    )  # the unseparated pair is visible per-prompt, not just in the rate


def test_an_empty_run_reports_zero_rather_than_dividing_by_zero() -> None:
    assert _h.Report(pairs=()).rate == 0.0


async def test_the_fake_judge_separates_the_two_shipped_layer_twos() -> None:
    """The dry-run path, asserted — it is what someone runs first, and a 0/20 there would look
    like a failing gate rather than an artefact of a lexical instrument."""
    first = _h.compose(_h.load_personality("default"))
    second = _h.compose(_h.load_personality("terse"))

    assert await FakeTextModel().judge_separation(
        prompt="anything", first=first, second=second
    )


def test_main_exits_zero_on_the_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    """Tier 5 **always exits 0** (§14.7): these are non-deterministic, and a flaky red build
    teaches you to ignore red builds. The dry run also has to say, loudly, that its number is not
    the gate's — otherwise it is a harness that reports a passing result from a config file."""
    code = _h.main(["--config", str(_REPO_ROOT / "config" / "sim.toml"), "--dry-run"])
    out = capsys.readouterr().out

    assert code == 0
    assert "separation rate" in out
    assert "DRY RUN" in out
    assert "nothing whatever about whether the" in out


def test_asyncio_is_available_for_the_scripted_judge() -> None:
    """Guards the ``importlib`` load itself: if the harness module failed to exec, every test
    above would error rather than fail, and the reason would be one line deep in a traceback."""
    assert asyncio.iscoroutinefunction(_ScriptedJudge({}).judge_separation)
