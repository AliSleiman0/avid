"""Avid — Pico, an AI Desktop Companion Robot.

Top-level package. The architecture is hexagonal (ports & adapters) around an
in-process event bus; see ``CLAUDE.md`` for the layering rules (P1–P8) and
``SDS.md`` for the full design. Layers, innermost first::

    domain/    pure logic — no I/O, no async, nothing third-party but pydantic
    core/      EventBus, StateManager, Config, ports (the Protocols)
    services/  async use-case orchestration
    adapters/  Real*/Fake* implementations of the ports
    main.py    composition root — the only place adapters are constructed
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("avid")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
