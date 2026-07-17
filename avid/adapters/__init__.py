"""Adapters layer — Real*/Fake* implementations of the ports.

Two implementations of every port, minimum. The fakes are first-class deliverables
that ship here (not in ``tests/``) and *are* the simulator (P6). Adapters are
constructed only by the composition root in ``avid/main.py`` (P3).

The ``Clock`` port's real adapter is named ``SystemClock`` (not ``RealClock``) — the
idiomatic name, matching SDS §9.3, AVID-12's acceptance criteria, and the bus's private
``_SystemClock`` stand-in.
"""

from avid.adapters.clock import FakeClock, SystemClock
from avid.adapters.display import FakeDisplay

__all__ = [
    # Clock (AVID-12)
    "FakeClock",
    "SystemClock",
    # Display (AVID-13)
    "FakeDisplay",
]
