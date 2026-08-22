"""The ``GET /state`` reading — three independent facts about a running robot (#385, SDS §9.5).

§9.5 has always specified the route as *"current `RobotState`, `Affect`, session status"*, and
those three live in three different objects: the :class:`~avid.core.state_manager.StateManager`,
``AffectService`` and ``ConversationService``.

⚠️ **The three readings are independent, and that is the whole design rather than a detail of it.**
SDS §3.10 makes ``RobotState`` and ``Affect`` **orthogonal** — the robot can be
SLEEPING-and-content or LISTENING-and-confused — and *"conflating them is the most common design
error in this class of project"*. A reporter handed one object and asked for both fields would
reintroduce that coupling at the reporting layer, where it would look harmless and read as fact.
Here each field has its **own provider, registered by whoever owns that source**: the composition
root registers ``state`` from the state manager it holds, and ``_wire_services`` registers
``affect`` and ``session`` from the services it builds. There is no code path along which one
could be derived from the other, because no single caller supplies both.

Deliberately the same shape as :class:`~avid.core.metrics.MetricsRegistry`, including its two
hard-won rules: **re-registration is a programmer error** (two sources answering to one name is
the ambiguity that makes a report lie, and it should fail at boot where it is cheap), and **a
value that could not be read is named in ``absent``, never replaced by a default**. A ``/state``
that reported ``IDLE`` because nothing was wired would be a lie with a plausible face.

Synchronous and cheap by contract (:class:`~avid.core.ports.StateSource`): every provider is an
attribute read, because a snapshot runs inline on the event loop while the robot may be mid-turn
(P8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from avid.domain import Affect, RobotState

_log = logging.getLogger("avid.core.state_report")

# The fields §9.5 specifies for this route, in the order a reader wants them. Registration of any
# other name is rejected: `/state` is a defined route with a defined shape, not a second metrics
# registry, and letting it grow field by field is how an endpoint stops being answerable.
_FIELDS: tuple[str, ...] = ("state", "affect", "session")


class StateReport:
    """Named providers for the ``/state`` fields, read on demand.

    Registration is composition-time and providers are plain zero-argument callables, so a source
    needs no base class and no knowledge of this module — ``lambda: affect_service.affect`` is a
    complete integration.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], Any]] = {}

    def register(self, name: str, provider: Callable[[], Any]) -> None:
        """Add one field's provider. Unknown names and repeats both fail loudly, at boot."""
        if name not in _FIELDS:
            raise ValueError(
                f"{name!r} is not a /state field (expected one of {_FIELDS})"
            )
        if name in self._providers:
            raise ValueError(f"/state field {name!r} is already registered")
        self._providers[name] = provider

    def snapshot(self) -> Mapping[str, Any]:
        """Read every registered field once; name the rest.

        Never raises. A provider that throws is named in ``absent`` and logged — a control
        endpoint that took the robot down would be a reliability defect living inside a
        reliability feature (SDS §3.12.3).
        """
        reading: dict[str, Any] = {}
        absent: list[str] = []
        for name in _FIELDS:
            provider = self._providers.get(name)
            if provider is None:
                absent.append(name)
                continue
            try:
                value = provider()
            except Exception:  # noqa: BLE001 - a reading must not take the robot down
                _log.warning("/state field %s could not be read", name, exc_info=True)
                absent.append(name)
                continue
            reading[name] = _render(value)
        # Flat, unlike `MetricsRegistry`'s `{"metrics": {...}}`: there are exactly three fields and
        # `curl -s .../state | jq .affect` is the shape §9.5's own examples use. `absent` cannot
        # collide with a field, because `_FIELDS` is closed.
        reading["absent"] = sorted(absent)
        return reading


def _render(value: object) -> object:
    """JSON-friendly rendering. Enum members become their ``name``; everything else passes through.

    ⚠️ ``name``, not ``value``. Both enums are ``auto()``-numbered, so ``RobotState.SLEEPING.value``
    is ``6`` — a number that means nothing to a person reading a `curl`, that changes if a member
    is ever inserted above it, and that needs a lookup table to interpret. ``"SLEEPING"`` is the
    identifier the transition table, the logs and this project's prose already use.
    """
    if isinstance(value, (RobotState, Affect)):
        return value.name
    return value


__all__ = ["StateReport"]
