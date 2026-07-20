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
    FramebufferDisplay,
    HealthServer,
    Pca9685Servo,
    Picamera2Camera,
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
    ServiceNotifier,
    Servo,
    Speaker,
)
from avid.core.state_manager import StateManager
from avid.services import AffectService, ExpressionService


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


def _wire_services(*, bus: AsyncioEventBus, clock: Clock, display: Display) -> None:
    """Construct the services and register what they *declared* (SDS §9.2).

    The inversion is the point: a service says what it wants to hear via
    ``subscriptions()``; the composition root decides whether to grant it. That is what
    keeps the subscriber graph static and knowable for §9.1.5's drift check, and it is why
    this loop lives here rather than inside each service.

    **Ordering is load-bearing — call this before** :func:`lifecycle.run`. That function
    opens ``async with bus:``, which calls ``bus.start()``, and
    :meth:`AsyncioEventBus.subscribe` raises ``RuntimeError`` once started: subscription is
    static-at-composition by design (P3, SDS §3.5.2). Register, then run. Moving this call
    below the handoff turns a boot into a crash.

    Both services are typed on the **ports** (``EventBus``/``Clock``/``Display``), never on
    a concrete adapter (P2) — which is what lets the identical wiring drive ``FakeDisplay``
    on a laptop and ``FramebufferDisplay`` on the Pi from one config literal.

    Returns ``None``, and drops both local references on purpose. Every ``Subscription``
    holds a **bound method**, which holds its instance alive for the life of the bus — so
    there is nothing here to keep. Neither service is started or stopped: both are purely
    reactive with no owned task (SDS §9.2's ``start``/``stop`` are no-ops on both). The
    lifecycle-managed-service question is deferred to M4's ``AudioService``, which will own
    a real stream loop and is the right occasion to add both a ``Service`` Protocol and a
    ``services=`` parameter to ``lifecycle.run`` — deliberately, not by omission.
    """
    affect = AffectService(bus=bus, clock=clock)
    expression = ExpressionService(bus=bus, display=display, clock=clock)
    for service in (affect, expression):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )


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
    # ``_wire_services`` for why that order is not negotiable. This is the line that makes
    # the display a face rather than a health-map entry.
    _wire_services(bus=bus, clock=clock, display=display)
    # ``camera``, ``servo``, ``microphone`` and ``speaker`` are still constructed only to
    # realize the switch and appear in the health map: driving the camera is the vision
    # service's job (M8), moving the servo is MotionService's (M9), and consuming the mic /
    # driving the speaker is AudioService's (M4) — all later issues. ``display`` has left
    # this list as of AVID-73; ExpressionService owns it now.
    _ = camera
    _ = servo
    _ = microphone
    _ = speaker
    return await lifecycle.run(
        bus=bus,
        clock=clock,
        state=state,
        adapter_health=adapter_health,
        notifier=notifier,
        health=health,
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
