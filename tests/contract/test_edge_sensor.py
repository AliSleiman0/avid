"""Contract suite for the ``EdgeSensor`` port (#400, ADR-015, SDS §3.9.5, §12.1 F-13, §14.4).

One promise: :meth:`clear` is ``True`` only if **every** sensor fitted sees surface. The
``"fake"`` case runs everywhere; the ``"real"`` case skips off the Pi and, on the Pi, reads
the TCRT5000s on the measured pins with the measured polarity — on a desk, so the honest
answer is ``True``, and the test that wants ``False`` asks a hand to make it so.
"""

from __future__ import annotations

import pytest

from avid.adapters.edge import FakeEdgeSensor
from avid.core.ports import EdgeSensor

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi


@pytest.fixture(params=FAKE_REAL_PARAMS)
def edge(request: pytest.FixtureRequest) -> EdgeSensor:
    """Every EdgeSensor adapter, real and fake, must satisfy the tests below (P6)."""
    if request.param == "fake":
        return FakeEdgeSensor()
    skip_off_pi()
    # gpiozero is Pi-only (apt, via --system-site-packages, ADR-008); imported on the Pi only.
    from avid.adapters.edge import Tcrt5000EdgeSensor

    return Tcrt5000EdgeSensor(pins=(23, 22), active_high=True)


def test_adapter_satisfies_the_edge_sensor_port(edge: EdgeSensor) -> None:
    assert isinstance(edge, EdgeSensor)


async def test_clear_answers_a_bool(edge: EdgeSensor) -> None:
    """A reading, not a fact (SDS §9.1.4) — and on a desk, with nothing under a hand, it is
    ``True``. Asserted as a type first: a sensor that answered ``1`` would satisfy a truthiness
    check and confuse every ``is True`` downstream."""
    assert isinstance(await edge.clear(), bool)


# --- FakeEdgeSensor-specific: the edge on demand ----------------------------------------------


async def test_the_fake_is_clear_by_default_and_counts_its_reads() -> None:
    """The desk is there until a test says otherwise; ``reads`` is how a test asserts the
    drive *asked* before it moved rather than assuming it did."""
    fake = FakeEdgeSensor()
    assert fake.reads == 0
    assert await fake.clear() is True
    assert await fake.clear() is True
    assert fake.reads == 2


async def test_the_fake_lies_on_cue_and_recovers_on_cue() -> None:
    """The edge, then the desk again: the shape a service test needs to prove the abort AND
    the latch release without a fall."""
    fake = FakeEdgeSensor(clear=True)
    fake.clear_flag = False
    assert await fake.clear() is False
    fake.clear_flag = True
    assert await fake.clear() is True


async def test_the_fake_can_be_built_already_at_an_edge() -> None:
    assert await FakeEdgeSensor(clear=False).clear() is False
