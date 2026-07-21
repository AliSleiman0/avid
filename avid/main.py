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
    FakeCamera,
    FakeDisplay,
    FakeMicrophone,
    FakeServiceNotifier,
    FakeServo,
    FakeSpeaker,
    FakeVoiceActivityDetector,
    FramebufferDisplay,
    HealthServer,
    Pca9685Servo,
    Picamera2Camera,
    SileroVad,
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
    Microphone,
    Service,
    ServiceNotifier,
    Servo,
    Speaker,
    VoiceActivityDetector,
)
from avid.core.state_manager import StateManager
from avid.services import AffectService, AudioService, ExpressionService


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

    ``AffectService`` and ``ExpressionService`` are purely reactive — no owned task, ``start``/
    ``stop`` are no-ops — so they are wired for their subscriptions and then dropped: every
    ``Subscription`` holds a **bound method** that keeps its instance alive for the life of the
    bus. ``AudioService`` is different: it owns a mic-consume loop (SDS §9.2), so it is **returned**
    for :func:`lifecycle.run` to ``start``/``stop`` at the right points in the boot/shutdown order.
    Its ``subscriptions()`` is empty at M4 (the ``conversation.*`` facts it will hear do not exist
    until M5), so it registers nothing today — but it goes through the same loop so the day those
    facts arrive is pure addition, not a wiring change.
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
    )
    for service in (affect, expression, audio):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    # Only the services with an owned task need lifecycle management; the two reactive ones
    # are kept alive by their bound-method subscriptions above.
    return (audio,)


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
    notifier = _build_notifier(config)
    health = HealthServer(bind=config.api.bind, port=config.api.port)
    bus = AsyncioEventBus(clock=clock)
    # The one state machine (SDS §3.8.4). Built here so every future service shares this
    # instance rather than growing a private copy — the lifecycle drives it to IDLE.
    state = StateManager(bus=bus, clock=clock)
    adapter_health = {
        "clock": True,
        "display": True,
        "camera": True,
        "servo": True,
        "microphone": True,
        "speaker": True,
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
        config=config,
    )
    # ``camera`` and ``servo`` are still constructed only to realize the switch and appear in the
    # health map: driving the camera is the vision service's job (M8) and moving the servo is
    # MotionService's (M9) — both later issues. ``display`` (AVID-73) and now ``microphone``/
    # ``speaker``/``vad`` (AVID-89) have left this list; their services own them.
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


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``avid`` console script.

    Parses args, loads the config (the one place ``OPENAI_API_KEY`` is read), and runs
    the robot. Returns a process exit code; ``--help``/``--version`` exit 0 via argparse
    before returning here, and a missing ``--config`` exits 2.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    return asyncio.run(_run(config))
