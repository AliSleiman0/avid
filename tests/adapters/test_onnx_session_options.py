"""Both ONNX adapters cap their thread pool — the #168 regression guard.

ONNX Runtime defaults to one intra-op thread **per core** and spin-waits between inferences. On the
4-core Pi that is a starvation bug wearing a performance costume: it violates P8 by *CPU monopoly*
rather than by an un-threaded call, so ``asyncio.to_thread`` does not save you and code review
cannot see it. It cost the project twice — Silero pegged three cores at the M5 bench and the robot
went deaf; :class:`~avid.adapters.embedder.LocalMiniLmEmbedder` then shipped with the same default
untouched for a whole milestone (#168), latent only because the real leg had never run on the Pi.

Both defects were found on hardware, by hand, after the fact. This suite makes the *next* one a CI
failure: it stubs ``onnxruntime`` in :data:`sys.modules` — a hand-written recording stub, not a mock
(SDS §14.3) — builds each adapter's session, and asserts the four settings that bound the pool. No
model blob, no Pi, no ONNX install required, so it runs on every leg.

It deliberately pins the thread **counts**, which differ (2 for MiniLM, 1 for Silero) and are not
interchangeable: Silero is a ~0.4 ms model where single-threading is nearly free, MiniLM a 90 MB
transformer where it costs 340 ms/embed. Pasting one adapter's number into the other is the exact
mistake this file exists to catch, so a change to either count has to be argued for here as well as
measured on the device.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from avid.adapters.embedder import LocalMiniLmEmbedder
from avid.adapters.vad import SileroVad

# The ONNX config key that stops the pool spin-waiting between inferences. Bounding the thread
# count alone is not enough: even a single thread burns its core between calls unless told not to.
_ALLOW_SPINNING = "session.intra_op.allow_spinning"


class _RecordingSessionOptions:
    """Stands in for ``onnxruntime.SessionOptions``, recording what the adapter set on it.

    The attributes start at ``None`` rather than at ONNX's real defaults on purpose: an adapter
    that never touches one leaves it ``None``, which fails the assertion loudly instead of quietly
    matching whatever ONNX would have chosen.
    """

    def __init__(self) -> None:
        self.intra_op_num_threads: int | None = None
        self.inter_op_num_threads: int | None = None
        self.execution_mode: object | None = None
        self.config_entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries[key] = value


class _RecordingSession:
    """Stands in for ``onnxruntime.InferenceSession``; captures the options it was handed."""

    def __init__(self, model_path: str, sess_options: Any = None) -> None:
        self.model_path = model_path
        self.sess_options = sess_options

    def get_inputs(
        self,
    ) -> list[Any]:  # pragma: no cover - no inference runs in this suite
        return []


class _RecordingTokenizer:
    """Stands in for ``tokenizers.Tokenizer`` — the embedder loads one beside the model."""

    @staticmethod
    def from_file(path: str) -> _RecordingTokenizer:
        return _RecordingTokenizer()


_ORT_SEQUENTIAL = (
    object()
)  # identity sentinel for onnxruntime.ExecutionMode.ORT_SEQUENTIAL


@pytest.fixture
def onnx_stub(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Install stub ``onnxruntime``/``tokenizers`` modules for the adapters' lazy imports.

    Both adapters import these *inside* ``_ensure_session`` (P5/ADR-008 — they are Pi-only), which
    is what makes a :data:`sys.modules` stub sufficient and honest here: the adapter runs its real
    session-building code, and only the vendor library is stood in for.
    """
    # setattr rather than attribute assignment: a ModuleType has no declared attributes, so the
    # direct form needs a `# type: ignore[attr-defined]` on every line to no benefit.
    execution_mode = ModuleType("ExecutionMode")
    setattr(execution_mode, "ORT_SEQUENTIAL", _ORT_SEQUENTIAL)

    onnxruntime = ModuleType("onnxruntime")
    setattr(onnxruntime, "SessionOptions", _RecordingSessionOptions)
    setattr(onnxruntime, "InferenceSession", _RecordingSession)
    setattr(onnxruntime, "ExecutionMode", execution_mode)

    tokenizers = ModuleType("tokenizers")
    setattr(tokenizers, "Tokenizer", _RecordingTokenizer)

    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)
    return onnxruntime


