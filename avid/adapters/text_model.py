"""``TextModel`` adapters — the deterministic fake for §7.8 supersession (#122).

The port behind which the cheap, off-turn-path text model hides (SDS §7.8, §9.4 catalog): its one real
adapter is an OpenAI text client over HTTPS (a later issue, #121); :class:`FakeTextModel` is the P6 fake
and simulator, and §7.8's tier-1 test double. A supersession judgment on a real LLM is genuine semantic
reasoning ("I switched to tea" contradicts "I drink coffee") that no dependency-free stand-in can
replicate — so the fake makes the *honest* conservative call: it supersedes only a **literal
restatement**, measured by word-token overlap, and never confabulates a semantic shift (§7.8: "unknown
is a valid answer; a confabulated one is a bug"). Tests that need the semantic case script a decision
directly, exactly as ``tests/adapters/test_retrieval.py`` scripts an embedder.

Stdlib only (P1/ADR-012): no vendor import, no ``numpy`` — the fake ships everywhere the sim runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# The word tokeniser and the tiny stop set the Jaccard overlap ignores, so "I live in Boston" and
# "I live in Seattle" are compared on {live, boston} vs {live, seattle} rather than being dragged
# together by the function words every sentence shares. Deliberately minimal — a proper stop list is
# the real ONNX/OpenAI model's concern, not this deterministic stand-in's.
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    {
        "i",
        "a",
        "an",
        "the",
        "is",
        "am",
        "are",
        "was",
        "were",
        "to",
        "of",
        "in",
        "on",
        "at",
        "my",
        "me",
        "you",
        "it",
        "and",
        "or",
        "that",
        "this",
        "have",
        "has",
    }
)

# The default overlap at which the fake calls two facts the same statement restated. 0.6 clears "I like
# coffee" → "I really like coffee" ({like, coffee} vs {really, like, coffee}, 2/3) while leaving
# unrelated facts alone. A constructor knob so a test can tighten or loosen it.
_DEFAULT_THRESHOLD = 0.6


def _tokens(text: str) -> frozenset[str]:
    """The lowercased content-word set of ``text`` (stop words dropped) — the unit Jaccard compares."""
    return frozenset(
        t for t in (m.lower() for m in _WORD.findall(text)) if t not in _STOP
    )


class FakeTextModel:
    """Deterministic :class:`~avid.core.ports.TextModel` — supersede a literal restatement (§7.8, P6).

    Judges a candidate superseded iff its content-word set and the new fact's overlap by Jaccard ≥
    ``threshold``. Catches restatements ("I live in Boston" → "I live in Boston now"); misses genuine
    semantic contradictions with low lexical overlap (coffee → tea) — by design, because a real judgment
    needs the real model. No state, no I/O, no third-party import.
    """

    def __init__(self, *, threshold: float = _DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        """Return the candidate ids whose text is a near-restatement of ``new_fact`` (Jaccard ≥
        threshold), best-effort and never a guess: ``()`` when there are no candidates, the new fact has
        no content words, or none overlap enough."""
        new_tokens = _tokens(new_fact)
        if not new_tokens:
            return []
        superseded: list[int] = []
        for fact_id, text in candidates:
            cand = _tokens(text)
            if not cand:
                continue
            overlap = len(new_tokens & cand) / len(new_tokens | cand)
            if overlap >= self._threshold:
                superseded.append(fact_id)
        return superseded


__all__ = ["FakeTextModel"]
