"""Smoke tests for the AVID-1 scaffold: the package imports and the CLI runs."""

from __future__ import annotations

import subprocess
import sys

import pytest

import avid
from avid.main import main


def test_version_is_nonempty_str() -> None:
    assert isinstance(avid.__version__, str)
    assert avid.__version__


def test_main_no_args_returns_zero() -> None:
    assert main([]) == 0


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "avid" in capsys.readouterr().out


def test_config_flag_is_accepted() -> None:
    # --config is part of the stable surface but not yet consumed (AVID-14).
    assert main(["--config", "config/sim.toml"]) == 0


def test_module_entrypoint_help_exits_zero() -> None:
    # Covers avid/__main__.py — `python -m avid --help` must exit 0.
    result = subprocess.run(
        [sys.executable, "-m", "avid", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: avid" in result.stdout
