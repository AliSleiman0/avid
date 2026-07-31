"""VAD adapters — the fake that *is* the simulator, and the real Silero detector (AVID-77).

Two implementations of the :class:`~avid.core.ports.VoiceActivityDetector` port, both behind
the one contract suite (P6, SDS §14.4). The port makes a single promise —
:meth:`~avid.core.ports.VoiceActivityDetector.is_speech`, a synchronous per-frame yes/no on an
:class:`~avid.core.hal.AudioChunk` — and both adapters keep it identically (SDS §6.3, §9.3):

* :class:`FakeVoiceActivityDetector` returns a **scriptable speech/silence timeline** and *is*
  the simulator VAD, so the sim can never drift from the real detector — it is the real gate
  with no model behind it (SDS §3.9.2). Stdlib only.
* :class:`SileroVad` runs the Silero v5 ONNX model via ``onnxruntime``. That library and
  ``numpy`` are pip-on-Pi (the ``pi`` extra) and absent off it (ADR-008, like ``picamera2`` /
  ``pyalsaaudio``), so they are imported **only** inside this module and **lazily**, inside the
  session helper — the module itself imports cleanly on CI and a laptop, where the fake path and
  mypy still need it to load (P5).

Two things the port deliberately keeps on *our* side of the boundary:

* **No vendor types cross it.** Silero speaks in probabilities, model handles and RNN state;
  the port speaks in frames and a ``bool``. The probability *threshold* is the adapter's
  business (SDS §6.3), so swapping Silero for another detector is exactly one adapter (P2).
* **The call is synchronous and fast** (SDS §9.3 budgets <5 ms/frame). Silero is sub-ms per
  frame on the Pi (SDS §6.3), comfortably under the 50 ms slow-callback gate, so inference runs
  in-process and the caller invokes it inline — no ``run_in_executor``, no loop hop (P8).

Constructed only by the composition root or a test fixture (P3); everything else depends on the
port (P2).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from avid.core.hal import AudioChunk

# S16_LE, 2 bytes/sample: the mic's capture format (see microphone.py). Silero wants float32
# samples normalised to [-1, 1], so int16 divides by this full-scale value.
_INT16_FULL_SCALE = 32768.0

# Silero v5 consumes a fixed window per inference: 512 samples at 16 kHz (256 at 8 kHz). Mic
# chunks are 20 ms = 320 samples, so the adapter re-windows — it buffers incoming PCM and runs
# the model once per whole 512-sample window (SDS §6.3 "every 30 ms frame" is our cadence; 512
# is Silero's hard requirement, absorbed here so the port stays in our vocabulary).
_SILERO_WINDOW = {16000: 512, 8000: 256}

# The on-Pi model location a deploy/setup step populates (kept out of git: the real adapter is
# Pi-only, so CI never needs the ~1 MB blob). Overridable via the injected model_path (P7).
_DEFAULT_MODEL_PATH = Path("/var/lib/robot/models/silero_vad.onnx")


class FakeVoiceActivityDetector:
    """The :class:`~avid.core.ports.VoiceActivityDetector` fake (P6): a scripted timeline, no model.

    ``is_speech`` walks an injected ``script`` of verdicts one call at a time — the scriptable
    speech/silence timeline the AudioService tests (AVID-79) drive turns through. Once the script
    is exhausted it **holds the last verdict** (or ``default`` if the script was empty), so a test
    can say "speech for three frames, then silence forever" without listing every frame. The public
    :attr:`calls` counter is the off-port observation point the contract asserts on, exactly as
    ``FakeMicrophone.chunks_yielded`` is off the ``Microphone`` port. Stdlib only.
    """

    def __init__(
        self, script: Sequence[bool] | None = None, *, default: bool = False
    ) -> None:
        self._script = list(script) if script is not None else []
        self._default = default
        # Public, assertable: how many frames were judged (off the port).
        self.calls = 0

    def is_speech(self, frame: AudioChunk) -> bool:
        """Return the next scripted verdict, holding the last once the script runs out.

        The ``frame`` is accepted and ignored — the fake's verdict comes from the script, not the
        PCM, which is what makes a speech/silence timeline reproducible in a millisecond test."""
        index = self.calls
        self.calls += 1
        if not self._script:
            return self._default
        if index < len(self._script):
            return self._script[index]
        return self._script[-1]


class SileroVad:
    """The real :class:`~avid.core.ports.VoiceActivityDetector`, running Silero v5 via ONNX.

    ``onnxruntime`` and ``numpy`` are imported lazily inside :meth:`_ensure_session` (P5, ADR-008):
    they belong to the Pi-only ``pi`` extra and are absent off the Pi, so keeping them out of module
    scope lets this file load everywhere — the fake path, mypy, and the composition-root import all
    work off-Pi. The session and the model's RNN ``state`` are built on first use and carried across
    calls (Silero is stateful), so :meth:`is_speech` is a hot, allocation-light path.

    :meth:`is_speech` normalises the chunk's S16_LE PCM to float32, appends it to a rolling buffer,
    and runs the model once per whole :data:`_SILERO_WINDOW` window, thresholding the resulting
    speech probability against the injected ``threshold`` to a ``bool``. Inference is sub-ms (SDS
    §6.3), so it runs inline rather than on a worker thread — well under the 50 ms slow-callback
    gate (P8). ``threshold``/``sample_rate``/``model_path`` are injected (P7).
    """

    def __init__(
        self,
        *,
        threshold: float,
        sample_rate: int,
        model_path: Path | None = None,
    ) -> None:
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._model_path = model_path if model_path is not None else _DEFAULT_MODEL_PATH
        self._window = _SILERO_WINDOW.get(sample_rate, 512)
        # Built lazily on the Pi; untyped (Any) because onnxruntime/numpy ship no stubs and are
        # absent off-Pi (mypy resolves them via ignore_missing_imports).
        self._session: Any | None = None
        self._state: Any | None = None
        self._sr_arg: Any = None  # int64 sample-rate arg, built with the session
        self._buffer: Any | None = (
            None  # rolling float32 samples awaiting a full window
        )
        # Last decision, held between windows so a sub-window chunk returns the current verdict.
        self._last = False

    def is_speech(self, frame: AudioChunk) -> bool:
        """Judge *frame*, running the model once per full window (SDS §6.3, §9.3).

        A chunk shorter than a Silero window buffers and returns the standing verdict; each time
        the buffer fills a window, the model runs and updates it. Synchronous by contract."""
        np = self._ensure_session()
        samples = np.frombuffer(frame.pcm, dtype=np.int16).astype(np.float32)
        samples /= _INT16_FULL_SCALE
        self._buffer = (
            samples if self._buffer is None else np.concatenate((self._buffer, samples))
        )
        while len(self._buffer) >= self._window:
            window, self._buffer = (
                self._buffer[: self._window],
                self._buffer[self._window :],
            )
            self._last = self._run_window(window) >= self._threshold
        return self._last

    def _run_window(self, window: Any) -> float:
        # One Silero inference over a full window: returns the speech probability. Carries the RNN
        # state forward (Silero v5's combined model takes/returns `state`), so successive windows
        # are judged in context, not independently.
        assert self._session is not None
        prob, self._state = self._session.run(
            None,
            {
                "input": window.reshape(1, -1),
                "state": self._state,
                "sr": self._sr_arg,
            },
        )
        return float(prob[0][0])

    def _ensure_session(self) -> Any:
        """Build the ONNX session and zeroed RNN state on first use; return the ``numpy`` module.

        Lazy, Pi-only imports — kept out of module scope so this file loads off-Pi (P5, ADR-008).
        Neither library ships stubs; mypy resolves them via ignore_missing_imports."""
        import numpy as np

        if self._session is None:
            import onnxruntime

            # Single-threaded, non-spinning — and both halves are load-bearing on a 4-core Pi.
            #
            # ONNX Runtime defaults to one intra-op thread PER CORE and, between inferences,
            # those threads **spin-wait** rather than sleep. For Silero that is a catastrophic
            # default: the model is tiny (a 32 ms window, ~0.4 ms per call) so the parallelism
            # buys nothing, while the spin burns three cores permanently. Measured on the Pi at
            # the #106 gate: 306% CPU, 11m28s of CPU in 3m44s of wall clock, with the audio loop
            # starved to the point that the robot stopped hearing anything at all.
            #
            # It went unnoticed through the sealed M4 gate because nothing else wanted the CPU
            # there — the loopback had no websocket, no playback stream and no resampling to
            # compete with. M5 put real work on the other cores and the starvation surfaced.
            #
            # The MiniLM embedder needs the same treatment with a DIFFERENT thread count —
            # see avid/adapters/embedder.py (#168). Any new ONNX session needs this block, and
            # needs its own measurement rather than a copy of either value.
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            # Belt and braces: even at one thread, the pool spins between calls unless told not to.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self._session = onnxruntime.InferenceSession(
                str(self._model_path), sess_options=options
            )
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._sr_arg = np.array(self._sample_rate, dtype=np.int64)
        return np
