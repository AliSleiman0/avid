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

from avid import __version__

# The one place Fake*/System* adapters are constructed (P3). CI greps for these
# outside main.py and test fixtures.
from avid.adapters import FakeDisplay, SystemClock
from avid.core import lifecycle
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Display


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
            return FakeDisplay()
        case other:  # pragma: no cover - guards a not-yet-built adapter
            raise NotImplementedError(
                f"display adapter {other!r} is not available yet — only 'fake' is "
                f"implemented at M0 (AVID-14)"
            )


async def _run(config: Config) -> int:
    """Build the adapters and bus, then hand off to the lifecycle.

    This is the P3 site: :class:`SystemClock` and the display adapter are constructed
    here and nowhere else. The real clock is injected into the bus, retiring its private
    ``_SystemClock`` default.
    """
    clock = SystemClock()
    display = _build_display(config)
    bus = AsyncioEventBus(clock=clock)
    adapter_health = {"clock": True, "display": True}
    # ``display`` is constructed to realize the switch and appear in the health map;
    # rendering to it is ExpressionService's job in a later issue.
    _ = display
    return await lifecycle.run(bus=bus, clock=clock, adapter_health=adapter_health)


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
