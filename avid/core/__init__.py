"""Core layer — application infrastructure.

The EventBus (AVID-9/10), StateManager, Config, Lifecycle, and the port Protocols
in ``ports.py`` (AVID-11). Depends on ``domain`` only; never imports
``avid.adapters`` (P1, P5).
"""

from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus, OverflowPolicy, Subscription
from avid.core.faces import render_face
from avid.core.hal import AudioChunk, Axis, CameraCaps, DisplayFrame, Frame
from avid.core.ports import (
    Camera,
    Clock,
    Display,
    EventBus,
    Microphone,
    ServiceNotifier,
    Servo,
    Speaker,
)

__all__ = [
    # Configuration (AVID-14)
    "Config",
    "load_config",
    # Event bus (AVID-9/10)
    "AsyncioEventBus",
    "OverflowPolicy",
    "Subscription",
    # Port Protocols (AVID-11)
    "Camera",
    "Clock",
    "Display",
    "EventBus",
    "Microphone",
    "Servo",
    "ServiceNotifier",
    "Speaker",
    # HAL value types (AVID-11)
    "AudioChunk",
    "Axis",
    "CameraCaps",
    "DisplayFrame",
    "Frame",
    # Face composition (AVID-70)
    "render_face",
]
