"""Shared hardware gating for the port contract suites (AVID-50, SDS §14.4).

A port's contract test runs against *every* adapter — the fake and the real one — so the
fake can never quietly drift from the real thing (P6). The real half needs the physical
Pi, so it is parametrized as a separate ``"real"`` case that **skips off the Pi**. This
module is that seam, declared once and reused by every hardware-backed port suite
(camera, servo, microphone, speaker, display) so the ``"real"`` slot is uniform.

**The matrix (SDS §14.4).** Same file, same assertions, two environments:

* **CI / a laptop** — ``on_pi()`` is ``False``, so the ``"real"`` params *skip*; only the
  fakes run. CI invokes a plain ``pytest`` (``.github/workflows/ci.yml``), no marker
  filter needed — the skip is automatic.
* **The Pi** — ``on_pi()`` is ``True``, so both halves run and must be indistinguishable
  through the port. The M2 gate (AVID-57) is exactly this run.

Set ``AVID_HARDWARE=1`` to force the real half on regardless of host (a local Pi run, or
to prove the seam wires up); ``AVID_HARDWARE=0`` forces it off. Reading the env here is
fine — the P7 config-purity grep scans ``avid/`` only, not ``tests/``.

This module has no ``test_`` prefix, so pytest never collects it; suites import it with
``from ._hardware import ...`` (``tests/contract/`` is a package).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# The device-tree node the kernel exposes on a Pi, e.g. "Raspberry Pi 4 Model B Rev 1.5"
# — the rig's actual string (#401).
# Absent on CI runners and dev laptops, which is exactly how those hosts read as not-a-Pi.
_MODEL_NODE = Path("/proc/device-tree/model")

# The param list every hardware-backed port fixture reuses. The fake runs everywhere; the
# real case carries the ``hardware`` mark (registered in pyproject.toml) so ``-m hardware``
# / ``-m "not hardware"`` can select it, and ``on_pi()`` skips it off-hardware. pytest
# applies marks on a fixture ``pytest.param`` to every test drawing that fixture, so all
# ``"real"`` cases are marked automatically.
FAKE_REAL_PARAMS = ["fake", pytest.param("real", marks=pytest.mark.hardware)]

# Env values that read as "off"; anything else present means "on".
_FALSEY = frozenset({"", "0", "false", "False"})


def on_pi() -> bool:
    """Whether the suite is running on a Raspberry Pi (SDS §14.4).

    An explicit ``AVID_HARDWARE`` override wins — so CI or a dev can force either answer,
    and both branches stay testable — otherwise the Pi is detected via its device-tree
    model node. A missing node (CI, laptop) reads as ``False``, which is what makes the
    ``"real"`` contract params skip there.
    """
    override = os.environ.get("AVID_HARDWARE")
    if override is not None:
        return override not in _FALSEY
    try:
        return "raspberry pi" in _MODEL_NODE.read_text(errors="ignore").lower()
    except OSError:
        return False


def skip_off_pi(reason: str = "requires Pi hardware (real adapter)") -> None:
    """Skip the current ``"real"`` contract case unless we are on the Pi.

    The first line of a fixture's ``"real"`` branch: on CI/a laptop it raises
    ``pytest.skip`` so only the fake runs; on the Pi it returns and the real adapter is
    exercised.
    """
    if not on_pi():
        pytest.skip(reason)
