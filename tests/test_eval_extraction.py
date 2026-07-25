"""Offline unit for the extraction eval harness's pure pieces (#125, SDS §14.7).

The harness (``tools/eval_extraction.py``) is a Tier-5, live, network-gated tool — its model call
never runs in CI. But its **logic** (loading the eval set, the fire/expect predicate, the tool-call
detector, scoring, the report) is pure and deterministic, so it is unit-tested here exactly as
``eval_recall``'s scorer is — the same principle: the live number is out of CI, the code that
computes it is not. ``tools/`` is deliberately not a package (it stays clear of mypy/coverage default
scope), so the module is loaded by path rather than imported."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "tools" / "eval_extraction.py"
_EVAL_SET = _REPO_ROOT / "assets" / "eval" / "extraction.json"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_extraction", _HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass(slots=True) resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_h = _load_harness()


def test_the_shipped_eval_set_is_well_formed() -> None:
    evalset = _h.load_eval_set(_EVAL_SET)
    assert len(evalset.transcripts) == 30  # §14.7: 30 transcripts
    # positives name an expected kind; negatives name none — the shape the categories promise.
    for t in evalset.transcripts:
        if t.expect_fire:
            assert t.expect_kind is not None
        else:
            assert t.expect_kind is None
    categories = {t.category for t in evalset.transcripts}
    assert categories == {
        "positive",
        "negative-remark",
        "negative-question",
        "negative-inferred",
    }


@pytest.mark.parametrize(
    ("expect_fire", "fired", "hit"),
    [
        (True, True, True),
        (False, False, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_is_hit_matches_firing_to_expectation(
    expect_fire: bool, fired: bool, hit: bool
) -> None:
    assert _h.is_hit(expect_fire, fired) is hit


def test_is_remember_fact_call_detects_the_finalized_tool_call() -> None:
    # the §6.6 finalize frame for a remember_fact call
    fire = {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "name": "remember_fact", "call_id": "c0"},
    }
    assert _h.is_remember_fact_call(fire) is True


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "name": "recall"},
        },
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.output_item.done"},
        {"type": "response.done"},
        {},
    ],
)
def test_is_remember_fact_call_ignores_everything_else(
    message: dict[str, object],
) -> None:
    assert _h.is_remember_fact_call(message) is False


def test_score_counts_hits_overall_and_per_category() -> None:
    evalset = _h.load_eval_set(_EVAL_SET)
    # A perfect run: every positive fired, every negative stayed quiet.
    fired = {t.id: t.expect_fire for t in evalset.transcripts}
    report = _h.score(evalset, fired)
    assert report.hits == report.total == 30
    assert report.accuracy == 1.0
    assert all(hits == total for hits, total in report.per_category.values())

    # A missing id counts as no-fire, so a positive that never returned is a miss.
    a_positive = next(t for t in evalset.transcripts if t.expect_fire)
    partial = {
        t.id: t.expect_fire for t in evalset.transcripts if t.id != a_positive.id
    }
    assert _h.score(evalset, partial).hits == 29


def test_format_report_renders_accuracy_and_categories() -> None:
    evalset = _h.load_eval_set(_EVAL_SET)
    report = _h.score(evalset, {t.id: t.expect_fire for t in evalset.transcripts})
    text = _h.format_report(report)
    assert "accuracy" in text
    assert "negative-question" in text
