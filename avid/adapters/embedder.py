"""Embedder adapters — the stdlib fake and the real ONNX MiniLM (#118/#119, SDS §9.3).

Two implementations of the :class:`~avid.core.ports.Embedder` port, both behind the one contract
suite (P6, SDS §14.4):

:class:`FakeEmbedder` satisfies the port with **zero third-party dependencies** — stdlib only. That
is the whole point of splitting it from the real :class:`LocalMiniLmEmbedder`: every memory consumer
— the §8.5 index (#120), ``MemoryService`` (#122), pre-session injection (#126) — is built and
CI-tested against this fake, and the heavy ``onnxruntime``/``numpy``/``tokenizers`` dependency never
enters CI. Per SDS §3.9.2 the fake is not a test double; it *is* the CI embedder and the simulator.

Two properties the real model also has, reproduced in the fake so downstream tests are meaningful
rather than merely green:

* **Pre-normalised output (§8.2).** Every returned vector is unit length, so cosine similarity is a
  dot product and §7.7's ranking needs no per-query normalisation.
* **Useful geometry (AC-4).** Texts sharing content words land measurably closer than unrelated
  texts — not random noise — so a retrieval test downstream actually exercises retrieval instead of
  passing by coincidence. A bag-of-words sum of per-token vectors gives exactly that: shared words
  contribute shared component vectors.

The fake's seed is a :func:`hashlib.sha256` digest of each token, **never Python's builtin
``hash()``** — ``hash()`` of a ``str`` is salted per process by ``PYTHONHASHSEED``, so it would hand
back different vectors after a restart. Determinism across a process restart is a hard requirement
(AC-3): the M7 gate restarts the robot and expects the same vectors, and a stored BLOB embedded
yesterday must match one embedded today. ``sha256`` is process-stable by construction.

:class:`LocalMiniLmEmbedder` is the real one — all-MiniLM-L6-v2, 384 dims, via ONNX (ADR-011/§7.4).
It follows :class:`~avid.adapters.vad.SileroVad` line for line: ``onnxruntime``, ``numpy`` and
``tokenizers`` are **lazily imported** inside :meth:`~LocalMiniLmEmbedder._ensure_session` — they
belong to the Pi-only ``pi`` extra and are absent off it (ADR-008), so keeping them out of module
scope lets this file load everywhere (the fake path, mypy, and the composition-root import all work
off-Pi, P5). The ~80–90 MB model file lives on the device and is never committed; its real contract
leg is Pi-gated so CI never sees the blob (§14.4). Three things differ from Silero: inference is
*tens of ms* (not sub-ms), so it runs **off the loop** via :func:`asyncio.to_thread` (P8, AC-6); it
needs a WordPiece tokenizer, loaded from the model's ``tokenizer.json``; and its ONNX thread pool is
capped at **two** intra-op threads rather than Silero's one — the same defect with different
arithmetic, justified in :meth:`LocalMiniLmEmbedder._ensure_session` (#168).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import Sequence
from pathlib import Path
from random import Random
from typing import Any

_log = logging.getLogger(__name__)

# Words to embed: runs of alphanumerics, case-folded. Punctuation and spacing are separators, so
# "Coffee, black." and "black coffee" share both content tokens (AC-4).
_WORD = re.compile(r"\w+")

# The on-Pi model + tokenizer location a deploy/setup step populates via tools/fetch_minilm.py (kept
# out of git: the real adapter is Pi-only, so CI never needs the ~80–90 MB blob). Overridable via
# the injected model_path/tokenizer_path (P7), mirroring _DEFAULT_MODEL_PATH in vad.py.
_DEFAULT_MODEL_PATH = Path("/var/lib/robot/models/all-MiniLM-L6-v2.onnx")
_TOKENIZER_FILENAME = "tokenizer.json"
_FETCH_SCRIPT = "tools/fetch_minilm.py"


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


class LocalMiniLmEmbedder:
    """The real :class:`~avid.core.ports.Embedder`: all-MiniLM-L6-v2, 384 dims, via ONNX (ADR-011).

    ``onnxruntime``, ``numpy`` and ``tokenizers`` are imported lazily inside :meth:`_ensure_session`
    (P5, ADR-008): they belong to the Pi-only ``pi`` extra and are absent off it, so keeping them out
    of module scope lets this file load everywhere — the fake path, mypy, and the composition-root
    import all work off-Pi. The ONNX session and tokenizer are built on first use and carried across
    calls, so :meth:`embed` is allocation-light after warm-up.

    :meth:`embed` tokenizes the text (WordPiece, from the model's ``tokenizer.json``), runs the
    transformer, **mean-pools the token embeddings with the attention mask applied** (AC-1 — the
    canonical sentence-transformers pooling; pooling done wrong is the classic silent quality bug),
    and L2-normalises so ‖v‖ = 1 (§8.2 — cosine becomes a dot product, the port's promise). Inference
    is *tens of ms* (§7.4 — plausible, not measured; the adapter logs its own latency so the gate
    records a real number), so unlike :class:`~avid.adapters.vad.SileroVad`'s sub-ms inline call it
    runs **off the event loop** via :func:`asyncio.to_thread` (P8, AC-6). ``dimensions``/``model_path``/
    ``tokenizer_path`` are injected (P7).
    """

    def __init__(
        self,
        *,
        dimensions: int = 384,
        model_path: Path | None = None,
        tokenizer_path: Path | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._model_path = model_path if model_path is not None else _DEFAULT_MODEL_PATH
        # The tokenizer.json sits beside the model unless told otherwise — one fetch, one dir.
        self._tokenizer_path = (
            tokenizer_path
            if tokenizer_path is not None
            else self._model_path.parent / _TOKENIZER_FILENAME
        )
        # Built lazily on the Pi; untyped (Any) because onnxruntime/numpy/tokenizers ship no stubs
        # and are absent off-Pi (mypy resolves them via ignore_missing_imports).
        self._session: Any | None = None
        self._tokenizer: Any | None = None

    @property
    def dimensions(self) -> int:
        """The fixed vector width (384 for MiniLM, §7.4). Checked against ``[memory] dimensions`` at
        composition (P7) so a model/config mismatch fails loudly at startup, not as a corrupt index."""
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        """Embed ``text`` into a pre-normalised, unit-length vector of :attr:`dimensions` floats.

        The model inference is *tens of ms* of pure CPU, so it runs on a worker thread — the event
        loop is never blocked (P8, AC-6). The returned :class:`tuple` crosses the port as plain
        ``float``s; packing to the §8.2 BLOB is the repository's job, not this adapter's."""
        return await asyncio.to_thread(self._embed_sync, text)

    def _embed_sync(self, text: str) -> tuple[float, ...]:
        """The synchronous inference body run on the worker thread (never on the loop, AC-6).

        Tokenize → transformer → **mask-weighted mean-pool** (AC-1) → L2-normalise (§8.2). The
        per-embed latency is measured with :func:`time.monotonic_ns` (never wall clock) and logged,
        so the §7.4 number is recorded at the gate rather than assumed."""
        np = self._ensure_session()
        assert self._tokenizer is not None and self._session is not None
        started_ns = time.monotonic_ns()

        encoding = self._tokenizer.encode(text)
        input_ids = np.asarray([encoding.ids], dtype=np.int64)
        attention_mask = np.asarray([encoding.attention_mask], dtype=np.int64)
        # token_type_ids is all-zeros for a single segment. Bind inputs by the session's own declared
        # names so an export that omits token_type_ids (some MiniLM exports do) still runs, instead of
        # erroring on an unexpected feed key.
        candidates = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": np.zeros_like(input_ids),
        }
        feed = {
            inp.name: candidates[inp.name]
            for inp in self._session.get_inputs()
            if inp.name in candidates
        }

        (token_embeddings, *_) = self._session.run(None, feed)
        # Mask-weighted mean over the sequence axis: sum the token vectors the mask keeps, divide by
        # how many that was (clipped off zero so an all-pad row can never divide by zero — AC-1/AC-5).
        mask = attention_mask.astype(np.float32)[:, :, None]
        summed = (token_embeddings * mask).sum(axis=1)
        counts = mask.sum(axis=1)
        np.clip(counts, 1e-9, None, out=counts)
        pooled = summed / counts
        # L2-normalise so ‖v‖ = 1 — the port's pre-normalised promise (§8.2), same guard as the fake.
        norms = np.sqrt((pooled * pooled).sum(axis=1, keepdims=True))
        np.clip(norms, 1e-12, None, out=norms)
        vector = (pooled / norms)[0]

        elapsed_ms = (time.monotonic_ns() - started_ns) / 1_000_000
        _log.debug("embedded %d chars in %.1f ms", len(text), elapsed_ms)
        return tuple(float(x) for x in vector)

    def _ensure_session(self) -> Any:
        """Build the ONNX session and tokenizer on first use; return the ``numpy`` module.

        Lazy, Pi-only imports — kept out of module scope so this file loads off-Pi (P5, ADR-008).
        The model/tokenizer files are checked **before** onnxruntime touches them, so a missing blob
        fails with an actionable error naming the path and the fetch script (AC-7) rather than an
        opaque ONNX ``NoSuchFile`` traceback."""
        import numpy as np

        if self._session is None:
            for path, what in (
                (self._model_path, "model"),
                (self._tokenizer_path, "tokenizer"),
            ):
                if not path.is_file():
                    raise FileNotFoundError(
                        f"MiniLM {what} not found at {path} — provision it on the device with "
                        f"`python {_FETCH_SCRIPT}` (the ~80–90 MB blob is not committed; #119)"
                    )
            import onnxruntime
            from tokenizers import Tokenizer

            # Bounded, non-spinning thread pool — the same treatment as
            # avid/adapters/vad.py's SileroVad session, with a DIFFERENT thread count. Keep the
            # two in step: they are the project's only two ONNX adapters (#168).
            #
            # ONNX Runtime defaults to one intra-op thread PER CORE and spin-waits between
            # inferences. Measured on the Pi (n=20 embeds, 4 cores): the shipped default took
            # 124 ms/embed while burning **3.92 of 4 cores**, which starves the audio loop and is
            # how the robot went deaf at M5 — P8 violated by CPU monopoly, not by an un-threaded
            # call, so the `to_thread` hop in embed() cannot save us on its own.
            #
            # 2, not vad.py's 1: Silero is tiny (~0.4 ms/call) so single-threading it is nearly
            # free, but MiniLM is a 90 MB transformer where it costs 340 ms/embed. The sweep:
            #
            #   threads | median/embed | cores busy
            #   1       | 340.5 ms     | 1.00
            #   2       | 192.1 ms     | 1.93   <- half the latency of 1, half the cores of 4
            #   3       | 239.0 ms     | 2.11
            #   4       | 191.2 ms     | 2.78   <- no faster than 2, 0.85 more cores
            #
            # 192 ms leaves ~5x headroom against `memory_inject_timeout_s` = 1.0 s and keeps two
            # cores for the audio loop and the websocket that open() runs concurrently.
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            # Kept even though the sweep shows spinning barely moves the median: the probe runs
            # embeds back to back and so measures only the *busy* case, while M5's lesson was
            # about cores burned **between** calls, with the robot idle. Off is the honest default.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self._session = onnxruntime.InferenceSession(
                str(self._model_path), sess_options=options
            )
            self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
        return np
