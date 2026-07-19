"""Tests for the contract-suite hardware seam (AVID-50).

The seam itself lives in :mod:`tests.contract._hardware`. These tests pin its behaviour
*deterministically* — via monkeypatch, never the ambient environment — so they give the
same result on a laptop, on CI, and on a real Pi (where ``on_pi()`` would otherwise be
True and couple the assertions to the host).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import _hardware
from ._hardware import on_pi, skip_off_pi


def test_on_pi_false_without_any_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No override and no device-tree node — a laptop / CI runner — reads as not-a-Pi."""
    monkeypatch.delenv("AVID_HARDWARE", raising=False)
    monkeypatch.setattr(_hardware, "_MODEL_NODE", tmp_path / "absent")
    assert on_pi() is False


def test_on_pi_true_when_model_node_says_raspberry_pi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AVID_HARDWARE", raising=False)
    node = tmp_path / "model"
    node.write_text(
        "Raspberry Pi 5 Model B Rev 1.0\x00"
    )  # trailing NUL, as the kernel writes
    monkeypatch.setattr(_hardware, "_MODEL_NODE", node)
    assert on_pi() is True


def test_on_pi_false_for_a_non_pi_model_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AVID_HARDWARE", raising=False)
    node = tmp_path / "model"
    node.write_text("Some Other SBC v2")
    monkeypatch.setattr(_hardware, "_MODEL_NODE", node)
    assert on_pi() is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_avid_hardware_override_wins_over_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str, expected: bool
) -> None:
    """The env override decides regardless of what the device-tree node says — here a
    node that *would* read as a Pi, so we prove the override takes precedence."""
    pi_node = tmp_path / "model"
    pi_node.write_text("Raspberry Pi 5")
    monkeypatch.setattr(_hardware, "_MODEL_NODE", pi_node)
    monkeypatch.setenv("AVID_HARDWARE", value)
    assert on_pi() is expected


def test_skip_off_pi_skips_when_not_on_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hardware, "on_pi", lambda: False)
    with pytest.raises(pytest.skip.Exception):
        skip_off_pi()


def test_skip_off_pi_is_a_noop_on_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hardware, "on_pi", lambda: True)
    skip_off_pi()  # returns without raising — the real case proceeds


def test_hardware_marker_is_registered(pytestconfig: pytest.Config) -> None:
    """`-m "not hardware"` must be clean — the mark has to be declared, not just used."""
    markers = pytestconfig.getini("markers")
    assert any(m.split(":", 1)[0] == "hardware" for m in markers)
