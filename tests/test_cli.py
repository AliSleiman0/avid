"""Smoke tests for the ``avid`` CLI surface (AVID-1, AVID-14).

The end-to-end boot/SIGTERM behaviour lives in ``tests/e2e/test_boot.py``; these tests
only cover the argument-parsing surface, which must stay cheap and not start a robot.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import avid
from avid.main import main


def test_version_is_nonempty_str() -> None:
    assert isinstance(avid.__version__, str)
    assert avid.__version__


def test_main_requires_config() -> None:
    # --config is required (AVID-14): you cannot run the robot without a config.
    # argparse exits 2 on a missing required argument, before main() runs anything.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "avid" in capsys.readouterr().out


def test_module_entrypoint_help_exits_zero() -> None:
    # Covers avid/__main__.py — `python -m avid --help` must exit 0.
    result = subprocess.run(
        [sys.executable, "-m", "avid", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: avid" in result.stdout
