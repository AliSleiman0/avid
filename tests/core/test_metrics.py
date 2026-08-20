"""Tier-1 tests for the metrics registry (#380, SDS §3.12.2).

Pure, milliseconds. The tests that earn their keep are all one idea: **an instrument that is not
there must not report a number.** A `0` from an unwired counter reads exactly like a real `0`, and
that confusion is the defect family that dominated M6's gate — an absent instrument's zero looked
like a measurement for long enough to be believed.
"""

from __future__ import annotations

import pytest

from avid.core.metrics import MetricsRegistry, ProvidedMetrics
from avid.core.ports import MetricsSource


def test_a_registered_provider_is_read_on_snapshot() -> None:
    registry = MetricsRegistry()
    registry.register("turns", lambda: 7)
    assert registry.snapshot() == {"metrics": {"turns": 7}, "absent": []}


def test_providers_are_read_lazily_not_at_registration() -> None:
    """A snapshot is a reading of *now*. If values were captured at registration, every metric
    would report the boot's value forever — the most confidently wrong dashboard available."""
    counter = {"n": 0}
    registry = MetricsRegistry()
    registry.register("n", lambda: counter["n"])
    counter["n"] = 42
    assert registry.snapshot()["metrics"]["n"] == 42


def test_a_provider_returning_none_is_absent_not_zero() -> None:
    """ "I have no source" and "my value is zero" are different statements, and the difference must
    survive to whoever reads this at thirty days."""
    registry = MetricsRegistry()
    registry.register("thermals", lambda: None)
    snapshot = registry.snapshot()
    assert "thermals" not in snapshot["metrics"]
    assert snapshot["absent"] == ["thermals"]


def test_a_failing_provider_is_named_absent_and_does_not_take_the_robot_down() -> None:
    """§3.12.3: nothing but a bad key at boot stops the robot, and a metrics read is the last
    thing that should. It is also never silently defaulted — a broken counter is reported broken.
    """

    def _boom() -> int:
        raise RuntimeError("sensor is on fire")

    registry = MetricsRegistry()
    registry.register("ok", lambda: 1)
    registry.register("broken", _boom)
    snapshot = registry.snapshot()
    assert snapshot["metrics"] == {"ok": 1}
    assert snapshot["absent"] == ["broken"]


def test_one_failing_provider_does_not_hide_the_others() -> None:
    """Every criterion reports before any verdict (CLAUDE.md §7.1), applied to instruments: a
    snapshot that abandoned the loop on the first exception would lose every metric after it."""
    registry = MetricsRegistry()
    registry.register("a", lambda: 1)
    registry.register("bad", lambda: 1 / 0)
    registry.register("z", lambda: 26)
    snapshot = registry.snapshot()
    assert snapshot["metrics"] == {"a": 1, "z": 26}
    assert snapshot["absent"] == ["bad"]


def test_registering_a_name_twice_fails_loudly() -> None:
    """Two sources answering to one metric name is the ambiguity that makes a dashboard lie. It
    should fail at boot, where it is cheap, rather than resolve silently to whichever won."""
    registry = MetricsRegistry()
    registry.register("turns", lambda: 1)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("turns", lambda: 2)


def test_an_empty_registry_snapshots_cleanly() -> None:
    assert MetricsRegistry().snapshot() == {"metrics": {}, "absent": []}


def test_provided_metrics_satisfies_the_port_structurally() -> None:
    assert isinstance(ProvidedMetrics(MetricsRegistry()), MetricsSource)
