"""Composition root and command-line entry point.

For M0 (AVID-1) this is a minimal CLI: ``avid --help`` must exit 0. The real
composition root — parsing a TOML config into a frozen pydantic model, selecting
fake vs. real adapters, wiring subscriptions, and reaching IDLE — arrives in
AVID-14. The ``--config`` option is accepted now so the surface is stable, but it
is not yet consumed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from avid import __version__


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
        type=Path,
        metavar="PATH",
        help="Path to a TOML config file (wired in AVID-14; ignored for now).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``avid`` console script.

    Returns a process exit code. ``--help`` and ``--version`` exit 0 via argparse
    before returning here.
    """
    parser = build_parser()
    parser.parse_args(argv)
    # AVID-14 will construct the config, adapters, bus, and services here.
    return 0
