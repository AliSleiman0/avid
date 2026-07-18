"""Tests for ``Affect`` and the ``affect.changed`` event (AVID-8, SDS §3.10.1, §6.8).

Tier-1, pure, milliseconds (SDS §14.2): no async, no I/O, no clock. The load-bearing
guarantee is that ``Affect`` stays **orthogonal** to ``RobotState`` — enforced
mechanically by the ``affect-state-orthogonality`` import-linter contract and stated
here where a reader can see it (``test_affect_is_decoupled_from_robotstate``).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from uuid import uuid4

import pytest

from avid.domain import (
    Affect,
    AffectChanged,
    RobotState,
    validate_event_name,
)

AFFECT_MODULE = Path(__file__).resolve().parents[2] / "avid" / "domain" / "affect.py"


def make_affect_changed(**overrides: object) -> AffectChanged:
    fields: dict[str, object] = {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": "AffectService",
        "affect": Affect.HAPPY,
        "tier": 2,
        "previous": Affect.IDLE,
    }
    fields.update(overrides)
    return AffectChanged(**fields)  # type: ignore[arg-type]


# --- the enum ---------------------------------------------------------------


def test_affect_has_exactly_the_eight_documented_members() -> None:
    """Pins the membership (SDS §6.8: Tier-1 IDLE/LISTENING/THINKING/SPEAKING faces +
    Tier-2 HAPPY/SAD/CONFUSED overlays + SLEEPING). SPEAKING is present per §6.8's
    tier-1 face list — the SDS wins over M0.md AVID-8's 7-member list."""
    assert {a.name for a in Affect} == {
        "IDLE",
        "LISTENING",
        "THINKING",
        "SPEAKING",
        "HAPPY",
        "SAD",
        "CONFUSED",
        "SLEEPING",
    }


# --- the affect.changed event -----------------------------------------------


def test_affect_changed_carries_affect_tier_and_previous() -> None:
    e = make_affect_changed(affect=Affect.CONFUSED, tier=1, previous=Affect.THINKING)
    assert e.name == "affect.changed"
    assert e.affect is Affect.CONFUSED
    assert e.tier == 1
    assert e.previous is Affect.THINKING


def test_affect_changed_is_frozen_slotted_kw_only() -> None:
    e = make_affect_changed()
    assert not hasattr(e, "__dict__")  # slotted
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.affect = Affect.SAD  # type: ignore[misc]
    with pytest.raises(TypeError):  # kw-only: positional construction rejected
        AffectChanged(uuid4(), uuid4(), 1, 2, "s", Affect.HAPPY, 2, Affect.IDLE)  # type: ignore[call-arg]


def test_affect_changed_name_validates() -> None:
    """Its declared name passes the P4 validator (already run at class creation)."""
    validate_event_name(AffectChanged.name)


# --- orthogonality to RobotState (AVID-8's hard rule) -----------------------


def test_affect_is_decoupled_from_robotstate() -> None:
    """``affect.py`` must not reach into ``state.py`` — the reader-visible half of the
    ``affect-state-orthogonality`` import-linter contract (SDS §3.10.1)."""
    tree = ast.parse(AFFECT_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "avid.domain.state", (
                "affect.py must not import avid.domain.state (orthogonality, SDS §3.10.1)"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "avid.domain.state", (
                    "affect.py must not import avid.domain.state (orthogonality)"
                )
    # Distinct types that share names (IDLE/LISTENING/…) but no member identity.
    assert Affect is not RobotState
    assert set(Affect) & set(RobotState) == set()
