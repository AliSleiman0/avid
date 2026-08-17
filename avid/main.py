"""Composition root and command-line entry point (P3, SDS §3.11, §9.6).

The single place concrete adapters are constructed and wired (P3): everywhere else
depends on ports, not classes. ``main`` parses the CLI, loads the frozen config,
selects fake-vs-real adapters from the ``[adapters]`` block, injects the real
:class:`~avid.adapters.clock.SystemClock` into the bus (retiring the bus's private
default), and runs the lifecycle to IDLE and back out on SIGTERM.

``uv run avid --config config/sim.toml`` is a running robot on a laptop (SDS §3.11.1).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import MutableMapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

from avid import __version__

# The one place Fake*/System* adapters are constructed (P3). CI greps for these
# outside main.py and test fixtures.
from avid.adapters import (
    AlsaMicrophone,
    AlsaSpeaker,
    CapturingRealtimeClient,
    FakeCamera,
    FakeDisplay,
    FakeEmbedder,
    FakeEpisodeStore,
    FakeFaceDetector,
    FakeFactRepository,
    FakeMicrophone,
    FakeServiceNotifier,
    FakeServo,
    FakeSpeaker,
    FakeTextModel,
    FakeTriggerStore,
    FakeVoiceActivityDetector,
    FramebufferDisplay,
    HealthServer,
    HybridRetriever,
    LocalMiniLmEmbedder,
    OnnxFaceDetector,
    OpenAIRealtimeClient,
    OpenAiTextModel,
    Pca9685Servo,
    Picamera2Camera,
    ReplayRealtimeClient,
    SileroVad,
    SqliteEpisodeStore,
    SqliteFactRepo,
    SqliteTriggerStore,
    SystemClock,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import Axis
from avid.core.personality import compose, compose_instructions
from avid.core.ports import (
    Camera,
    Clock,
    Display,
    Embedder,
    EpisodeStore,
    EventBus,
    FaceDetector,
    FactRepository,
    Microphone,
    RealtimeClient,
    Retriever,
    Service,
    ServiceNotifier,
    Servo,
    Speaker,
    TextModel,
    VoiceActivityDetector,
)
from avid.core.state_manager import StateManager
from avid.domain import PolicyLimits, ScoreWeights
from avid.domain.vision import PresenceParams
from avid.services import (
    CAPABILITY_INSTRUCTIONS,
    TOOL_SCHEMAS,
    AffectService,
    AudioService,
    BehaviorService,
    ConversationService,
    CostMeterService,
    CueBank,
    EpisodeRecorder,
    ExpressionService,
    MemoryService,
    ObservabilityService,
    PresenceService,
)

_log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``avid`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="avid",
        description="Pico - an AI Desktop Companion Robot.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"avid {__version__}",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to a TOML config file, e.g. config/sim.toml.",
    )
    parser.add_argument(
        "--capture",
        metavar="NAME",
        help=(
            "Record one live Realtime session into assets/sessions/NAME/ in the replay "
            "fixture format (#105), instead of running the robot. Needs [adapters] realtime "
            "= 'openai' and OPENAI_API_KEY."
        ),
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=60,
        metavar="N",
        help="How long to record in --capture mode (default 60).",
    )
    return parser


def _build_display(config: Config) -> Display:
    """Select the ``Display`` adapter named by ``[adapters] display`` (AVID-14/55).

    ``fake`` is the laptop/sim default — writes eyeball-able PNGs to ``[display] frames_dir``,
    no hardware; ``framebuffer`` pushes XRGB8888 to the Pi panel's framebuffer device (the
    file handle lives inside that adapter, opened lazily on-Pi). Both take the same injected
    ``[display]`` geometry as their :attr:`resolution` (P7). Any other value fails loudly
    rather than silently doing nothing.
    """
    resolution = (config.display.width, config.display.height)
    match config.adapters.display:
        case "fake":
            # frames_dir is injected (P7): the default repo-relative scratch dir on a
            # laptop, but a writable StateDirectory path under the Pi's read-only unit.
            return FakeDisplay(
                out_dir=Path(config.display.frames_dir), resolution=resolution
            )
        case "framebuffer":  # pragma: no cover - needs the Pi (M2 gate #57)
            return FramebufferDisplay(
                device=config.display.device,
                width=config.display.width,
                height=config.display.height,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"display adapter {other!r} is not available — only 'fake' and "
                f"'framebuffer' exist (AVID-55)"
            )


def _build_camera(config: Config) -> Camera:
    """Select the ``Camera`` adapter named by ``[adapters] camera`` (AVID-51).

    ``fake`` is the laptop/sim default — synthetic frames, no hardware; ``picamera2`` is
    the real Pi sensor (the ``picamera2`` import lives inside that adapter, apt/optional,
    ADR-008). Capture geometry is injected from ``[camera]`` (P7). Any other value fails
    loudly rather than silently doing nothing.
    """
    match config.adapters.camera:
        case "fake":
            return FakeCamera(
                width=config.camera.width,
                height=config.camera.height,
                fps=config.camera.fps,
            )
        case "picamera2":  # pragma: no cover - needs the Pi (M2 gate #57)
            return Picamera2Camera(
                width=config.camera.width,
                height=config.camera.height,
                fps=config.camera.fps,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"camera adapter {other!r} is not available — only 'fake' and "
                f"'picamera2' exist (AVID-51)"
            )


def _build_servo(config: Config) -> Servo:
    """Select the ``Servo`` adapter named by ``[adapters] servo`` (AVID-52).

    ``fake`` is the laptop/sim default — a recorded movement trace, no hardware; ``pca9685``
    is the real PCA9685 over I2C (the servo-lib import lives inside that adapter, apt/pip on
    the Pi only, ADR-008). Both are handed the same single ``Axis`` built from ``[servo]``
    (P7), so clamp/cancel/relax behave identically. Any other value fails loudly rather than
    silently doing nothing.
    """
    axes = (
        Axis(
            name=config.servo.name,
            channel=config.servo.channel,
            min_deg=config.servo.min_deg,
            max_deg=config.servo.max_deg,
        ),
    )
    match config.adapters.servo:
        case "fake":
            return FakeServo(axes=axes)
        case "pca9685":  # pragma: no cover - needs the Pi (M2 gate #57)
            return Pca9685Servo(
                axes=axes,
                i2c_address=config.servo.i2c_address,
                min_pulse_us=config.servo.min_pulse_us,
                max_pulse_us=config.servo.max_pulse_us,
                freq_hz=config.servo.freq_hz,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"servo adapter {other!r} is not available — only 'fake' and "
                f"'pca9685' exist (AVID-52)"
            )


def _build_microphone(config: Config) -> Microphone:
    """Select the ``Microphone`` adapter named by ``[adapters] microphone`` (AVID-53).

    ``fake`` is the laptop/sim default — synthesized PCM, no hardware; ``alsa`` captures from
    the ReSpeaker via ALSA (the ``alsaaudio`` import lives inside that adapter, pip-on-Pi only,
    ADR-008). Both are handed the same capture params from ``[microphone]`` (P7), so the stream
    contract behaves identically. Any other value fails loudly rather than silently doing nothing.
    """
    match config.adapters.microphone:
        case "fake":
            return FakeMicrophone(
                sample_rate=config.microphone.sample_rate,
                channels=config.microphone.channels,
                chunk_ms=config.microphone.chunk_ms,
            )
        case "alsa":  # pragma: no cover - needs the Pi (M2 gate #57)
            return AlsaMicrophone(
                device=config.microphone.device,
                sample_rate=config.microphone.sample_rate,
                channels=config.microphone.channels,
                chunk_ms=config.microphone.chunk_ms,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"microphone adapter {other!r} is not available — only 'fake' and "
                f"'alsa' exist (AVID-53)"
            )


def _build_speaker(config: Config) -> Speaker:
    """Select the ``Speaker`` adapter named by ``[adapters] speaker`` (AVID-54).

    ``fake`` is the laptop/sim default — records what it plays and writes eyeball-able WAVs to
    ``[speaker] wav_dir``, no hardware; ``alsa`` plays to the MAX98357 I2S DAC via ALSA (the
    ``alsaaudio`` import lives inside that adapter, pip-on-Pi only, ADR-008). Both are handed the
    same **nominal** playback params from ``[speaker]`` (P7), so the play/stop contract behaves
    identically — nominal because both adapters honour each chunk's own declared format and only
    report a deviation from these (AVID-91). Any other value fails loudly rather than silently
    doing nothing.
    """
    match config.adapters.speaker:
        case "fake":
            # wav_dir is injected (P7): the repo-relative scratch dir on a laptop, but the Pi's
            # writable StateDirectory under its read-only unit (like [display] frames_dir).
            return FakeSpeaker(out_dir=Path(config.speaker.wav_dir))
        case "alsa":  # pragma: no cover - needs the Pi (M2 gate #57)
            return AlsaSpeaker(
                device=config.speaker.device,
                sample_rate=config.speaker.sample_rate,
                channels=config.speaker.channels,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"speaker adapter {other!r} is not available — only 'fake' and "
                f"'alsa' exist (AVID-54)"
            )


def _build_vad(config: Config) -> VoiceActivityDetector:
    """Select the ``VoiceActivityDetector`` named by ``[adapters] vad`` (AVID-77).

    ``fake`` is the laptop/sim default — a scripted speech/silence timeline, no model; ``silero``
    runs Silero v5 via ONNX (the ``onnxruntime``/``numpy`` imports live lazily inside that adapter,
    the Pi-only ``pi`` extra, ADR-008 — so ``main.py`` still imports off-Pi). The real gate is fed
    the local speech-probability ``threshold`` from ``[gate]`` and the capture ``sample_rate`` from
    ``[microphone]`` (P7). Any other value fails loudly rather than silently doing nothing.
    """
    match config.adapters.vad:
        case "fake":
            # default=False: no scripted timeline, so every frame reads as silence — the loop runs
            # and gates cleanly to IDLE under fakes, minting no spurious turns. #90 drives a script.
            return FakeVoiceActivityDetector()
        case "silero":  # pragma: no cover - needs the Pi (M4 gate #91)
            return SileroVad(
                threshold=config.gate.threshold,
                sample_rate=config.microphone.sample_rate,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"vad adapter {other!r} is not available — only 'silero' and "
                f"'fake' exist (AVID-77)"
            )


def _build_face_detector(
    config: Config, *, executor: Executor | None = None
) -> FaceDetector:
    """Select the ``FaceDetector`` named by ``[adapters] face_detector`` (#220, ADR-013).

    ``fake`` is the laptop/sim default — it reads ``FakeCamera``'s scripted presence flag out
    of the frame bytes, so the pair compose into a complete simulator with no model and no
    camera; ``yunet`` is the real YuNet ONNX detector (its ``onnxruntime``/``numpy`` imports
    live lazily inside that adapter, the Pi-only ``pi`` extra, ADR-008 — so ``main.py`` still
    imports off-Pi). Any other value fails loudly rather than silently doing nothing, which
    matters more here than elsewhere: a detector that quietly reports nothing is
    indistinguishable from an empty room.
    """
    match config.adapters.face_detector:
        case "fake":
            # No script: every frame reports the default confidence, so presence is driven
            # entirely by FakeCamera.person_present. #223 drives a script.
            return FakeFaceDetector()
        case "yunet":  # pragma: no cover - needs the Pi (M8 gate #226)
            # The executor is PresenceService's single-thread pool, shared with capture
            # (§3.8.2). Passing none falls back to asyncio.to_thread, which is right for a
            # probe or a one-shot script and wrong for the running robot.
            return OnnxFaceDetector(
                scale=config.vision.detector_scale, executor=executor
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"face_detector adapter {other!r} is not available — only 'yunet' "
                f"and 'fake' exist (#220)"
            )


def _build_embedder(config: Config) -> Embedder:
    """Select the ``Embedder`` adapter named by ``[adapters] embedder`` (#118/#119, SDS §9.3).

    ``fake`` is the laptop/sim default — the stdlib, dependency-free :class:`FakeEmbedder`, the CI
    embedder and simulator (P6); ``local_minilm`` is the real ONNX all-MiniLM-L6-v2 adapter (#119,
    its ``onnxruntime``/``numpy``/``tokenizers`` in the Pi-only ``pi`` extra, ADR-008 — model/tokenizer
    paths take their in-adapter defaults, mirroring ``_build_vad``). The vector width is injected from
    ``[memory] dimensions`` (P7).

    The returned embedder's :attr:`~avid.core.ports.Embedder.dimensions` is asserted equal to
    ``[memory] dimensions`` (AC-6): a fixed-dim real model paired with a mismatched config must fail
    **loudly at startup**, not silently write half-width BLOBs that only surface as a wrong index at
    the M7 gate. The fake takes its width from the same config value, so the guard can only ever fire
    for a real model — but it lives here, at composition, where the mismatch is knowable.
    """
    match config.adapters.embedder:
        case "fake":
            embedder: Embedder = FakeEmbedder(dimensions=config.memory.dimensions)
        case (
            "local_minilm"
        ):  # pragma: no cover - real ONNX adapter, Pi-gated (#119, §14.4)
            embedder = LocalMiniLmEmbedder(dimensions=config.memory.dimensions)
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"embedder adapter {other!r} is not available — only 'fake' and "
                f"'local_minilm' exist (#118)"
            )
    if (
        embedder.dimensions != config.memory.dimensions
    ):  # pragma: no cover - real fixed-dim only
        raise RuntimeError(
            f"embedder produces {embedder.dimensions}-dim vectors but [memory] dimensions is "
            f"{config.memory.dimensions} — a mismatch would corrupt the index (SDS §8.2, #118)"
        )
    return embedder


def _build_fact_repository(config: Config, *, clock: Clock) -> FactRepository:
    """Select the ``FactRepository`` adapter named by ``[adapters] store`` (#117/#120, SDS §8.3).

    ``fake`` is the laptop/sim default — :class:`FakeFactRepository` at ``":memory:"``, the same
    schema and SQL as the real store with no file, so it *is* the simulator (P6); ``sqlite`` is the
    file-backed :class:`SqliteFactRepo` at ``[memory] db_path``. Both run everywhere (SQLite is not
    a device), so neither branch is Pi-gated. The blocking ``sqlite3`` I/O rides the adapter's one
    writer thread (§3.8.2); the connection opens lazily, so building the store here is inert until
    ``MemoryService`` (#122) drives its lifecycle.
    """
    match config.adapters.store:
        case "fake":
            repo: FactRepository = FakeFactRepository(clock=clock)
        case "sqlite":
            repo = SqliteFactRepo(db_path=config.memory.db_path, clock=clock)
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"store adapter {other!r} is not available — only 'sqlite' and "
                f"'fake' exist (#117)"
            )
    return repo


def _build_episode_store(config: Config, *, clock: Clock) -> EpisodeStore:
    """Select the ``EpisodeStore`` adapter for the §7.5 transcript tier (#123, SDS §8.3).

    Reuses the **same** ``[adapters] store`` switch as the fact repository — facts and episodes
    are one SQLite database file, so one real/fake toggle governs both (no separate axis). ``sqlite``
    is the file-backed :class:`SqliteEpisodeStore` at ``[memory] db_path`` (a second connection to
    that file; WAL makes two writers safe, §8.4); ``fake`` is :class:`FakeEpisodeStore` at
    ``":memory:"`` — same schema, no file — the laptop/sim default. Neither is Pi-gated (SQLite is
    not a device); the connection opens lazily, so building it here touches no file.
    """
    match config.adapters.store:
        case "fake":
            store: EpisodeStore = FakeEpisodeStore(clock=clock)
        case "sqlite":
            store = SqliteEpisodeStore(db_path=config.memory.db_path, clock=clock)
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"store adapter {other!r} is not available — only 'sqlite' and "
                f"'fake' exist (#117)"
            )
    return store


def _build_trigger_store(config: Config, *, clock: Clock) -> SqliteTriggerStore:
    """Select the trigger/proactive-log store for §10 (#237, SDS §8.3).

    The **same** ``[adapters] store`` switch again — facts, episodes and triggers are one SQLite
    file, so one real/fake toggle governs all three. Returns the concrete class rather than a port
    because it satisfies *two* Protocols (``TriggerRepository`` and ``ProactiveLog``) and the
    composition root hands the same object to both parameters; the service still depends only on
    the Protocols (P2).
    """
    match config.adapters.store:
        case "fake":
            store: SqliteTriggerStore = FakeTriggerStore(clock=clock)
        case "sqlite":
            store = SqliteTriggerStore(db_path=config.memory.db_path, clock=clock)
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"store adapter {other!r} is not available — only 'sqlite' and "
                f"'fake' exist (#117)"
            )
    return store


def _build_retriever(
    config: Config,
    *,
    repo: FactRepository,
    embedder: Embedder,
    bus: EventBus,
    clock: Clock,
) -> HybridRetriever:
    """Build the §8.5 hybrid retriever over the store + embedder (#120, SDS §7.7).

    A portless concrete adapter (like ``HealthServer``): the memory read path — FTS5 keyword ∪
    cosine over the in-memory numpy index, scored by #116's pure ``rank_candidates``. ``top_k``, the
    recency half-life and the three §7.7 weights are injected from ``[memory]`` (P7, AC-5), never
    literals. Its ``rebuild()`` (boot) and write-through lifecycle are driven by ``MemoryService``
    (#122); here it is built and held so the ``[adapters] store`` switch is realized end-to-end and
    the retriever appears in the health map.
    """
    weights = config.memory.weights
    return HybridRetriever(
        repo=repo,
        embedder=embedder,
        bus=bus,
        clock=clock,
        top_k=config.memory.top_k,
        half_life_days=config.memory.recency_half_life_days,
        weights=ScoreWeights(
            recency=weights.recency,
            importance=weights.importance,
            relevance=weights.relevance,
        ),
    )


def _build_text_model(config: Config) -> TextModel:
    """Select the ``TextModel`` adapter named by ``[adapters] text_model`` (#122, SDS §7.8).

    ``fake`` is the laptop/sim default — :class:`FakeTextModel`, the deterministic §7.8 supersession
    judge (a literal-restatement rule, no network), the P6 fake and simulator; ``openai`` is the real
    HTTPS text client (#121), fed the pinned ``[ai] text_model`` snapshot and the injected key. The cheap
    text model is off the turn path, so it never touches the audio loop (P8). Any other value fails loudly
    rather than silently doing nothing.
    """
    match config.adapters.text_model:
        case "fake":
            return FakeTextModel()
        case "openai":
            # The key is unwrapped once here (P7, SECURITY.md), as ``_build_realtime`` does: read as a
            # SecretStr in load_config, handed to the adapter only to build the client. Absent → refuse
            # loudly rather than construct a keyless client that fails obscurely on the first call.
            if config.openai_api_key is None:
                raise RuntimeError(
                    "text_model adapter 'openai' requires OPENAI_API_KEY in the environment "
                    "(read once as SecretStr, SECURITY.md/P7) — none was injected"
                )
            return OpenAiTextModel(
                api_key=config.openai_api_key.get_secret_value(),
                model=config.ai.text_model,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"text_model adapter {other!r} is not available — only 'openai' and "
                f"'fake' exist (#122)"
            )


def _build_realtime(config: Config, *, clock: Clock) -> RealtimeClient:
    """Select the ``RealtimeClient`` adapter named by ``[adapters] realtime`` (#101/#105).

    ``replay`` is the laptop/sim default and the vendor blast-radius escape hatch (R-10): it
    plays a recorded session (``[realtime] session_dir``, #101) back deterministically on the
    injected ``clock`` — no key, no network, no cost. ``openai`` is the live Realtime WSS client
    (#105 — the ``websockets`` import lives lazily inside that adapter, ADR-008), fed
    model/voice/instructions/turn-detection from ``[ai]`` and the injected key. The whole
    ``ConversationService`` is built and CI-gated against ``replay`` before a single API call.
    Any other value fails loudly rather than silently doing nothing.
    """
    match config.adapters.realtime:
        case "replay":
            return ReplayRealtimeClient.from_dir(
                Path(config.realtime.session_dir), clock=clock
            )
        case "openai":
            # The one place the secret is unwrapped (P7, SECURITY.md, AC-3): read once as a
            # SecretStr in load_config, handed to the adapter only to build the auth header.
            if config.openai_api_key is None:
                raise RuntimeError(
                    "realtime adapter 'openai' requires OPENAI_API_KEY in the environment "
                    "(read once as SecretStr, SECURITY.md/P7) — none was injected"
                )
            return OpenAIRealtimeClient(
                api_key=config.openai_api_key.get_secret_value(),
                model=config.ai.model,
                voice=config.ai.voice,
                # §6.4's static prefix — layers 1, 2 and 3, in that order and assembled in exactly
                # one place (AVID-213). This used to be a hand-rolled concatenation of layers 1
                # and 3 with a comment noting layer 2 was still M6; it now goes through the
                # composer, so the ordering rule lives with the function that owns it rather than
                # here. Layer 4 (the §6.7 memory block) is appended per-session by the adapter and
                # must stay last, or it invalidates the cached prefix behind it (§6.10.3).
                instructions=compose_instructions(
                    identity=config.ai.instructions,
                    personality=compose(config.personality),
                    capabilities=CAPABILITY_INSTRUCTIONS,
                ),
                max_output_tokens=config.ai.max_output_tokens,
                turn_detection=config.ai.turn_detection.model_dump(),
                # Realtime transcribes the user's speech only when asked; without this no
                # UserTranscript is ever produced and the turn arc stalls (§6.2.2).
                transcription_model=config.ai.transcription_model,
                transcription_language=config.ai.transcription_language,
                # The §6.6 tool declarations (#125): recall/forget/remember_fact as JSON Schema,
                # static for the session's life. Vendor-neutral dicts, injected like turn_detection.
                tools=TOOL_SCHEMAS,
            )
        case other:  # pragma: no cover - guards an unreachable literal
            raise NotImplementedError(
                f"realtime adapter {other!r} is not available — only 'openai' and "
                f"'replay' exist (#101)"
            )


def _build_cue_bank(config: Config, *, speaker: Speaker) -> CueBank:
    """Build the degraded-mode :class:`~avid.services.cue_bank.CueBank` (AVID-80, #102).

    ConversationService's first and only consumer (SDS §6.9): it resolves a named cue to a WAV
    under the injected ``[cues] dir`` and plays it through the shared ``Speaker`` port
    (``play_file``), degrading quietly if the assets are missing. The base dir is injected (P7);
    the same ``speaker`` instance drives both AudioService playback and these canned phrases.
    """
    return CueBank(speaker=speaker, asset_dir=Path(config.cues.dir))


def _build_notifier(config: Config) -> ServiceNotifier:
    """Select the ``ServiceNotifier`` named by ``[adapters] notifier`` (AVID-38).

    ``systemd`` gets the real sd_notify adapter, fed the ``$NOTIFY_SOCKET`` address the
    config injected from the env (P7); ``fake`` gets the recording simulator — the
    laptop default, where no supervisor exists.
    """
    match config.adapters.notifier:
        case "fake":
            return FakeServiceNotifier()
        case "systemd":
            return SystemdNotifier(address=config.notify_socket)


def _wire_services(
    *,
    bus: AsyncioEventBus,
    clock: Clock,
    state: StateManager,
    display: Display,
    microphone: Microphone,
    speaker: Speaker,
    vad: VoiceActivityDetector,
    camera: Camera,
    face_detector: FaceDetector,
    vision_pool: Executor,
    realtime: RealtimeClient,
    embedder: Embedder,
    text_model: TextModel,
    fact_store: FactRepository,
    retriever: Retriever,
    episode_store: EpisodeStore,
    trigger_store: SqliteTriggerStore,
    cues: CueBank,
    config: Config,
    adapter_health: MutableMapping[str, bool] | None = None,
) -> Sequence[Service]:
    """Construct the services, register what they *declared*, return the ones with an owned task.

    The inversion is the point: a service says what it wants to hear via
    ``subscriptions()``; the composition root decides whether to grant it. That is what
    keeps the subscriber graph static and knowable for §9.1.5's drift check, and it is why
    this loop lives here rather than inside each service.

    **Ordering is load-bearing — call this before** :func:`lifecycle.run`. That function
    opens ``async with bus:``, which calls ``bus.start()``, and
    :meth:`AsyncioEventBus.subscribe` raises ``RuntimeError`` once started: subscription is
    static-at-composition by design (P3, SDS §3.5.2). Register, then run. Moving this call
    below the handoff turns a boot into a crash.

    Every service is typed on the **ports** (``EventBus``/``Clock``/``Display``/``Microphone``/
    ``Speaker``/``VoiceActivityDetector``), never on a concrete adapter (P2) — which is what lets
    the identical wiring drive the fakes on a laptop and the real HALs on the Pi from one config
    literal. Config values (`[gate]`/`[microphone]`) are injected, never read by the service (P7).

    ``AffectService``, ``ExpressionService`` and ``CostMeterService`` (#105) are purely reactive —
    no owned task, ``start``/``stop`` are no-ops — so they are wired for their subscriptions and
    then dropped: every ``Subscription`` holds a **bound method** that keeps its instance alive for
    the life of the bus. ``AudioService`` and ``ConversationService`` own tasks (the mic loop; the
    per-session pump/mic/idle), so they are **returned** for :func:`lifecycle.run` to ``start``/
    ``stop``. Each ``subscriptions()`` set is small and static — AudioService's is empty (it is
    *not* a ``conversation.*`` subscriber; the assistant-audio seam is the ``TurnSink`` port it
    implements, §9.1.4), ConversationService's is the two ``audio.*`` turn origins plus the
    barge-in feed, and CostMeterService's is the single ``conversation.turn_ended`` it meters.

    ``AudioService`` *is* the real ``TurnSink`` (#103): it is built first and injected as
    ``ConversationService``'s ``sink``, so a turn's audio crosses the two services through the
    port without either importing the other (P5). ``loopback=False`` selects the M5 seam; the #91
    transport-gate demo constructs its own AudioService with ``loopback=True``.

    ``MemoryService`` (#122) is the §9.1.4 exception: it subscribes to **nothing** (so it adds no edge
    to the graph), but it owns the store + index lifecycle — ``start`` rebuilds the §8.5 index — so it is
    **returned** for the lifecycle to ``start``/``stop`` like ``AudioService``. It is handed the store,
    the retriever, the embedder and the text model as **ports** (P2); ``main`` built the concretes. It
    is built **before** ``ConversationService`` and injected into it as the ``MemoryTools`` port (#125),
    so the §6.6 tool dispatch reaches memory by **direct call** (§9.1.4) — ``ConvSvc`` names the port,
    never the service module (P2/P5). Injecting the concrete here does not add a bus edge (the tool
    dispatch rides the Realtime event pump, not the bus), so the subscriber graph is unchanged by #125.
    """
    affect = AffectService(bus=bus, clock=clock)
    expression = ExpressionService(bus=bus, display=display, clock=clock)
    audio = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=microphone,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=config.gate.ring_buffer_ms,
        sample_rate=config.microphone.sample_rate,
        channels=config.microphone.channels,
        silence_hold_ms=config.gate.silence_hold_ms,
        barge_in_margin_db=config.gate.barge_in_margin_db,
        highpass_hz=config.gate.highpass_hz,
        highpass_order=config.gate.highpass_order,
        echo_tail_ms=config.gate.echo_tail_ms,
        loopback=False,
    )
    # The memory service (#122): the sole writer/reader of persistent facts, reached by direct call, so
    # its subscriptions() is empty — it appears in the loop only for uniformity. Injected the store,
    # index, embedder and text model as ports (P2); it owns their rebuild/close lifecycle. Built
    # **before** ConversationService (#125) because that service is injected it as its ``MemoryTools``
    # port — the concrete satisfies the port structurally, and the tool dispatch reaches it by call.
    memory = MemoryService(
        bus=bus,
        clock=clock,
        repo=fact_store,
        retriever=retriever,
        embedder=embedder,
        text_model=text_model,
        supersession_threshold=config.memory.supersession_threshold,
        supersession_k=config.memory.supersession_k,
        forget_relevance_floor=config.memory.forget_relevance_floor,
        forget_k=config.memory.forget_k,
        top_facts_max=config.memory.top_facts_max,
        top_facts_token_budget=config.memory.top_facts_token_budget,
    )
    # BehaviorService before ConversationService, and the order is load-bearing since #243: it
    # satisfies the `BehaviorTools` port that `set_quiet` dispatches against, so conversation
    # takes it as a constructor argument. It is also the service whose subscriptions span every
    # other one's output (§9.1.3) — but it depends on none of them, which is what makes this
    # ordering possible at all.
    # ⚠️ PolicyLimits is assembled here, from config, and passed as a value — the gate never sees
    # a Config object, because a pure function that can read configuration is not a pure function.
    behavior = BehaviorService(
        bus=bus,
        clock=clock,
        state=state,
        triggers=trigger_store,
        proactive_log=trigger_store,
        limits=PolicyLimits(
            quiet_start_minutes=config.behavior.quiet_hours.start_minutes,
            quiet_end_minutes=config.behavior.quiet_hours.end_minutes,
            presence_window_s=config.behavior.presence_window_s,
            ambient_speech_threshold_s=config.behavior.ambient_speech_threshold_s,
            global_cooldown_s=config.behavior.global_cooldown_s,
            daily_budget=config.behavior.daily_budget,
        ),
        timezone=config.behavior.timezone,
        default_cooldown_s=config.behavior.global_cooldown_s,
        hold_open_s=config.behavior.hold_open_s,
        ignore_backoff_multiplier=config.behavior.ignore_backoff_multiplier,
        stale_grace_s=config.behavior.stale_grace_s,
        ignore_streak_limit=config.behavior.ignore_streak_limit,
    )

    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=realtime,
        sink=audio,
        cues=cues,
        # The MemoryTools port for the §6.6 tool dispatch (#125) + §6.7-path-1 pre-injection (#126) —
        # the concrete MemoryService, injected as the port so ConvSvc names no service module (P2/P5).
        memory=memory,
        # AffectService satisfies AffectTools structurally — no inheritance, no edit there.
        # It is built above, before this call, so no reordering was needed (AVID-214).
        affect=affect,
        behavior=behavior,
        session_idle_close_s=config.gate.session_idle_close_s,
        memory_inject_timeout_s=config.gate.memory_inject_timeout_s,
        default_timezone=config.behavior.timezone,
        hold_open_s=config.behavior.hold_open_s,
        think_timeout_s=config.gate.think_timeout_s,
        # Who owns the turn boundary (AVID-194). False — the shipped value — means the local VAD
        # is the only authority and ConversationService commits from its falling edge.
        server_turn_detection=config.ai.turn_detection.server_is_an_authority,
        thinking_delay_ms=config.cues.thinking_delay_ms,
    )
    # The cost meter (#105, SDS §6.10.6): a reactive consumer of conversation.turn_ended — the
    # observability subscriber the §9.1.3 catalog already lists for that fact. Owns no task, so
    # like the two faces it is wired for its subscription and then dropped. Rates are keyed by the
    # injected model name (a model swap stays a config edit); no vendor, no device (P1/P5).
    cost_meter = CostMeterService(bus=bus, model=config.ai.model)
    # The structured tap (#242, §3.12.2). Three §9.1.3 rows had an `Observability` subscriber in the
    # catalog and none in the code — state.transitioned's is even tagged (M10). Owns no task, so
    # like the two faces it is wired for its subscriptions and then dropped.
    observability = ObservabilityService()
    # The episode recorder (#123, SDS §7.5): the write-only transcript observer. Subscribes to the four
    # conversation.* facts and mirrors each into the episodes table, keyed by correlation_id; it
    # publishes nothing (no bus handed to it) and reads nothing back into any flow (AC-4). It owns one
    # task — the 90-day prune loop — so it is returned to the lifecycle like AudioService. The store is
    # injected as the EpisodeStore port (P2), the same real/fake as the fact store (one DB file).
    episode_recorder = EpisodeRecorder(
        clock=clock,
        store=episode_store,
        retention_days=config.memory.episode_retention_days,
        prune_interval_s=config.memory.episode_prune_interval_s,
        prune_batch=config.memory.episode_prune_batch,
    )
    # The presence service (#223, SDS §3.6.1): the only clock-driven service in the system, so its
    # subscriptions() is empty and it appears in the loop below only for uniformity. It is handed the
    # camera and detector as **ports** (P2) plus the one single-thread executor both of them and it
    # share (§3.8.2 — two pools is two cores), and it owns that pool's shutdown. The [vision] filter
    # knobs are injected (P7); the filter itself is a pure domain function so #225 can replay a real
    # recorded hour through the identical code path in CI.
    presence = PresenceService(
        bus=bus,
        clock=clock,
        state=state,
        camera=camera,
        detector=face_detector,
        executor=vision_pool,
        fps=config.vision.fps,
        params=PresenceParams(
            confidence_threshold=config.vision.confidence_threshold,
            gain_window_s=config.vision.gain_window_s,
            lose_window_s=config.vision.lose_window_s,
        ),
        nap_after_s=config.vision.nap_after_s,
        health=adapter_health,
    )
    for service in (
        affect,
        expression,
        audio,
        conversation,
        cost_meter,
        memory,
        episode_recorder,
        presence,
        behavior,
        observability,
    ):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    # The services that own tasks need lifecycle management: MemoryService's boot rebuild + store close,
    # AudioService's mic loop, ConversationService's per-session pump/mic/idle, EpisodeRecorder's prune
    # loop + store close. Memory is started first so the index is ready before a session ever asks for
    # top_facts. The reactive services (the two faces, the cost meter) own no task and are kept alive by
    # their bound-method subscriptions above.
    return (memory, audio, conversation, episode_recorder, presence, behavior)


async def _run(config: Config) -> int:
    """Build the adapters and bus, then hand off to the lifecycle.

    This is the P3 site: :class:`SystemClock`, the display, the notifier, and the
    control-API server are constructed here and nowhere else. The real clock is injected
    into the bus, retiring its private ``_SystemClock`` default.
    """
    clock = SystemClock()
    display = _build_display(config)
    camera = _build_camera(config)
    servo = _build_servo(config)
    microphone = _build_microphone(config)
    speaker = _build_speaker(config)
    vad = _build_vad(config)
    # ONE thread for capture AND inference (SDS §3.8.2, §2.7.1). Built here because P3 governs
    # *construction* and the two adapters that need it are built here too; owned by
    # PresenceService, because it is that service's lifetime that bounds it and its stop() that
    # drains it. A ThreadPoolExecutor is a stdlib resource, not an adapter, so this is not a P2
    # or P3 exception. Two pools would be two cores, and asyncio.to_thread's default pool is
    # sized to the CPU count — which is how a ≤1-core budget is lost without anyone noticing.
    vision_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")
    face_detector = _build_face_detector(config, executor=vision_pool)
    embedder = _build_embedder(config)
    fact_store = _build_fact_repository(config, clock=clock)
    episode_store = _build_episode_store(config, clock=clock)
    trigger_store = _build_trigger_store(config, clock=clock)
    text_model = _build_text_model(config)
    realtime = _build_realtime(config, clock=clock)
    cues = _build_cue_bank(config, speaker=speaker)
    notifier = _build_notifier(config)
    bus = AsyncioEventBus(clock=clock)
    # The one state machine (SDS §3.8.4). Built here so every future service shares this
    # instance rather than growing a private copy — the lifecycle drives it to IDLE.
    state = StateManager(bus=bus, clock=clock)
    # The memory read path (#120): the retriever owns the store + embedder + bus. Built after the bus
    # (it publishes memory.recall_completed); MemoryService (#122) drives its rebuild/close lifecycle.
    retriever = _build_retriever(
        config, repo=fact_store, embedder=embedder, bus=bus, clock=clock
    )
    adapter_health = {
        "clock": True,
        "display": True,
        "camera": True,
        "servo": True,
        "microphone": True,
        "speaker": True,
        "face_detector": True,
        "embedder": True,
        "fact_store": True,
        "episode_store": True,
        "retriever": True,
        "text_model": True,
        "notifier": True,
        "health": True,
    }
    # The services, and their subscriptions, BEFORE the lifecycle starts the bus — see
    # ``_wire_services`` for why that order is not negotiable. This is the line that makes the
    # display a face and the mic/speaker/VAD an audio loop, rather than health-map entries. It
    # returns the services with an owned task (AudioService) for the lifecycle to start/stop.
    services = _wire_services(
        bus=bus,
        clock=clock,
        state=state,
        display=display,
        camera=camera,
        face_detector=face_detector,
        vision_pool=vision_pool,
        microphone=microphone,
        speaker=speaker,
        vad=vad,
        realtime=realtime,
        embedder=embedder,
        text_model=text_model,
        fact_store=fact_store,
        retriever=retriever,
        episode_store=episode_store,
        trigger_store=trigger_store,
        cues=cues,
        config=config,
        adapter_health=adapter_health,
    )
    # The control API is built *after* the services, and that ordering is #244's: §9.5's
    # POST /quiet sets the same state the `set_quiet` tool does, through the same
    # `BehaviorTools` port, so the server needs the behaviour engine to exist first. Two doors,
    # one room — §9.5 calls the route "also reachable via set_quiet tool", and the only way to
    # make that true rather than approximately true is for both to call one method.
    health = HealthServer(
        bind=config.api.bind,
        port=config.api.port,
        behavior=next(s for s in services if isinstance(s, BehaviorService)),
    )
    # ``servo`` is the last adapter still constructed only to realize the switch and appear in the
    # health map — moving it is MotionService's job (M9). ``camera`` and ``face_detector`` left this
    # list at #223: ``PresenceService`` drives them now, which is what the comment here used to
    # promise. The store, embedder, retriever and text model are owned by ``MemoryService`` (#122);
    # ``display`` (AVID-73) and ``microphone``/``speaker``/``vad`` (AVID-89) left earlier.
    _ = servo
    return await lifecycle.run(
        bus=bus,
        clock=clock,
        state=state,
        adapter_health=adapter_health,
        notifier=notifier,
        health=health,
        services=services,
        watchdog_interval_s=config.systemd.watchdog_interval_s,
    )


async def _capture(  # pragma: no cover - live capture needs network + a real key (#105)
    config: Config, *, name: str, seconds: int
) -> int:
    """Record one live Realtime session into ``assets/sessions/<name>/`` (AC-4).

    Wraps whatever ``_build_realtime`` selects (``openai`` for a live recording) in a
    :class:`~avid.adapters.realtime.CapturingRealtimeClient`, forwards mic audio up and drains the
    event stream — which the wrapper writes to the ``replay`` fixture format on close — for
    ``seconds``, then tears down. The recording *is* the fixture ``ReplayRealtimeClient`` plays, so
    replay can never drift from the real API. Network-gated: never exercised in CI (the format
    round-trip is proven offline in ``tests/adapters/test_realtime_capture.py``).
    """
    clock = SystemClock()
    inner = _build_realtime(config, clock=clock)
    microphone = _build_microphone(config)
    out_dir = Path("assets") / "sessions" / name
    client = CapturingRealtimeClient(inner=inner, clock=clock, out_dir=out_dir)
    _log.info("capturing a live session into %s for %ss", out_dir, seconds)
    await client.open()

    async def forward_mic() -> None:
        async for chunk in microphone.stream():
            await client.send_audio(chunk)

    async def drain_events() -> None:
        async for _event in client.events():
            pass

    mic_task = asyncio.create_task(forward_mic(), name="capture.mic")
    drain_task = asyncio.create_task(drain_events(), name="capture.drain")
    try:
        await asyncio.sleep(seconds)
    finally:
        mic_task.cancel()
        drain_task.cancel()
        await client.aclose()  # flushes session.json + the WAVs
    _log.info("capture complete: %s", out_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``avid`` console script.

    Parses args, loads the config (the one place ``OPENAI_API_KEY`` is read), and runs the robot —
    or, with ``--capture NAME``, records one live session into the replay fixture format and exits
    (#105). Returns a process exit code; ``--help``/``--version`` exit 0 via argparse before
    returning here, and a missing ``--config`` exits 2.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.capture is not None:
        return asyncio.run(_capture(config, name=args.capture, seconds=args.seconds))
    return asyncio.run(_run(config))
