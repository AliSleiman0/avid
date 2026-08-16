"""``TextModel`` adapters — the deterministic fake and the real OpenAI client for §7.8 (#122/#121).

The port behind which the cheap, off-turn-path text model hides (SDS §7.8, §9.4 catalog). Two adapters:

* :class:`FakeTextModel` (#122) — the P6 fake, simulator, and §7.8's tier-1 test double. A supersession
  judgment on a real LLM is genuine semantic reasoning ("I switched to tea" contradicts "I drink coffee")
  that no dependency-free stand-in can replicate — so the fake makes the *honest* conservative call: it
  supersedes only a **literal restatement**, measured by word-token overlap, and never confabulates a
  semantic shift (§7.8: "unknown is a valid answer; a confabulated one is a bug"). Stdlib only.
* :class:`OpenAiTextModel` (#121) — the **real** client over the OpenAI chat-completions HTTPS API. It
  **seals the vendor inside** (CLAUDE.md §3, R-10): the ``openai`` SDK is imported **lazily** inside
  :meth:`~OpenAiTextModel.judge_supersession` (the ``openai`` optional group is absent off a networked
  host, so keeping it out of module scope lets this file load everywhere, exactly as
  ``OpenAIRealtimeClient`` does for ``websockets``), and no vendor type ever crosses the port. The two
  network-free, testable pieces — :func:`_build_messages` and :func:`_parse_superseded` — live at module
  scope and are unit-tested offline with canned strings (the only part provable without a socket, mirroring
  ``realtime._translate``). The key is injected already-unwrapped and used only to build the client; the
  ``__repr__`` is key-free (SECURITY.md). A genuine failure (network/timeout/API error, or an unparseable
  reply) **raises** — ``MemoryService`` catches it and degrades to storing the fact without supersession
  (AC-9), the one place with the turn's correlation id to log.

Tests that need the semantic case script a decision directly, exactly as ``tests/adapters/test_retrieval.py``
scripts an embedder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

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

    async def judge_separation(self, *, prompt: str, first: str, second: str) -> bool:
        """Call two answers separated iff their content words overlap **less** than ``threshold``.

        The mirror image of the supersession judgment above, and deliberately the same crude
        instrument: lexical distance is a poor proxy for "different personality" — two configs can
        differ sharply in register while using the same words — so this is not a stand-in for the
        real judge. It exists so the harness's loading, pairing, scoring and report shaping can be
        tested in CI without a key, which is the split §14.7 requires."""
        first_tokens, second_tokens = _tokens(first), _tokens(second)
        if not first_tokens or not second_tokens:
            # Nothing to compare. "Not separated" is the honest answer and the conservative one:
            # a metric that counted empty responses as a success would reward a broken run.
            return False
        overlap = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
        return overlap < self._threshold


_SEPARATION_SYSTEM_PROMPT = (
    "You compare two answers to the same question, each written by a different assistant "
    "persona. Decide ONLY whether the two answers appear to come from DIFFERENT personalities "
    "- different register, verbosity, warmth, or habits. Do NOT judge which answer is better, "
    "more accurate, or more helpful; quality is irrelevant and must not affect your decision. "
    'Reply with JSON: {"separated": true} or {"separated": false}.'
)


def _build_separation_messages(
    prompt: str, first: str, second: str
) -> list[dict[str, str]]:
    """The judge's messages (AVID-215). Pure, so the wording is asserted offline in CI.

    The answers are labelled A and B rather than by config name: naming them would invite the
    judge to reason about which personality is which, and the question is only whether they
    differ."""
    return [
        {"role": "system", "content": _SEPARATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "\n\n".join(
                (f"Question: {prompt}", f"Answer A: {first}", f"Answer B: {second}")
            ),
        },
    ]


def _parse_separated(content: str) -> bool:
    """Read the judge's JSON reply. Raises :class:`ValueError` on anything unparseable.

    Never defaults to ``True``: a judge that failed would otherwise inflate the separation rate,
    which is the one direction a broken measurement must not fail in."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge reply is not JSON: {content!r}") from exc
    if not isinstance(payload, dict) or "separated" not in payload:
        raise ValueError(f"judge reply has no 'separated' key: {content!r}")
    value = payload["separated"]
    if not isinstance(value, bool):
        raise ValueError(f"judge 'separated' is not a boolean: {value!r}")
    return value


# --- OpenAiTextModel (#121): the real chat-completions client ------------------------------

# The §7.8 judge prompt (SDS 1465-1466: "Does F_new update or contradict any of these? Return ids").
# Session-static, so it names the confabulation rule explicitly — "unknown is a valid empty answer" —
# because the whole point of the write-time check is that a *wrong* supersession silently deletes a true
# fact from the present. JSON-object output is forced at the call site, so the shape is contractual.
_SYSTEM_PROMPT = (
    "You decide whether a new fact about a user updates or contradicts any existing facts. "
    "You are given a NEW FACT and a numbered list of EXISTING FACTS. Return a JSON object "
    '{"superseded": [ids]} listing the ids of existing facts the new fact makes no longer '
    "true (it updates or contradicts them). Include an id ONLY when you are confident the new "
    "fact replaces it — an unrelated or merely similar fact is NOT superseded. If none apply, "
    'return {"superseded": []}. Never invent an id that is not listed. Unknown is a valid, '
    "expected empty answer; a confabulated supersession is a bug."
)


