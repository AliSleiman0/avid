"""The ``Event`` envelope and the P4 event-naming rule (SDS §9.1).

Every event on the bus carries the fields defined here; they are not optional and
no event may redefine them. Concrete events subclass :class:`Event` and declare a
``name: ClassVar[str]`` of the form ``<domain>.<past_tense_verb>`` — validated at
class creation. This module is pure: it imports only the stdlib.
"""

import re
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

# The nine authoritative event domains (SDS §3.5.3). ``display`` and ``expression``
# are deliberately NOT domains: they are driven by direct port calls (SDS §9.1.4),
# never by events.
EVENT_DOMAINS: frozenset[str] = frozenset(
    {
        "system",
        "audio",
        "conversation",
        "affect",
        "state",
        "vision",
        "memory",
        "behavior",
        "motion",
    }
)

# Past-tense verbs that don't end in "-ed" but are legitimate event verbs
# (SDS §9.1.3: vision.presence_lost, conversation.session_lost).
_IRREGULAR_PAST: frozenset[str] = frozenset({"lost"})

# Catalogued names that don't fit the past-tense rule yet are normative anyway
# (SDS §9.1.3). Kept explicit so nothing else slips through the validator.
_NAME_EXCEPTIONS: frozenset[str] = frozenset({"system.shutting_down"})

# <domain>.<snake_case_verb>, lowercase ASCII only.
_NAME_RE = re.compile(r"^[a-z]+\.[a-z]+(?:_[a-z]+)*$")


def validate_event_name(name: str) -> None:
    """Validate an event name against P4: ``<domain>.<past_tense_verb>``.

    An event states what *happened*; it is a fact, not a command (SDS §9.1.2).
    Raises :class:`ValueError` if *name* is malformed, uses an unknown domain, or
    is not past tense.
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"event name {name!r} must be '<domain>.<past_tense_verb>' in "
            f"lowercase snake_case, e.g. 'affect.changed'"
        )
    domain, _, verb = name.partition(".")
    if domain not in EVENT_DOMAINS:
        raise ValueError(
            f"unknown event domain {domain!r} in {name!r}; "
            f"must be one of {sorted(EVENT_DOMAINS)}"
        )
    if name in _NAME_EXCEPTIONS:
        return
    last_token = verb.rsplit("_", 1)[-1]
    if last_token.endswith("ed") or last_token in _IRREGULAR_PAST:
        return
    raise ValueError(
        f"event verb {verb!r} in {name!r} must be past tense — an event is a "
        f"fact, not a command (P4): use 'affect.changed', not 'affect.change'"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base class for every event on the bus (SDS §9.1.1).

    Frozen because handlers are dispatched concurrently (SDS §3.5.2), so a mutable
    event is a data race with extra steps. Two clocks: wall-clock for humans,
    monotonic for arithmetic — never subtract ``timestamp_ms``.

    Concrete events subclass this and set ``name: ClassVar[str]`` to their
    ``<domain>.<past_tense_verb>`` identity; it is validated at class creation.
    """

    event_id: UUID  # unique, this occurrence
    correlation_id: UUID  # the TURN this belongs to. §3.12.2.
    timestamp_ms: int  # epoch milliseconds, wall clock - for logs
    monotonic_ns: int  # time.monotonic_ns() - for latency math
    source: str  # publishing component name, e.g. "AudioService"

    # Dotted event name, set by each concrete subclass. Declared here (annotation
    # only) so ``Event``-typed code can read ``.name``; validated below.
    name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        # Resolve the base explicitly, not via zero-arg ``super()``: ``slots=True``
        # makes ``@dataclass`` *recreate* ``Event``, orphaning the ``__class__`` cell a
        # bare ``super()`` reads — which raises ``TypeError`` on Python 3.11 (the Pi's
        # interpreter, ADR-008). CPython fixed the cell on 3.12; naming ``Event`` keeps
        # it correct on both. See issue #27.
        super(Event, cls).__init_subclass__(**kwargs)
        declared = cls.__dict__.get("name")
        if isinstance(declared, str):
            validate_event_name(declared)


# The two ``reason`` values the bus stamps on a ``system.handler_failed`` (§3.5.5,
# §9.1.3). Only ``queue_overflow`` is pinned by the AVID-10 acceptance criteria; the
# raise reason is ours. Kept as module constants so the bus and its tests agree.
REASON_HANDLER_RAISED = "handler_raised"
REASON_QUEUE_OVERFLOW = "queue_overflow"


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemHandlerFailed(Event):
    """The bus's only self-referential event (SDS §9.1.3): a subscriber either raised
    (§3.5.2) or overflowed its bounded queue (§3.5.5). Published *by the EventBus
    itself* so a broken subscriber degrades one feature instead of killing the robot.

    It must never be published from a handler *of* ``system.handler_failed`` — that is
    an infinite loop, guarded in the bus (see ``AsyncioEventBus._emit_handler_failed``).
    """

    name: ClassVar[str] = "system.handler_failed"

    handler: str  # the failing subscriber's registered name
    event_type: str  # ``type(event).__name__`` of the event being delivered
    reason: str  # REASON_HANDLER_RAISED | REASON_QUEUE_OVERFLOW
    exc: str | None = None  # ``repr(exception)`` for a raise; None for an overflow
