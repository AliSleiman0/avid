"""The ``SpendSource`` fake — money, without a bill (#472, P6).

The real implementation is ``CostMeterService`` itself, which is a *service* rather than an
adapter for the same reason ``AudioService`` implements ``TurnSink``: the thing that already
knows what turns cost is the thing that should answer how much they cost. This is its simulator
half, and it exists because a port without a working fake is an incomplete port (P6).

⚠️ It is a **stub with a dial**, not a re-implementation. Deliberately: the interesting behaviour
of the real one is its rolling monotonic window, and a fake that reimplemented that would be a
second copy of the arithmetic to keep in step — and would happily agree with a broken original.
Tests that care about the window drive ``CostMeterService`` with a ``FakeClock``; tests that care
about *what the ceiling does when spend is high* set a number here and get on with it.
"""

from __future__ import annotations


class FakeSpendSource:
    """A ``SpendSource`` that reports whatever it was told to (SDS §9.3).

    ``usd`` is public and mutable so a test can move spend under a running service — which is the
    case worth testing, since a ceiling that only ever sees a constant is a ceiling nobody has
    watched cross its own bar.
    """

    def __init__(self, *, usd: float = 0.0) -> None:
        self.usd = usd
        #: Every window this was asked about, in order — so a test can assert the caller passed
        #: the configured window rather than a literal it made up. That is the same class of
        #: defect as a gate harness that never passed its own knobs (AVID-180).
        self.windows: list[float] = []

    def spend_since(self, window_s: float) -> float:
        self.windows.append(window_s)
        return self.usd