def _build_messages(
    new_fact: str, candidates: Sequence[tuple[int, str]]
) -> list[dict[str, str]]:
    """The chat messages for one §7.8 judgment — a fixed system rule plus the new fact and the numbered
    ``id: text`` candidates. Pure and vendor-free (a plain ``list[dict]``), so it is unit-tested offline
    with no client, exactly as ``realtime._translate`` is."""
    existing = "\n".join(f"{cid}: {text}" for cid, text in candidates)
    user = f"NEW FACT:\n{new_fact}\n\nEXISTING FACTS:\n{existing}"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _parse_superseded(content: str, valid_ids: set[int]) -> tuple[int, ...]:
    """Parse the model's ``{"superseded": [ids]}`` reply into the subset of ``valid_ids`` it names, best
    effort and order-stable (AC-1's "a subset of the input ids").

    The subset intersection is the structural enforcement of §7.8's "confabulation is a bug": an id the
    model invents that was never a candidate is **dropped**, never stored, so a hallucinated reply can only
    ever supersede *fewer* facts, never a fact it did not see. A malformed or wrong-shaped reply is a
    "nonsense reply" (AC-9) and :class:`ValueError` — ``MemoryService`` catches it and stores the fact
    without supersession, logging the turn's correlation id."""
    try:
        payload = json.loads(content)
        raw = payload["superseded"]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError(f"unparseable supersession reply: {content!r}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"'superseded' is not a list: {raw!r}")
    result: list[int] = []
    for item in raw:
        try:
            fact_id = int(item)
        except (TypeError, ValueError):
            continue  # a non-int entry cannot name a fact — drop it, do not fail the whole write
        if fact_id in valid_ids and fact_id not in result:
            result.append(fact_id)
    return tuple(result)


class OpenAiTextModel:
    """The real :class:`~avid.core.ports.TextModel`, over the OpenAI chat-completions API (#121, §7.8).

    Maps the §7.8 supersession question onto one cheap, JSON-forced completion and **seals the vendor
    inside** (CLAUDE.md §3, R-10): the ``openai`` SDK is imported lazily in :meth:`judge_supersession` so
    the module loads without the ``openai`` extra, and no vendor type crosses the port — the request is
    built by :func:`_build_messages`, the reply parsed by :func:`_parse_superseded`, both plain values.
    The model is a pinned dated snapshot (``[ai] text_model``, §6.10); ``temperature=0`` and
    ``response_format`` make the judgment deterministic and the shape contractual. Stateless
    request/response, so there is no session lifecycle — the ``AsyncOpenAI`` client is built once, on the
    first call, and lives for the process. Constructed only by the composition root (P3); the key is
    injected already-unwrapped, used only to build the client, and never reaches ``repr`` (AC-6)."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = (
            api_key  # private; only ever handed to the AsyncOpenAI client (AC-6)
        )
        self._model = model
        self._client: Any = None  # the AsyncOpenAI client, untyped (lazy vendor import)

    def __repr__(self) -> str:
        """Key-free repr (AC-6): the secret must never reach a log line via ``repr`` (SECURITY.md)."""
        return f"OpenAiTextModel(model={self._model!r})"

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        """Ask the model which ``candidates`` ``new_fact`` supersedes (§7.8 step 3), returning the subset
        of their ids. Short-circuits with no API call when there are no candidates (the check only fires on
        a near-duplicate, so the common write never reaches here). Raises on a transport/API failure or an
        unparseable reply — the caller's AC-9 guard degrades to no supersession."""
        if not candidates:
            return ()
        from openai import (
            AsyncOpenAI,  # lazy, adapter-local optional group (AC-2, ADR-008)
        )

        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=_build_messages(new_fact, candidates),
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        return _parse_superseded(content, {cid for cid, _ in candidates})

    async def judge_separation(self, *, prompt: str, first: str, second: str) -> bool:
        """Ask whether two answers to one prompt came from different personalities (AVID-215).

        ``temperature=0`` and a JSON response, like the supersession judge above, so a score shift
        means the robot changed rather than the ruler wobbled. The model is **pinned to a dated
        snapshot** through ``[ai] text_model`` for the same reason: a judge is a model, and models
        change."""
        from openai import (
            AsyncOpenAI,  # lazy, adapter-local optional group (AC-2, ADR-008)
        )

        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=_build_separation_messages(prompt, first, second),
            response_format={"type": "json_object"},
            temperature=0,
        )
        return _parse_separated(response.choices[0].message.content or "")


__all__ = ["FakeTextModel", "OpenAiTextModel"]
