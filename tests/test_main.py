"""Composition-root wiring in main.py (AVID-14).

Covers the adapter switch and the build/delegate glue cross-platform, without waiting
for a real signal — the full boot-to-SIGTERM path is in ``tests/e2e/test_boot.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from avid.adapters import FakeDisplay
from avid.core.config import load_config
from avid.main import _build_display, main

_SIM_TOML = Path(__file__).resolve().parents[1] / "config" / "sim.toml"


def test_build_display_selects_fake() -> None:
    config = load_config(_SIM_TOML)
    assert isinstance(_build_display(config), FakeDisplay)


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
    assert captured["adapter_health"] == {"clock": True, "display": True}
    assert captured["bus"] is not None
    assert captured["clock"] is not None
