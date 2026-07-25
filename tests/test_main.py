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
from pathlib import Path
from typing import Any

import pytest

from avid.adapters import (
    FakeCamera,
    FakeClock,
    FakeDisplay,
    FakeEmbedder,
    FakeFactRepository,
    FakeMicrophone,
    FakeServiceNotifier,
    FakeServo,
    FakeSpeaker,
    FakeTextModel,
    FakeVoiceActivityDetector,
    HybridRetriever,
    OpenAIRealtimeClient,
    OpenAiTextModel,
    ReplayRealtimeClient,
    SqliteFactRepo,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import load_config
from avid.core.event_bus import AsyncioEventBus, OverflowPolicy
from avid.core.hal import DisplayFrame
from avid.core.state_manager import StateManager
from avid.domain import (
    AffectChanged,
    AudioPlaybackFinished,
    AudioSpeechEnded,
    AudioSpeechStarted,
    ConversationTurnEnded,
    StateTransitioned,
)
from avid.main import (
    _build_camera,
    _build_cue_bank,
    _build_display,
    _build_embedder,
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
    AudioService,
    ConversationService,
    CueBank,
    MemoryService,
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
    "CostMeterService.turn_ended",
}

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


def test_build_embedder_selects_fake_at_the_configured_dimension() -> None:
    # sim.toml (and pi.toml) set embedder = "fake"; the local_minilm ONNX branch lands in a later
    # issue. The AC-6 guard's happy path — dimensions match [memory] dimensions — runs here (the
    # mismatch branch is only reachable by a real fixed-dim model, so it is pragma-excluded).
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
        "embedder": True,
        "fact_store": True,
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
    # store close, started first so the index is ready), AudioService's mic loop, and
    # ConversationService's per-session pump/mic/idle — are handed to the lifecycle to start/stop;
    # the reactive services (the two faces, the cost meter) are not. See ``_wire_services``.
    assert [type(s) for s in captured["services"]] == [
        MemoryService,
        AudioService,
        ConversationService,
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
    # ``audio.playback_finished`` barge-in feed (#104), and the cost meter's
    # ``conversation.turn_ended`` (#105).
    assert set(bus._subs) == {
        AffectChanged,
        StateTransitioned,
        AudioSpeechStarted,
        AudioSpeechEnded,
        AudioPlaybackFinished,
        ConversationTurnEnded,
    }

    subs = [sub for subs in bus._subs.values() for sub in subs]
    assert {sub.name for sub in subs} == _EXPECTED_SUBSCRIPTIONS
    # DROP_OLDEST throughout: only the latest edge is worth acting on (SDS §9.1.3).
    assert all(sub.policy is OverflowPolicy.DROP_OLDEST for sub in subs)


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
        microphone=FakeMicrophone(
            sample_rate=16000, channels=1, chunk_ms=20, pcm=b"\x00\x00"
        ),
        speaker=FakeSpeaker(),
        vad=FakeVoiceActivityDetector(),
        realtime=ReplayRealtimeClient(clock=clock, timeline=()),
        embedder=embedder,
        text_model=FakeTextModel(),
        fact_store=fact_store,
        retriever=retriever,
        cues=CueBank(speaker=FakeSpeaker(), asset_dir=None),
        config=config,
    )
    memory, _audio, conversation = services
    assert isinstance(memory, MemoryService)
    assert isinstance(conversation, ConversationService)
    # the ConversationService names the port; the concrete injected is the wired MemoryService
    assert conversation._memory is memory


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
        microphone=FakeMicrophone(
            sample_rate=16000, channels=1, chunk_ms=20, pcm=b"\x00\x00"
        ),
        speaker=FakeSpeaker(out_dir=tmp_path),
        vad=FakeVoiceActivityDetector(),
        realtime=ReplayRealtimeClient(clock=clock, timeline=()),
        embedder=embedder,
        text_model=FakeTextModel(),
        fact_store=fact_store,
        retriever=retriever,
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
