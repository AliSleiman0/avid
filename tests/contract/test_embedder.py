"""Contract suite for the ``Embedder`` port (#118/#119, SDS §9.3).

A port's contract test runs against *every* adapter, real and fake, so the fake can never quietly
drift from the real thing (P6, SDS §3.9.2). The shared tier below is parametrized over the M2.0
hardware seam (:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case
skips off the Pi (SDS §14.4) and, on the Pi, exercises the real ONNX :class:`LocalMiniLmEmbedder`
against the provisioned model — CI proves the mapping and the fake, the device proves the model.

Beyond the shared invariants, three clauses get their own proofs: the fake's *useful geometry* (AC-4
— shared content words pull texts together, so downstream retrieval tests mean something) and its
*cross-process determinism* (AC-3 — a subprocess must return byte-identical vectors, which holds only
because the seed is ``sha256`` and not the per-process-salted builtin ``hash()``); and, on the Pi,
the real model's *reference vector* (AC-1 — a fixed sentence embeds to a committed known-good vector,
which catches a mis-wired pooling or tokenizer that would silently poison every embedding).
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from avid.adapters.embedder import FakeEmbedder, LocalMiniLmEmbedder
from avid.core.ports import Embedder

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

_DIMS = 384

# The AC-1 reference: a fixed sentence and the unit vector the real model produced for it, captured
# at the pinned model revision (tools/fetch_minilm.py). Cross-checked on the Pi by cosine tolerance,
# not exact bytes — ARM vs x86 BLAS rounding differs, but a pooling/tokenizer bug collapses the
# cosine far below the threshold, which is the regression this guards.
_REFERENCE_PATH = Path(__file__).parent / "minilm_reference.json"


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in v))


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


# --- shared contract: every Embedder adapter must satisfy it ----------------


@pytest.fixture(params=FAKE_REAL_PARAMS, scope="session")
def make_embedder(request: pytest.FixtureRequest) -> Callable[[], Embedder]:
    """A factory for the current param's adapter, so a test can build a *fresh* instance of the same
    type (the cross-instance determinism proof needs two). The ``"real"`` case skips off the Pi; on
    the Pi it builds :class:`LocalMiniLmEmbedder` at its default on-device model path (P3/§14.4)."""
    param = request.param
    if param == "real":
        skip_off_pi(
            "LocalMiniLmEmbedder needs the on-Pi model blob (real adapter, §14.4)"
        )

    def build() -> Embedder:
        if param == "fake":
            return FakeEmbedder(dimensions=_DIMS)
        return LocalMiniLmEmbedder(dimensions=_DIMS)

    return build


@pytest.fixture(scope="session")
def embedder(make_embedder: Callable[[], Embedder]) -> Embedder:
    """Session-scoped on purpose: :class:`LocalMiniLmEmbedder` builds a 90 MB ONNX session on its
    first ``embed()``, measured at ~565 ms on the Pi, and a function-scoped fixture paid that once
    per test. Both adapters are stateless past that cached session, so sharing one instance across
    the module is safe — and the tests that specifically need a *second* instance build their own
    through ``make_embedder`` rather than relying on this fixture's scope (#168)."""
    return make_embedder()


def test_adapter_satisfies_the_embedder_port(embedder: Embedder) -> None:
    assert isinstance(embedder, Embedder)


def test_dimensions_matches_the_vector_length(embedder: Embedder) -> None:
    v = await_embed(embedder, "the cat sat on the mat")
    assert embedder.dimensions == _DIMS
    assert len(v) == _DIMS


def test_vectors_are_pre_normalised(embedder: Embedder) -> None:
    """AC-2: ‖v‖ ≈ 1, so cosine similarity is a plain dot product (§8.2/§7.7)."""
    for text in ("hello world", "a", "coffee, black, no sugar", "  spaced  out  "):
        assert _norm(await_embed(embedder, text)) == pytest.approx(1.0, abs=1e-5)


def test_embedding_is_deterministic_across_instances(
    embedder: Embedder, make_embedder: Callable[[], Embedder]
) -> None:
    """AC-3 (in-process half): the same text embeds to the identical vector, even from a *fresh*
    instance — so the vectors cannot depend on per-instance state, only on the text. Holds for both
    adapters: the fake reseeds from the text, and the real model is deterministic on one machine."""
    text = "my name is Ali and I drink oat milk"
    first = await_embed(embedder, text)
    fresh = await_embed(make_embedder(), text)
    assert tuple(first) == tuple(fresh)


def test_distinct_texts_give_distinct_vectors(embedder: Embedder) -> None:
    assert tuple(await_embed(embedder, "cats")) != tuple(await_embed(embedder, "dogs"))


def test_empty_string_is_defined_not_a_crash(embedder: Embedder) -> None:
    """AC-5: the empty string (and all-punctuation) is a defined unit vector, never a crash or a
    zero vector that cannot be normalised."""
    for text in ("", "   ", "!!!"):
        v = await_embed(embedder, text)
        assert len(v) == _DIMS
        assert _norm(v) == pytest.approx(1.0, abs=1e-5)


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


# --- LocalMiniLmEmbedder-specific: the real model's reference vector (Pi-gated) ----


@pytest.mark.hardware
def test_real_embedder_matches_reference_vector() -> None:
    """AC-1: on the Pi, a fixed sentence embeds to the committed known-good vector.

    Asserted by cosine (both are unit vectors, so the dot product *is* the cosine) with a loose
    floor: ARM-vs-x86 BLAS rounding shifts individual components slightly, but a wrong pooling
    (CLS-only instead of mean, or the mask dropped) or the wrong tokenizer collapses the cosine far
    below this, which is exactly the silent quality bug this test exists to catch (§7.4)."""
    skip_off_pi("reference vector needs the on-Pi MiniLM model (real adapter, §14.4)")
    reference = json.loads(_REFERENCE_PATH.read_text())
    v = await_embed(LocalMiniLmEmbedder(dimensions=_DIMS), reference["sentence"])
    assert len(v) == _DIMS
    assert _norm(v) == pytest.approx(1.0, abs=1e-5)
    cosine = _dot(v, reference["vector"])
    assert cosine > 0.999, (
        f"cosine {cosine:.6f} vs reference — pooling/tokenizer regression?"
    )


# --- tiny async bridge ------------------------------------------------------


def await_embed(embedder: Embedder, text: str) -> Sequence[float]:
    """Run the coroutine ``embed`` to completion in a fresh loop.

    The shared tests are plain (non-``async``) functions because embedding is a one-shot call with
    nothing to interleave; a per-call loop keeps them synchronous and readable rather than dragging
    ``anyio``/``asyncio`` fixtures through every assertion.
    """
    return asyncio.run(embedder.embed(text))
