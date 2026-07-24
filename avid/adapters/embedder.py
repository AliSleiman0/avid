"""Embedder adapters — the fake, ahead of its real counterpart (#118, SDS §9.3).

:class:`FakeEmbedder` satisfies the :class:`~avid.core.ports.Embedder` port with **zero
third-party dependencies** — stdlib only. That is the whole point of splitting it from the real
``LocalMiniLmEmbedder`` (all-MiniLM-L6-v2 via ONNX, ADR-011/§7.4, a later issue): every memory
consumer — the §8.5 index (#120), ``MemoryService`` (#122) — is built and CI-tested against this
fake before the ~90 MB model file exists, and the heavy ``onnxruntime``/``numpy`` dependency never
enters CI. Per SDS §3.9.2 the fake is not a test double; it *is* the CI embedder and the simulator.

Two properties the real model also has, reproduced here so downstream tests are meaningful rather
than merely green:

* **Pre-normalised output (§8.2).** Every returned vector is unit length, so cosine similarity is a
  dot product and §7.7's ranking needs no per-query normalisation.
* **Useful geometry (AC-4).** Texts sharing content words land measurably closer than unrelated
  texts — not random noise — so a retrieval test downstream actually exercises retrieval instead of
  passing by coincidence. A bag-of-words sum of per-token vectors gives exactly that: shared words
  contribute shared component vectors.

The seed is a :func:`hashlib.sha256` digest of each token, **never Python's builtin ``hash()``** —
``hash()`` of a ``str`` is salted per process by ``PYTHONHASHSEED``, so it would hand back different
vectors after a restart. Determinism across a process restart is a hard requirement (AC-3): the M7
gate restarts the robot and expects the same vectors, and a stored BLOB embedded yesterday must match
one embedded today. ``sha256`` is process-stable by construction, which is what makes that hold.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from random import Random

# Words to embed: runs of alphanumerics, case-folded. Punctuation and spacing are separators, so
# "Coffee, black." and "black coffee" share both content tokens (AC-4).
_WORD = re.compile(r"\w+")


class FakeEmbedder:
    """Deterministic, dependency-free :class:`~avid.core.ports.Embedder` (P6, SDS §3.9.2).

    Constructed only by the composition root or a test fixture (P3). ``dimensions`` is injected so
    the same fake stands in for any real model's width (384 for MiniLM, §7.4) and the composition
    root's :func:`~avid.main._build_embedder` dimension guard has something to check against.
    """

    def __init__(self, *, dimensions: int = 384) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        """Embed ``text`` into a pre-normalised, unit-length vector of :attr:`dimensions` floats.

        Each token seeds a PRNG that draws one ``dimensions``-long Gaussian vector; the token
        vectors are summed (bag-of-words) and the sum is L2-normalised. A token-less input (the
        empty string, or all-punctuation) falls back to the raw text as a single token, so a
        deterministic unit vector is always returned and never a zero vector that cannot be
        normalised (AC-5). Pure CPU, no I/O — returns directly without touching the loop.
        """
        tokens = _WORD.findall(text.lower()) or [text]
        acc = [0.0] * self._dimensions
        for token in tokens:
            vec = self._token_vector(token)
            for i in range(self._dimensions):
                acc[i] += vec[i]
        norm = math.sqrt(math.fsum(x * x for x in acc))
        # A non-empty token list of finite Gaussians virtually never sums to the zero vector, but
        # guard the divide so a pathological cancellation degrades to a fixed unit vector rather
        # than producing NaNs that would silently poison every downstream cosine.
        if norm == 0.0:  # pragma: no cover - unreachable for finite Gaussian sums
            return tuple(1.0 if i == 0 else 0.0 for i in range(self._dimensions))
        return tuple(x / norm for x in acc)

    def _token_vector(self, token: str) -> list[float]:
        """One token's Gaussian vector, seeded from its ``sha256`` so it is identical in every
        process and every run (AC-3). The same token always yields the same vector, which is why
        shared words pull two texts together (AC-4)."""
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        rng = Random(int.from_bytes(digest, "big"))
        return [rng.gauss(0.0, 1.0) for _ in range(self._dimensions)]
