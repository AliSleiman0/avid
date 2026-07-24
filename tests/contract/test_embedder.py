"""Contract suite for the ``Embedder`` port (#118, SDS §9.3).

A port's contract test runs against *every* adapter, real and fake, so the fake can never quietly
drift from the real thing (P6, SDS §3.9.2). The shared tier below is parametrized over the adapters
that exist today — only :class:`FakeEmbedder`, since the real ONNX ``LocalMiniLmEmbedder`` lands in a
later issue — and is written so that adapter joins as one extra ``params`` entry, no test-body change.

Two clauses get their own proofs beyond the shared invariants: the fake's *useful geometry* (AC-4 —
shared content words pull texts together, so downstream retrieval tests mean something) and its
*cross-process determinism* (AC-3 — a subprocess must return byte-identical vectors, which holds only
because the seed is ``sha256`` and not the per-process-salted builtin ``hash()``).
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import sys
from collections.abc import Sequence

import pytest

from avid.adapters.embedder import FakeEmbedder
from avid.core.ports import Embedder

_DIMS = 384


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in v))


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


# --- shared contract: every Embedder adapter must satisfy it ----------------


@pytest.fixture(params=["fake"])
def embedder(request: pytest.FixtureRequest) -> Embedder:
    if request.param == "fake":
        return FakeEmbedder(dimensions=_DIMS)
    raise AssertionError(
        f"unknown embedder param {request.param!r}"
    )  # pragma: no cover


def test_adapter_satisfies_the_embedder_port(embedder: Embedder) -> None:
    assert isinstance(embedder, Embedder)


def test_dimensions_matches_the_vector_length(embedder: Embedder) -> None:
    v = await_embed(embedder, "the cat sat on the mat")
    assert embedder.dimensions == _DIMS
    assert len(v) == _DIMS


def test_vectors_are_pre_normalised(embedder: Embedder) -> None:
    """AC-2: ‖v‖ ≈ 1, so cosine similarity is a plain dot product (§8.2/§7.7)."""
    for text in ("hello world", "a", "coffee, black, no sugar", "  spaced  out  "):
        assert _norm(await_embed(embedder, text)) == pytest.approx(1.0, abs=1e-6)


def test_embedding_is_deterministic_across_instances(embedder: Embedder) -> None:
    """AC-3 (in-process half): the same text embeds to the identical vector, even from a *fresh*
    instance — so the vectors cannot depend on per-instance state, only on the text."""
    text = "my name is Ali and I drink oat milk"
    first = await_embed(embedder, text)
    fresh = await_embed(FakeEmbedder(dimensions=_DIMS), text)
    assert tuple(first) == tuple(fresh)


def test_distinct_texts_give_distinct_vectors(embedder: Embedder) -> None:
    assert tuple(await_embed(embedder, "cats")) != tuple(await_embed(embedder, "dogs"))


def test_empty_string_is_defined_not_a_crash(embedder: Embedder) -> None:
    """AC-5: the empty string (and all-punctuation) is a defined unit vector, never a crash or a
    zero vector that cannot be normalised."""
    for text in ("", "   ", "!!!"):
        v = await_embed(embedder, text)
        assert len(v) == _DIMS
        assert _norm(v) == pytest.approx(1.0, abs=1e-6)


# --- FakeEmbedder-specific: geometry and cross-process determinism ----------


def test_shared_words_land_closer_than_unrelated_texts() -> None:
    """AC-4: the fake produces *useful* geometry, not noise — texts sharing content words are
    measurably closer (higher cosine) than an unrelated pair, or every downstream retrieval test
    passes by coincidence."""
    embedder = FakeEmbedder(dimensions=_DIMS)
    a = await_embed(embedder, "I love drinking black coffee in the morning")
    b = await_embed(embedder, "black coffee is my favourite morning drink")
    c = await_embed(embedder, "the spacecraft entered orbit around Jupiter")
    assert _dot(a, b) > _dot(a, c)


def test_embedding_is_deterministic_across_processes() -> None:
    """AC-3 (the honest half): a *separate* Python process embeds the same text to a byte-identical
    vector. This is what proves the seed is ``sha256``-stable and not the ``PYTHONHASHSEED``-salted
    builtin ``hash()`` — a bug an in-process test can never catch."""
    text = "determinism across a process restart is the M7 gate requirement"
    in_process = tuple(await_embed(FakeEmbedder(dimensions=_DIMS), text))

    script = (
        "import asyncio\n"
        "from avid.adapters.embedder import FakeEmbedder\n"
        f"v = asyncio.run(FakeEmbedder(dimensions={_DIMS}).embed({text!r}))\n"
        "print(','.join(x.hex() for x in v))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess_vec = tuple(float.fromhex(x) for x in proc.stdout.strip().split(","))
    assert subprocess_vec == in_process


# --- tiny async bridge ------------------------------------------------------


def await_embed(embedder: Embedder, text: str) -> Sequence[float]:
    """Run the coroutine ``embed`` to completion in a fresh loop.

    The shared tests are plain (non-``async``) functions because embedding is a one-shot call with
    nothing to interleave; a per-call loop keeps them synchronous and readable rather than dragging
    ``anyio``/``asyncio`` fixtures through every assertion.
    """
    return asyncio.run(embedder.embed(text))
