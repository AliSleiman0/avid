"""Domain layer — pure logic.

No I/O, no async, no globals, and no third-party imports beyond the stdlib and
``pydantic`` (P1). The domain imports nothing else from ``avid``. Home of the
``Event`` envelope (AVID-6), ``RobotState`` + the transition table (AVID-7), and
``Affect`` (AVID-8).
"""

from avid.domain.events import EVENT_DOMAINS, Event, validate_event_name

__all__ = ["EVENT_DOMAINS", "Event", "validate_event_name"]
