"""Composition-root wiring in main.py (AVID-14, AVID-73).

Covers the adapter switch and the build/delegate glue cross-platform, without waiting
for a real signal — the full boot-to-SIGTERM path is in ``tests/e2e/test_boot.py``.

Since AVID-73 this file also owns the two halves of the service wiring: that ``_run``
*registers* the declared subscriptions before the bus starts, and that the registered graph
actually *renders* on boot-to-IDLE. Both are needed — a correct helper called from nowhere
would pass the second and fail the first.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from avid.adapters import (
    FakeCamera,
    FakeClock,
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
    HybridRetriever,
    OpenAIRealtimeClient,
    OpenAiTextModel,
    ReplayRealtimeClient,
    SqliteEpisodeStore,
    SqliteFactRepo,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus, OverflowPolicy
from avid.core.hal import Axis, DisplayFrame
from avid.core.ports import AffectTools
from avid.core.state_manager import StateManager
from avid.domain import (
    AffectChanged,
    AudioCaptureResumed,
    AudioCaptureStalled,
    AudioPlaybackFinished,
    AudioSpeechEnded,
    AudioSpeechStarted,
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    StateTransitioned,
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemStarted,
    VisionPresenceGained,
    VisionPresenceLost,
)
from avid.main import (
    _build_camera,
    _build_cue_bank,
    _build_display,
    _build_embedder,
    _build_episode_store,
    _build_face_detector,
    _build_fact_repository,
    _build_microphone,
    _build_notifier,
    _build_realtime,
    _build_retriever,
    _build_servo,
    _build_speaker,
    _build_text_model,
    _build_vad,
    _wire_services,
    main,
)
from avid.services import (
    TOOL_SCHEMAS,
    AffectService,
    AudioService,
    BehaviorService,
    ConversationService,
    CueBank,
    EpisodeRecorder,
    MemoryService,
    MotionService,
    PresenceService,
)

# The exact subscriber graph the composition root is expected to build (SDS §9.1.3). Spelled
# out rather than derived from the services, so that a service silently dropping or renaming
# a subscription fails here instead of quietly agreeing with itself.
_EXPECTED_SUBSCRIPTIONS = {
    "AffectService.state_transitioned",
    "ExpressionService.affect_changed",
    "ExpressionService.state_transitioned",
    "ConversationService.speech_started",
    "ConversationService.speech_ended",
    "ConversationService.playback_finished",
    # #239: the second turn origin, a declared seam from M5 until M10.
    "ConversationService.trigger_fired",
    "CostMeterService.turn_ended",
    # EpisodeRecorder (#123): the write-only §7.5 transcript observer of the four conversation.* facts
    "EpisodeRecorder.turn_started",
    "EpisodeRecorder.user_transcribed",
    "EpisodeRecorder.assistant_responded",
    "EpisodeRecorder.turn_ended",
    # PresenceService (#223) subscribes to NOTHING — it is the only clock-driven service
    # (§3.6.1, "polls camera port"), so it contributes no edge to this graph. Its absence
    # here is the assertion; tests/services/test_presence.py asserts it from the other side.
    #
    # BehaviorService (#237) is the opposite extreme: it hears from nearly every other service,
    # because §9.1.3 lists it against twelve events rather than the three §10's prose implies.
    # Shipping only the fire path would have left it structurally incomplete against its own
    # catalog row — the gap M8's epic caught for PresenceService before it shipped.
    "BehaviorService.system_started",
    "BehaviorService.state_transitioned",
    "BehaviorService.degraded_entered",
    "BehaviorService.degraded_exited",
    "BehaviorService.presence_gained",
    "BehaviorService.presence_lost",
    "BehaviorService.speech_started",
    "BehaviorService.speech_ended",
    "BehaviorService.assistant_responded",
    "BehaviorService.user_transcribed",
    "BehaviorService.fact_stored",
    "BehaviorService.fact_superseded",
    "BehaviorService.fact_deleted",
    # #347: whether the microphone is delivering at all. §10.5 turns silence into a verdict about
    # the user, and silence has three causes — the user chose not to answer, the robot never spoke
    # (#337), or the robot could not hear. Only the first is an ignore.
    "BehaviorService.capture_stalled",
    "BehaviorService.capture_resumed",
    # ObservabilityService (#242): the three §9.1.3 rows whose Observability subscriber existed in
    # the catalog and nowhere else — state.transitioned's is even tagged (M10). Note what is NOT
    # here: behavior.proactive_delivered/_suppressed, whose audit trail is the proactive_log table
    # (§10.6). Two counts of one fact is one count too many.
    "ObservabilityService.state_transitioned",
    "ObservabilityService.trigger_fired",
    "ObservabilityService.trigger_disabled",
    # MotionService (#203): the other arm of §3.7.2's fan-out, and the third subscriber of
    # affect.changed after ExpressionService. Its catalog row already named it — "MotionService
    # (M9)" — so this is that tag discharged, not a new edge.
    "MotionService.affect_changed",
    # And the three motion.* rows, whose only §9.1.3 subscriber is Observability. Without them
    # the events publish into an empty room and #207's AC-3 — preemption confirmed "by log AND
    # by eye" — has no log to read.
    "ObservabilityService.gesture_started",
    "ObservabilityService.gesture_completed",
    "ObservabilityService.gesture_preempted",
}

# The 2 DoF rig both shipped profiles declare (#200) — built here rather than loaded so these
# wiring tests do not silently start depending on a TOML they are not about.
_AXES = (
    Axis(name="pan", channel=0, min_deg=30.0, max_deg=150.0),
    Axis(name="tilt", channel=13, min_deg=60.0, max_deg=120.0),
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIM_TOML = _REPO_ROOT / "config" / "sim.toml"
_PI_TOML = _REPO_ROOT / "config" / "pi.toml"


def test_build_display_selects_fake() -> None:
    config = load_config(_SIM_TOML)
    assert isinstance(_build_display(config), FakeDisplay)


def test_build_camera_selects_fake() -> None:
    # sim.toml (and pi.toml) set camera = "fake"; the picamera2 branch needs the Pi and
    # is proven by the on-hardware contract run (AVID-51).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_camera(config), FakeCamera)


def test_build_servo_selects_fake() -> None:
    # sim.toml (and pi.toml) set servo = "fake"; the pca9685 branch needs the Pi and is
    # proven by the on-hardware contract run (AVID-52 / #57).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_servo(config), FakeServo)


def test_build_servo_reports_every_declared_axis() -> None:
    """#200: the composition root turns ``[[servo.axes]]`` into what ``Servo.axes`` reports.

    This is the whole inventory path in one assertion. §3.9.3 makes the adapter's report the
    authority the gesture planner negotiates against, so an axis that is declared and does not
    arrive here is an axis the robot will never move — and the failure looks like a planner bug
    rather than a wiring one.

    Both shipped profiles are checked, then a three-axis rig the files do not contain, because
    "reads the two we ship" and "reads whatever is declared" are different claims and only the
    second is what §3.9.3 promises."""
    for profile in (_SIM_TOML, _PI_TOML):
        servo = _build_servo(load_config(profile))
        assert [(a.name, a.channel) for a in servo.axes] == [
            ("pan", 0),
            ("tilt", 13),
        ], profile

    six_dof = Config.model_validate(
        {
            "servo": {
                "axes": [
                    {"name": "pan", "channel": 0},
                    {"name": "tilt", "channel": 13},
                    {"name": "roll", "channel": 7, "min_deg": 80.0, "max_deg": 100.0},
                ]
            }
        }
    )
    servo = _build_servo(six_dof)
    assert [a.name for a in servo.axes] == ["pan", "tilt", "roll"]
    # The reach travels with the axis: it is what the adapter clamps to, per axis (§3.9.1).
    assert servo.axes[2].min_deg == 80.0 and servo.axes[2].max_deg == 100.0


def test_build_microphone_selects_fake() -> None:
    # sim.toml (and pi.toml) set microphone = "fake"; the alsa branch needs the Pi and is
    # proven by the on-hardware contract run (AVID-53 / #57).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_microphone(config), FakeMicrophone)


def test_build_speaker_selects_fake() -> None:
    # sim.toml (and pi.toml) set speaker = "fake"; the alsa branch needs the Pi and is
    # proven by the on-hardware contract run (AVID-54 / #57).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_speaker(config), FakeSpeaker)


def test_build_vad_selects_fake() -> None:
    # sim.toml (and pi.toml) set vad = "fake"; the silero branch needs the Pi's onnxruntime
    # model and is proven by the on-hardware M4 gate (AVID-77 / #91).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_vad(config), FakeVoiceActivityDetector)


def test_build_face_detector_selects_fake() -> None:
    # sim.toml (and pi.toml) set face_detector = "fake"; the yunet branch needs the Pi's
    # onnxruntime and a provisioned model blob, and is proven by the M8 gate (#221 / #226).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_face_detector(config), FakeFaceDetector)


def test_build_embedder_selects_fake_at_the_configured_dimension() -> None:
    # sim.toml (and pi.toml) set embedder = "fake"; the local_minilm ONNX branch is real now (#119)
    # but Pi-gated/pragma-excluded — CI never builds it. The AC-6 guard's happy path — dimensions
    # match [memory] dimensions — runs here (the mismatch branch is only reachable by a real
    # fixed-dim model, so it too is pragma-excluded).
    config = load_config(_SIM_TOML)
    embedder = _build_embedder(config)
    assert isinstance(embedder, FakeEmbedder)
    assert embedder.dimensions == config.memory.dimensions


def test_build_fact_repository_selects_fake_for_sim() -> None:
    # sim.toml defaults [adapters] store = "fake" → the in-memory FakeFactRepository.
    config = load_config(_SIM_TOML)
    assert isinstance(
        _build_fact_repository(config, clock=FakeClock()), FakeFactRepository
    )


async def test_build_fact_repository_selects_sqlite_when_configured() -> None:
    # The real file-backed store branch: SQLite runs everywhere, so this is not Pi-gated. The
    # connection opens lazily, so constructing it here touches no file.
    config = load_config(_SIM_TOML)
    sqlite_config = config.model_copy(
        update={"adapters": config.adapters.model_copy(update={"store": "sqlite"})}
    )
    repo = _build_fact_repository(sqlite_config, clock=FakeClock())
    assert isinstance(repo, SqliteFactRepo)
    await repo.aclose()


def test_build_episode_store_selects_fake_for_sim() -> None:
    # #123: the episode store reuses the [adapters] store switch; sim.toml defaults it to "fake".
    config = load_config(_SIM_TOML)
    assert isinstance(_build_episode_store(config, clock=FakeClock()), FakeEpisodeStore)


async def test_build_episode_store_selects_sqlite_when_configured() -> None:
    # The same switch as the fact store picks the file-backed episode store (one DB file, #123).
    config = load_config(_SIM_TOML)
    sqlite_config = config.model_copy(
        update={"adapters": config.adapters.model_copy(update={"store": "sqlite"})}
    )
    store = _build_episode_store(sqlite_config, clock=FakeClock())
    assert isinstance(store, SqliteEpisodeStore)
    await store.aclose()


async def test_build_retriever_wires_the_store_and_embedder_from_config() -> None:
    config = load_config(_SIM_TOML)
    clock = FakeClock()
    repo = _build_fact_repository(config, clock=clock)
    embedder = _build_embedder(config)
    bus = AsyncioEventBus(clock=clock)
    retriever = _build_retriever(
        config, repo=repo, embedder=embedder, bus=bus, clock=clock
    )
    assert isinstance(retriever, HybridRetriever)
    await repo.aclose()


def test_build_text_model_selects_fake_for_sim() -> None:
    # sim.toml defaults [adapters] text_model = "fake" → the deterministic §7.8 supersession judge.
    config = load_config(_SIM_TOML)
    assert isinstance(_build_text_model(config), FakeTextModel)


def test_build_text_model_selects_openai_with_the_injected_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # text_model = "openai" builds the real chat-completions client (#121), fed the pinned [ai] text_model
    # snapshot and the key read once from the env as a SecretStr (P7). Construction only — the openai SDK
    # is imported lazily on the first call, so this stays network-free.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    toml = tmp_path / "openai.toml"
    toml.write_text('[adapters]\ntext_model = "openai"\n', encoding="utf-8")
    config = load_config(toml)
    assert isinstance(_build_text_model(config), OpenAiTextModel)


def test_build_text_model_openai_without_a_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The 'openai' text adapter needs OPENAI_API_KEY; absent, the composition root refuses loudly rather
    # than constructing a keyless client that would fail obscurely on the first call (AC-6, like realtime).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = tmp_path / "openai.toml"
    toml.write_text('[adapters]\ntext_model = "openai"\n', encoding="utf-8")
    config = load_config(toml)
    with pytest.raises(RuntimeError):
        _build_text_model(config)


def test_build_realtime_selects_replay() -> None:
    # sim.toml (and pi.toml) set realtime = "replay": the replay client is built from
    # [realtime] session_dir on the injected clock (#101), no key, no network.
    config = load_config(_SIM_TOML)
    client = _build_realtime(config, clock=FakeClock())
    assert isinstance(client, ReplayRealtimeClient)


def test_build_realtime_selects_openai_with_the_injected_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # realtime = "openai" builds the real WSS client (#105), fed the key read once from the env
    # as a SecretStr (P7). Construction only — no connect, so this stays network-free.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    toml = tmp_path / "openai.toml"
    toml.write_text('[adapters]\nrealtime = "openai"\n', encoding="utf-8")
    config = load_config(toml)
    client = _build_realtime(config, clock=FakeClock())
    assert isinstance(client, OpenAIRealtimeClient)


def test_build_realtime_openai_without_a_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The 'openai' adapter needs OPENAI_API_KEY; absent, the composition root refuses loudly
    # rather than constructing a keyless client that would fail obscurely at connect (AC-3).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = tmp_path / "openai.toml"
    toml.write_text('[adapters]\nrealtime = "openai"\n', encoding="utf-8")
    config = load_config(toml)
    with pytest.raises(RuntimeError):
        _build_realtime(config, clock=FakeClock())


def test_build_realtime_openai_seeds_the_tools_and_capability_instructions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #125: the openai client is fed the three §6.6 tool declarations (the recall/forget/remember_fact
    # schemas, static cached prefix) and the §7.6 capability text appended to the base instructions.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    toml = tmp_path / "openai.toml"
    toml.write_text('[adapters]\nrealtime = "openai"\n', encoding="utf-8")
    config = load_config(toml)
    client = _build_realtime(config, clock=FakeClock())
    assert isinstance(client, OpenAIRealtimeClient)
    assert client._tools == TOOL_SCHEMAS  # the §6.6 declarations reached the client
    # the §7.6 capability layer is appended to the base identity prompt (AC-4)
    assert client._instructions.startswith(config.ai.instructions)
    assert "anything you inferred rather than were told" in client._instructions


def test_build_cue_bank_uses_the_injected_cues_dir() -> None:
    # CueBank is handed the shared speaker and the [cues] dir (P7); ConversationService is its
    # only consumer (SDS §6.9).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_cue_bank(config, speaker=FakeSpeaker()), CueBank)


def test_build_notifier_selects_fake_for_sim() -> None:
    # sim.toml omits [adapters] notifier -> the fake default (no supervisor).
    config = load_config(_SIM_TOML)
    assert isinstance(_build_notifier(config), FakeServiceNotifier)


def test_build_notifier_selects_systemd_for_pi() -> None:
    # pi.toml sets notifier = "systemd" -> the real sd_notify adapter. With no
    # $NOTIFY_SOCKET in the env it holds no socket (no-op), which is the correct
    # off-systemd behaviour and keeps this test hardware- and platform-free.
    config = load_config(_PI_TOML)
    assert isinstance(_build_notifier(config), SystemdNotifier)


def test_main_wires_and_delegates_to_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the run loop so main() builds the adapters + bus and returns without waiting
    # for a signal. Exercises load_config -> _run -> lifecycle.run on every platform.
    captured: dict[str, Any] = {}

    async def _stub_run(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("avid.core.lifecycle.run", _stub_run)

    assert main(["--config", str(_SIM_TOML)]) == 0
    assert captured["adapter_health"] == {
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
    assert captured["bus"] is not None
    assert captured["clock"] is not None
    assert isinstance(captured["notifier"], FakeServiceNotifier)
    assert captured["health"] is not None
    assert captured["watchdog_interval_s"] == 15.0
    # The lifecycle-managed services — the ones that own a task: MemoryService (boot rebuild +
    # store close, started first so the index is ready), AudioService's mic loop,
    # ConversationService's per-session pump/mic/idle, EpisodeRecorder's prune loop + store
    # close (#123), and PresenceService's capture loop + the vision thread pool it drains
    # (#223) — are handed to the lifecycle to start/stop; the reactive services (the two
    # faces, the cost meter) are not. See ``_wire_services``.
    assert [type(s) for s in captured["services"]] == [
        MemoryService,
        AudioService,
        ConversationService,
        EpisodeRecorder,
        PresenceService,
        BehaviorService,
        # MotionService owns the in-flight gesture task, so it is returned like the rest —
        # and it is LAST because lifecycle.run stops in reverse (§9.2). Its stop() relaxes
        # every channel, and a servo left energised is the one failure that outlives the
        # process, so it should be the first thing unwound rather than waiting behind a
        # database close.
        MotionService,
    ]


def test_main_capture_flag_routes_to_capture_not_the_run_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --capture NAME records a session instead of running the robot (#105). Stub the live capture
    # (it needs a network + key) and assert main() dispatches to it with the parsed name/seconds,
    # never touching lifecycle.run. This covers the CLI wiring without a socket.
    captured: dict[str, Any] = {}

    async def _stub_capture(config: Any, *, name: str, seconds: int) -> int:
        captured["name"] = name
        captured["seconds"] = seconds
        return 0

    monkeypatch.setattr("avid.main._capture", _stub_capture)
    assert (
        main(["--config", str(_SIM_TOML), "--capture", "demo", "--seconds", "5"]) == 0
    )
    assert captured == {"name": "demo", "seconds": 5}


def test_main_registers_the_service_subscriptions_before_starting_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVID-73 AC-1: ``_run`` wires the services, and does it while the bus is still cold.

    Stubbing ``lifecycle.run`` is what makes this observable: ``run`` is where
    ``async with bus:`` lives, so with it stubbed the bus never starts and the declaration
    graph can be read afterwards. That the graph is non-empty at this point *is* the proof of
    ordering — ``subscribe()`` raises ``RuntimeError`` after start, so a graph this complete
    could not exist if registration had been moved below the handoff.
    """
    captured: dict[str, Any] = {}

    async def _stub_run(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("avid.core.lifecycle.run", _stub_run)
    assert main(["--config", str(_SIM_TOML)]) == 0

    bus = captured["bus"]
    # Every event type the wired services care about, and nothing else: the two reactive faces,
    # ConversationService's ``audio.speech_started`` origin + ``audio.speech_ended`` (#102) + its
    # ``audio.playback_finished`` barge-in feed (#104), the cost meter's ``conversation.turn_ended``
    # (#105), and EpisodeRecorder's four ``conversation.*`` facts (#123 — ``turn_started`` /
    # ``user_transcribed`` / ``assistant_responded`` new here; ``turn_ended`` shared with the meter).
    assert set(bus._subs) == {
        AffectChanged,
        StateTransitioned,
        AudioSpeechStarted,
        AudioSpeechEnded,
        AudioPlaybackFinished,
        ConversationTurnStarted,
        ConversationUserTranscribed,
        ConversationAssistantResponded,
        ConversationTurnEnded,
        # BehaviorService's own feeds (#237, §9.1.3) — the world it reconstructs a PolicyContext
        # from, since every service that owns a piece of that state keeps it private (P5).
        SystemStarted,
        SystemDegradedEntered,
        SystemDegradedExited,
        VisionPresenceGained,
        VisionPresenceLost,
        MemoryFactStored,
        MemoryFactSuperseded,
        MemoryFactDeleted,
        BehaviorTriggerFired,
        BehaviorTriggerDisabled,
        # ...and whether the robot can hear at all (#347). Without these, an unanswered proactive
        # turn is recorded as the user ignoring it even when the microphone stopped delivering
        # frames — and three of those disable proactivity and blame the user for it.
        AudioCaptureStalled,
        AudioCaptureResumed,
        # The three motion.* rows (#203). New event TYPES here, unlike MotionService's own
        # affect.changed subscription — which added a subscriber to a type that was already
        # in this set, and so would have been invisible in this assertion.
        MotionGestureStarted,
        MotionGestureCompleted,
        MotionGesturePreempted,
    }

    subs = [sub for subs in bus._subs.values() for sub in subs]
    assert {sub.name for sub in subs} == _EXPECTED_SUBSCRIPTIONS
    # DROP_OLDEST for the edges where only the latest reading is worth acting on (§9.1.3).
    # BehaviorService's memory + system feeds are DROP_NEWEST, matching the catalog's own column:
    # a dropped `memory.fact_stored` is a routine that never becomes a schedule, so the OLDEST
    # queued one is the one worth keeping.
    _drop_newest = {
        "ConversationService.trigger_fired",
        "BehaviorService.system_started",
        "BehaviorService.degraded_entered",
        "BehaviorService.degraded_exited",
        "BehaviorService.fact_stored",
        "BehaviorService.fact_superseded",
        "BehaviorService.fact_deleted",
    }
    for sub in subs:
        expected = (
            OverflowPolicy.DROP_NEWEST
            if sub.name in _drop_newest
            else OverflowPolicy.DROP_OLDEST
        )
        assert sub.policy is expected, sub.name


def test_wire_services_injects_the_memory_port_into_conversation() -> None:
    """#125: ``_wire_services`` builds ``MemoryService`` before ``ConversationService`` and injects it
    as the latter's ``MemoryTools`` port, so the §6.6 tool dispatch reaches memory by direct call. The
    returned lifecycle tuple is ``(memory, audio, conversation)``, so ``conversation._memory`` is the
    very ``MemoryService`` at index 0 — the wiring, not a fresh instance. This adds no bus edge (tool
    dispatch rides the Realtime pump), so the subscription assertions elsewhere are unchanged."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock)
    config = load_config(_SIM_TOML)
    fact_store = FakeFactRepository(clock=clock)
    embedder = FakeEmbedder()
    retriever = _build_retriever(
        config, repo=fact_store, embedder=embedder, bus=bus, clock=clock
    )
    services = _wire_services(
        bus=bus,
        clock=clock,
        state=state,
        display=FakeDisplay(
            out_dir=Path(config.display.frames_dir), resolution=(64, 48)
        ),
        servo=FakeServo(axes=_AXES),
        microphone=FakeMicrophone(
            sample_rate=16000, channels=1, chunk_ms=20, pcm=b"\x00\x00"
        ),
        speaker=FakeSpeaker(),
        vad=FakeVoiceActivityDetector(),
        camera=FakeCamera(width=32, height=24, fps=5),
        face_detector=FakeFaceDetector(),
        vision_pool=ThreadPoolExecutor(max_workers=1),
        realtime=ReplayRealtimeClient(clock=clock, timeline=()),
        embedder=embedder,
        text_model=FakeTextModel(),
        fact_store=fact_store,
        retriever=retriever,
        episode_store=FakeEpisodeStore(clock=clock),
        trigger_store=FakeTriggerStore(clock=clock),
        cues=CueBank(speaker=FakeSpeaker(), asset_dir=None),
        config=config,
    )
    memory, _audio, conversation, _episode, presence, _behavior, motion = services
    assert isinstance(memory, MemoryService)
    assert isinstance(conversation, ConversationService)
    # PresenceService owns a loop, so it is returned for the lifecycle to start/stop — the
    # same reason AudioService is (#223, SDS §9.2). Asserted positionally here because the
    # tuple's shape is the contract lifecycle.run consumes.
    assert isinstance(presence, PresenceService)
    # MotionService is returned for the same reason and asserted positionally for the same
    # reason: the tuple's shape is the contract lifecycle.run consumes (#203).
    assert isinstance(motion, MotionService)
    # the ConversationService names the port; the concrete injected is the wired MemoryService
    assert conversation._memory is memory
    # ...and the same for AffectTools (AVID-214). The wired AffectService is not in the returned
    # tuple — it owns no task and is kept alive by its bound-method subscription — so this is
    # asserted through the service that holds it: it must be an AffectService, satisfying the
    # port structurally, and not some other object that happens to have the method.
    assert isinstance(conversation._affect, AffectService)
    assert isinstance(conversation._affect, AffectTools)


def test_the_set_affect_wire_adds_no_subscription() -> None:
    """AVID-214 is a *tool* wire, so the subscriber graph must be unchanged.

    Asserted explicitly rather than left to the exact-set assertion elsewhere in this file, for
    the reason #125 asserted the same thing: a tool that quietly grew a subscription would be a
    second place the face could be driven from, and the §9.1.5 drift check would then be
    describing a graph nobody intended."""
    clock = FakeClock()
    affect = AffectService(bus=AsyncioEventBus(clock=clock), clock=clock)

    assert [sub.name for sub in affect.subscriptions()] == [
        "AffectService.state_transitioned"
    ]


class _SignallingDisplay(FakeDisplay):
    """``FakeDisplay`` plus an awaitable "a frame landed" signal.

    Instrumentation, not a second fake: it still encodes and writes a real PNG through the
    real adapter. Needed because ``ready`` fires when the lifecycle has *published*
    ``state.transitioned``, not when the bus worker has finished handling it — asserting on
    the frame count at ``ready`` would be a race that passes locally and fails on CI.
    """

    def __init__(self, *, out_dir: Path) -> None:
        super().__init__(out_dir=out_dir, resolution=(64, 48))
        self.rendered_once = asyncio.Event()

    async def render(self, frame: DisplayFrame) -> None:
        await super().render(frame)
        self.rendered_once.set()


async def test_the_wired_graph_renders_a_face_on_boot_to_idle(tmp_path: Path) -> None:
    """AVID-73 AC-6: the wiring is not just registered, it draws.

    The real ``lifecycle.run`` against the real bus and the real ``FakeDisplay`` — no stubs,
    no mocks. The chain under test is the whole point of M3: lifecycle publishes
    ``system.started`` -> ``StateManager`` transitions BOOTING -> IDLE and publishes
    ``state.transitioned`` -> ``ExpressionService`` renders the Tier-1 face for IDLE.

    Exactly **one** frame is expected, and the reason is worth stating: ``AffectService`` also
    handles that transition, computes a baseline of IDLE, compares it against a current of
    IDLE and correctly publishes nothing. A second frame here would mean its no-op suppression
    had regressed.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    display = _SignallingDisplay(out_dir=tmp_path)
    state = StateManager(bus=bus, clock=clock)
    config = load_config(_SIM_TOML)
    # The audio loop is constructed here (the real #89 wiring) but deliberately NOT started —
    # ``services=`` is left off ``lifecycle.run`` below — so this stays a pure face-render
    # assertion (the running loop is #90's). A cheap silent ``pcm`` skips FakeMicrophone's tone
    # synth, which under coverage would trip the P8 slow-callback gate at construction. The memory
    # collaborators are built too (MemoryService is wired but, like the audio loop, not started).
    fact_store = FakeFactRepository(clock=clock)
    embedder = FakeEmbedder()
    retriever = _build_retriever(
        config, repo=fact_store, embedder=embedder, bus=bus, clock=clock
    )
    _wire_services(
        bus=bus,
        clock=clock,
        state=state,
        display=display,
        servo=FakeServo(axes=_AXES),
        microphone=FakeMicrophone(
            sample_rate=16000, channels=1, chunk_ms=20, pcm=b"\x00\x00"
        ),
        speaker=FakeSpeaker(out_dir=tmp_path),
        vad=FakeVoiceActivityDetector(),
        camera=FakeCamera(width=32, height=24, fps=5),
        face_detector=FakeFaceDetector(),
        vision_pool=ThreadPoolExecutor(max_workers=1),
        realtime=ReplayRealtimeClient(clock=clock, timeline=()),
        embedder=embedder,
        text_model=FakeTextModel(),
        fact_store=fact_store,
        retriever=retriever,
        episode_store=FakeEpisodeStore(clock=clock),
        trigger_store=FakeTriggerStore(clock=clock),
        cues=CueBank(speaker=FakeSpeaker(out_dir=tmp_path), asset_dir=None),
        config=config,
    )

    shutdown = asyncio.Event()
    ready = asyncio.Event()
    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            state=state,
            adapter_health={"display": True},
            notifier=FakeServiceNotifier(),
            watchdog_interval_s=0,
            shutdown=shutdown,
            ready=ready,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        await asyncio.wait_for(display.rendered_once.wait(), timeout=1.0)
        assert display.frames_rendered == 1
        assert display.frames[0].exists()
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)
