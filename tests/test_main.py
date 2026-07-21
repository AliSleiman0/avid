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
    FakeMicrophone,
    FakeServiceNotifier,
    FakeServo,
    FakeSpeaker,
    FakeVoiceActivityDetector,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import load_config
from avid.core.event_bus import AsyncioEventBus, OverflowPolicy
from avid.core.hal import DisplayFrame
from avid.core.state_manager import StateManager
from avid.domain import AffectChanged, StateTransitioned
from avid.main import (
    _build_camera,
    _build_display,
    _build_microphone,
    _build_notifier,
    _build_servo,
    _build_speaker,
    _build_vad,
    _wire_services,
    main,
)
from avid.services import AudioService

# The exact subscriber graph the composition root is expected to build (SDS §9.1.3). Spelled
# out rather than derived from the services, so that a service silently dropping or renaming
# a subscription fails here instead of quietly agreeing with itself.
_EXPECTED_SUBSCRIPTIONS = {
    "AffectService.state_transitioned",
    "ExpressionService.affect_changed",
    "ExpressionService.state_transitioned",
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
        "notifier": True,
        "health": True,
    }
    assert captured["bus"] is not None
    assert captured["clock"] is not None
    assert isinstance(captured["notifier"], FakeServiceNotifier)
    assert captured["health"] is not None
    assert captured["watchdog_interval_s"] == 15.0
    # AVID-89: the one lifecycle-managed service (the mic loop) is handed to the lifecycle to
    # start/stop; the two reactive services are not (they own no task). See ``_wire_services``.
    assert [type(s) for s in captured["services"]] == [AudioService]


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
    # Both event types the two services care about, and nothing else.
    assert set(bus._subs) == {AffectChanged, StateTransitioned}

    subs = [sub for subs in bus._subs.values() for sub in subs]
    assert {sub.name for sub in subs} == _EXPECTED_SUBSCRIPTIONS
    # DROP_OLDEST throughout: a backlog of stale faces is worse than no backlog (SDS §9.1.3).
    assert all(sub.policy is OverflowPolicy.DROP_OLDEST for sub in subs)


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
    # synth, which under coverage would trip the P8 slow-callback gate at construction.
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
