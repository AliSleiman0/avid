"""FakeTextModel — the deterministic §7.8 supersession judge (#122).

The P6 fake and simulator, and §7.8's tier-1 test double. It supersedes a **literal restatement** (high
word-token Jaccard) and nothing else — the honest conservative call a dependency-free stand-in can make
without confabulating a semantic shift (§7.8: "unknown is valid; a confabulated answer is a bug").
Adapters are coverage-omitted (P6), so this suite is the proof.
"""

from __future__ import annotations

from avid.adapters import FakeTextModel


async def test_supersedes_a_literal_restatement() -> None:
    superseded = await FakeTextModel().judge_supersession(
        new_fact="the user lives in Boston now",
        candidates=[(7, "the user lives in Boston")],
    )
    assert list(superseded) == [7]


async def test_leaves_an_unrelated_fact_alone() -> None:
    superseded = await FakeTextModel().judge_supersession(
        new_fact="the user has a dog named Rex",
        candidates=[(3, "the user likes jazz")],
    )
    assert list(superseded) == []


async def test_does_not_confabulate_a_semantic_shift() -> None:
    """coffee → tea is a real contradiction with almost no lexical overlap — the fake declines to guess
    it (that needs the real model); unknown is a valid 'not superseded' (§7.8)."""
    superseded = await FakeTextModel().judge_supersession(
        new_fact="the user switched to tea",
        candidates=[(1, "the user drinks coffee")],
    )
    assert list(superseded) == []


async def test_no_candidates_returns_empty() -> None:
    assert (
        list(
            await FakeTextModel().judge_supersession(new_fact="anything", candidates=[])
        )
        == []
    )


async def test_empty_new_fact_returns_empty() -> None:
    superseded = await FakeTextModel().judge_supersession(
        new_fact="", candidates=[(1, "the user drinks coffee")]
    )
    assert list(superseded) == []


async def test_threshold_is_configurable() -> None:
    """A stricter threshold rejects a partial restatement a looser one accepts. ``{dogs, cats, birds,
    fish}`` vs ``{dogs, cats}`` is Jaccard 2/4 = 0.5 — in at 0.5, out at 0.6."""
    new = "dogs cats birds fish"
    cand = [(1, "dogs cats")]
    assert list(
        await FakeTextModel(threshold=0.5).judge_supersession(
            new_fact=new, candidates=cand
        )
    ) == [1]
    assert (
        list(
            await FakeTextModel(threshold=0.6).judge_supersession(
                new_fact=new, candidates=cand
            )
        )
        == []
    )
