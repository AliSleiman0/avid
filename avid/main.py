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
    FakeCamera,
    FakeDisplay,
    FakeServiceNotifier,
    HealthServer,
    Picamera2Camera,
    SystemClock,
    SystemdNotifier,
)
from avid.core import lifecycle
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Camera, Display, ServiceNotifier


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
    """Select the ``Display`` adapter named by ``[adapters] display`` (the sim/real switch).

    At M0 only the fake exists; the real HDMI/PNG adapters land with their hardware
    issues. Any other value fails loudly rather than silently doing nothing.
    """
    match config.adapters.display:
        case "fake":
            # frames_dir is injected (P7): the default repo-relative scratch dir on a
            # laptop, but a writable StateDirectory path under the Pi's read-only unit.
            return FakeDisplay(out_dir=Path(config.display.frames_dir))
        case other:  # pragma: no cover - guards a not-yet-built adapter
            raise NotImplementedError(
                f"display adapter {other!r} is not available yet — only 'fake' is "
                f"implemented at M0 (AVID-14)"
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


async def _run(config: Config) -> int:
    """Build the adapters and bus, then hand off to the lifecycle.

    This is the P3 site: :class:`SystemClock`, the display, the notifier, and the
    control-API server are constructed here and nowhere else. The real clock is injected
    into the bus, retiring its private ``_SystemClock`` default.
    """
    clock = SystemClock()
    display = _build_display(config)
    camera = _build_camera(config)
    notifier = _build_notifier(config)
    health = HealthServer(bind=config.api.bind, port=config.api.port)
    bus = AsyncioEventBus(clock=clock)
    adapter_health = {
        "clock": True,
        "display": True,
        "camera": True,
        "notifier": True,
        "health": True,
    }
    # ``display`` and ``camera`` are constructed to realize the switch and appear in the
    # health map; rendering to the display is ExpressionService's job and driving the
    # camera is the vision service's (M8) — both later issues.
    _ = display
    _ = camera
    return await lifecycle.run(
        bus=bus,
        clock=clock,
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
