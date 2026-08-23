"""Guard the M7 gate's fact matcher, which decides the one number O2 is graded on (AVID-450).

`docs/demos/memory_pi.py`'s `_matches` answers *"is this stored row the fact we declared?"*, and
its answer becomes AC-2's recall rate. It has to hold two properties at once, pulling opposite
ways — `facts.json`'s own `_readme` states them:

> `'match'` is every lowercase substring that must appear in a stored row for it to count as this
> fact — **deliberately loose on phrasing** (the model stores the user's own words, §7.6) and
> **strict on content**.

⚠️ **It was loose in the wrong place and strict in the wrong place, and it shipped that way through
a milestone seal.** The model stored *"Ali's team stand-up is at 9 AM every weekday"*; the declared
needle was `"standup"`; `_norm` lowercased and collapsed whitespace but left punctuation alone, so
a correct fact scored as a **miss**. The `v0.M7.0` tag recorded it in a parenthesis — *"standup is
a scoring artifact"* — and nobody filed it, so it stayed in the instrument.

The reason it matters more than a hyphen: `_probe` counts a fact it cannot find as a recall miss
*"per AC-2"*. So a matcher false-negative, an extraction failure and a retrieval failure produce
the **identical** row, `missed 'standup'`. Three defects, one symptom, in the number a milestone
turns on — which is why #447's investigation could not trust its own instrument until this was
fixed.

Pure function tests. No store, no embedder, no loop.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_MEMORY_PI = _ROOT / "docs" / "demos" / "memory_pi.py"


def _load() -> Any:
    """Import `memory_pi` by path — `docs/demos` is not a package."""
    spec = importlib.util.spec_from_file_location("memory_pi_matching", _MEMORY_PI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_pi_matching"] = module
    spec.loader.exec_module(module)
    return module


memory_pi = _load()


def test_a_hyphen_in_the_stored_row_does_not_hide_the_fact() -> None:
    """The exact row that scored as a miss at the M7 seal, against its exact declared needle."""
    assert memory_pi._matches(
        "Ali's team stand-up is at 9 AM every weekday", ["standup"]
    ), "a correctly-stored fact still scores as a recall miss over one hyphen"


def test_the_shipped_corpus_needles_all_match_their_own_declared_text() -> None:
    """Every declared fact must match its own `say` string.

    ⚠️ A corpus whose needle cannot find its own declared sentence is one that would report a miss
    no matter what the robot did — the failure is in the ruler, and it would be scored as the
    robot's.
    """
    spec = json.loads(
        (_ROOT / "docs" / "demos" / "m7_evidence" / "facts.json").read_text(
            encoding="utf-8"
        )
    )
    unmatched = [
        fact["key"]
        for fact in spec["facts"]
        if not memory_pi._matches(fact["say"], fact["match"])
    ]
    assert unmatched == [], (
        f"needles that cannot match their own `say` text: {unmatched}"
    )


def test_punctuation_folding_does_not_make_a_different_fact_score_as_this_one() -> None:
    """⚠️ The half that must survive the fix: **strict on content.**

    Loosening a matcher is easy to do too far, and the failure would be silent and *flattering* —
    a gate that scores the wrong row as the right one reports a higher recall rate than the robot
    earned. `_matches`'s own docstring names the case, so it is asserted rather than trusted.
    """
    assert not memory_pi._matches("Ali drinks tea", ["coffee"])
    assert not memory_pi._matches("Ali runs on Tuesday mornings", ["marathon"])
    assert not memory_pi._matches("Ali's sister Maya is a doctor", ["manager"])
    # Every needle must still be present, not just one of them.
    assert not memory_pi._matches("Ali loves spicy food", ["spicy", "window seat"])


def test_folding_joins_a_broken_word_without_joining_two_real_ones() -> None:
    """Deleting punctuation rather than spacing it is what makes `stand-up` reach `standup`.

    The risk of deleting is that it could weld two genuinely separate words together. It cannot
    here, because whitespace is preserved independently and collapsed afterwards — so an intra-word
    break closes and a real word boundary does not.
    """
    assert memory_pi._norm("stand-up") == "standup"
    assert memory_pi._norm("black coffee, no sugar") == "black coffee no sugar"
    assert memory_pi._norm("Ali's") == "alis"
    # A real boundary survives: these two words do not become one.
    assert "coffeetea" not in memory_pi._norm("coffee, tea")