def _build_embedder_session(tmp_path: Path) -> _RecordingSessionOptions:
    """Drive ``LocalMiniLmEmbedder._ensure_session`` and return the options it built."""
    # The adapter checks both files exist before ONNX touches them (a friendlier error than a raw
    # NoSuchFile), so give it two empty ones — the stub session never reads their contents.
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = tmp_path / "all-MiniLM-L6-v2.onnx"
    tokenizer = tmp_path / "tokenizer.json"
    model.write_bytes(b"")
    tokenizer.write_text("{}", encoding="utf-8")

    embedder = LocalMiniLmEmbedder(model_path=model, tokenizer_path=tokenizer)
    embedder._ensure_session()
    return _options_of("LocalMiniLmEmbedder", embedder._session)


def _build_vad_session(tmp_path: Path) -> _RecordingSessionOptions:
    """Drive ``SileroVad._ensure_session`` and return the options it built."""
    vad = SileroVad(
        threshold=0.5, sample_rate=16_000, model_path=tmp_path / "silero.onnx"
    )
    vad._ensure_session()
    return _options_of("SileroVad", vad._session)


def _options_of(name: str, session: Any) -> _RecordingSessionOptions:
    """The options *name* handed to its ``InferenceSession`` — the no-options case named.

    Passing no ``sess_options`` at all is the shape the original defect had, so it gets its own
    message rather than surfacing as a bare ``isinstance`` failure that says nothing about why the
    reader should care.
    """
    assert isinstance(session, _RecordingSession)
    assert isinstance(session.sess_options, _RecordingSessionOptions), (
        f"{name} built its InferenceSession with no SessionOptions at all — it inherits ONNX's "
        f"one-thread-per-core spinning default and starves the audio loop on the Pi (#168)"
    )
    return session.sess_options


# (adapter name, builder, expected intra-op threads). The count is part of the expectation, not an
# incidental — see the module docstring on why the two differ.
_ADAPTERS = [
    pytest.param("LocalMiniLmEmbedder", _build_embedder_session, 2, id="embedder"),
    pytest.param("SileroVad", _build_vad_session, 1, id="vad"),
]


@pytest.mark.parametrize(("name", "build", "intra_op"), _ADAPTERS)
@pytest.mark.usefixtures("onnx_stub")
def test_onnx_adapters_bound_their_thread_pool(
    name: str,
    build: Any,
    intra_op: int,
    tmp_path: Path,
) -> None:
    """Every ONNX session is built with an explicit, bounded, non-spinning thread pool (#168).

    All four settings are load-bearing on a 4-core Pi. The thread caps stop the pool taking every
    core during inference; ``ORT_SEQUENTIAL`` stops it running graph nodes in parallel on top of
    that; and ``allow_spinning = 0`` stops the threads burning their cores *between* inferences,
    which is the half no back-to-back benchmark can see.
    """
    options = build(tmp_path)

    assert options.intra_op_num_threads == intra_op, (
        f"{name} left the intra-op pool at {options.intra_op_num_threads} — ONNX's default is one "
        f"thread per core, which starves the audio loop on the Pi (#168)"
    )
    assert options.inter_op_num_threads == 1
    assert options.execution_mode is _ORT_SEQUENTIAL
    assert options.config_entries.get(_ALLOW_SPINNING) == "0", (
        f"{name} lets the ONNX pool spin-wait between inferences — it burns its cores while the "
        f"robot is idle, which is exactly what a busy-loop benchmark cannot measure (#168)"
    )


@pytest.mark.usefixtures("onnx_stub")
def test_the_two_adapters_do_not_share_a_thread_count(tmp_path: Path) -> None:
    """MiniLM gets two threads and Silero one — copying either number to the other is the bug.

    Stated as its own assertion because the temptation runs both ways: #168 shipped by copying
    ONNX's default into the embedder, and the obvious fix — pasting ``vad.py``'s ``1`` — would have
    cost 340 ms/embed against a 1.0 s injection budget. Neither number generalises; each was
    measured on the device it runs on.
    """
    embedder_options = _build_embedder_session(tmp_path / "embedder")
    vad_options = _build_vad_session(tmp_path / "vad")

    # Both counts have to be *set* before "they differ" means anything: two unset pools are also
    # unequal to nothing, and a check that passes on None is the disarmed kind.
    assert embedder_options.intra_op_num_threads is not None
    assert vad_options.intra_op_num_threads is not None
    assert embedder_options.intra_op_num_threads != vad_options.intra_op_num_threads
