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
from collections.abc import Sequence
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
    FakeFactRepository,
    FakeMicrophone,
    FakeServiceNotifier,
    FakeServo,
    FakeSpeaker,
    FakeTextModel,
    FakeVoiceActivityDetector,
    FramebufferDisplay,
    HealthServer,
    HybridRetriever,
    OpenAIRealtimeClient,
    OpenAiTextModel,
    Pca9685Servo,
    Picamera2Camera,
    ReplayRealtimeClient,
    SileroVad,
    SqliteFactRepo,
    SystemClock,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import Axis
from avid.core.ports import (
    Camera,
    Clock,
    Display,
    Embedder,
    EventBus,
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
from avid.domain import ScoreWeights
from avid.services import (
    AffectService,
    AudioService,
    ConversationService,
    CostMeterService,
    CueBank,
    ExpressionService,
    MemoryService,
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
    same playback params from ``[speaker]`` (P7), so the play/stop contract behaves identically.
    Any other value fails loudly rather than silently doing nothing.
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


def _build_embedder(config: Config) -> Embedder:
    """Select the ``Embedder`` adapter named by ``[adapters] embedder`` (#118, SDS §9.3).

    ``fake`` is the laptop/sim default — the stdlib, dependency-free :class:`FakeEmbedder`, the CI
    embedder and simulator (P6); ``local_minilm`` is the real ONNX all-MiniLM-L6-v2 adapter (a later
    issue, its ``onnxruntime``/``numpy`` in the ``memory`` extra, ADR-008). The vector width is
    injected from ``[memory] dimensions`` (P7).

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
        ):  # pragma: no cover - real ONNX adapter lands in a later issue
            raise NotImplementedError(
                "embedder adapter 'local_minilm' is not available yet — only 'fake' exists "
                "(#118 ships the port + fake; the ONNX adapter follows)"
            )
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
                instructions=config.ai.instructions,
                max_output_tokens=config.ai.max_output_tokens,
                turn_detection=config.ai.turn_detection.model_dump(),
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
    realtime: RealtimeClient,
    embedder: Embedder,
    text_model: TextModel,
    fact_store: FactRepository,
    retriever: Retriever,
    cues: CueBank,
    config: Config,
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
    the retriever, the embedder and the text model as **ports** (P2); ``main`` built the concretes.
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
        loopback=False,
    )
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=realtime,
        sink=audio,
        cues=cues,
        session_idle_close_s=config.gate.session_idle_close_s,
    )
    # The cost meter (#105, SDS §6.10.6): a reactive consumer of conversation.turn_ended — the
    # observability subscriber the §9.1.3 catalog already lists for that fact. Owns no task, so
    # like the two faces it is wired for its subscription and then dropped. Rates are keyed by the
    # injected model name (a model swap stays a config edit); no vendor, no device (P1/P5).
    cost_meter = CostMeterService(bus=bus, model=config.ai.model)
    # The memory service (#122): the sole writer/reader of persistent facts, reached by direct call, so
    # its subscriptions() is empty — it appears in the loop only for uniformity. Injected the store,
    # index, embedder and text model as ports (P2); it owns their rebuild/close lifecycle.
    memory = MemoryService(
        bus=bus,
        clock=clock,
        repo=fact_store,
        retriever=retriever,
        embedder=embedder,
        text_model=text_model,
        supersession_threshold=config.memory.supersession_threshold,
        supersession_k=config.memory.supersession_k,
        top_facts_max=config.memory.top_facts_max,
        top_facts_token_budget=config.memory.top_facts_token_budget,
    )
    for service in (affect, expression, audio, conversation, cost_meter, memory):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    # The services that own tasks need lifecycle management: MemoryService's boot rebuild + store close,
    # AudioService's mic loop, ConversationService's per-session pump/mic/idle. Memory is started first so
    # the index is ready before a session ever asks for top_facts. The reactive services (the two faces,
    # the cost meter) own no task and are kept alive by their bound-method subscriptions above.
    return (memory, audio, conversation)


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
    embedder = _build_embedder(config)
    fact_store = _build_fact_repository(config, clock=clock)
    text_model = _build_text_model(config)
    realtime = _build_realtime(config, clock=clock)
    cues = _build_cue_bank(config, speaker=speaker)
    notifier = _build_notifier(config)
    health = HealthServer(bind=config.api.bind, port=config.api.port)
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
        "embedder": True,
        "fact_store": True,
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
        microphone=microphone,
        speaker=speaker,
        vad=vad,
        realtime=realtime,
        embedder=embedder,
        text_model=text_model,
        fact_store=fact_store,
        retriever=retriever,
        cues=cues,
        config=config,
    )
    # ``camera`` and ``servo`` are still constructed only to realize the switch and appear in the health
    # map: driving the camera is the vision service's job (M8) and moving the servo is MotionService's
    # (M9) — both later issues. The store, embedder, retriever and text model are now **owned** by
    # ``MemoryService`` (#122, wired above), so they are no longer held here. ``display`` (AVID-73) and
    # ``microphone``/``speaker``/``vad`` (AVID-89) left this list earlier; their services own them.
    _ = camera
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
