"""Adapters layer — Real*/Fake* implementations of the ports.

Two implementations of every port, minimum. The fakes are first-class deliverables
that ship here (not in ``tests/``) and *are* the simulator (P6). Adapters are
constructed only by the composition root in ``avid/main.py`` (P3).

The ``Clock`` port's real adapter is named ``SystemClock`` (not ``RealClock``) — the
idiomatic name, matching SDS §9.3, AVID-12's acceptance criteria, and the bus's private
``_SystemClock`` stand-in.
"""

from avid.adapters.camera import FakeCamera, Picamera2Camera
from avid.adapters.clock import FakeClock, SystemClock
from avid.adapters.display import FakeDisplay
from avid.adapters.health import HealthServer
from avid.adapters.microphone import AlsaMicrophone, FakeMicrophone
from avid.adapters.notifier import FakeServiceNotifier, SystemdNotifier
from avid.adapters.servo import FakeServo, Pca9685Servo

__all__ = [
    # Camera (AVID-51)
    "FakeCamera",
    "Picamera2Camera",
    # Servo (AVID-52)
    "FakeServo",
    "Pca9685Servo",
    # Microphone (AVID-53)
    "FakeMicrophone",
    "AlsaMicrophone",
    # Clock (AVID-12)
    "FakeClock",
    "SystemClock",
    # Display (AVID-13)
    "FakeDisplay",
    # ServiceNotifier (AVID-38)
    "FakeServiceNotifier",
    "SystemdNotifier",
    # Local control API (AVID-40)
    "HealthServer",
]
