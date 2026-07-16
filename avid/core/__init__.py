"""Core layer — application infrastructure.

The EventBus (AVID-9/10), StateManager, Config, Lifecycle, and the port Protocols
in ``ports.py`` (AVID-11). Depends on ``domain`` only; never imports
``avid.adapters`` (P1, P5).
"""

from avid.core.event_bus import AsyncioEventBus, OverflowPolicy, Subscription

__all__ = ["AsyncioEventBus", "OverflowPolicy", "Subscription"]
