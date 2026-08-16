"""``ObservabilityService`` — the structured tap, and the thing it deliberately does not tap (#242).

The negative test is the one worth reading first. ``behavior.proactive_delivered`` and
``behavior.proactive_suppressed`` are already written durably to ``proactive_log`` (§10.6), and that
table *is* R-08's instrument. A second consumer here would produce a second count of the same fact,
and when the two disagreed there would be no way to say which was right.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.core.envelope import envelope
from avid.domain import (
    BehaviorProactiveDelivered,
    BehaviorProactiveSuppressed,
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    RobotState,
    StateTransitioned,
    Trigger,
)
from avid.services.observability import ObservabilityService

_CORR = uuid4()


def _env(clock: FakeClock) -> dict[str, object]:
    return envelope(clock=clock, correlation_id=_CORR, source="test")


def _lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Every line this service emitted, parsed back from JSON.

    Parsing rather than substring-matching is the assertion: §3.12.2 asks for *structured* logging,
    and a line that reads well but does not parse is a line no log processor can use.
    """
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "avid.observability"
    ]


async def test_it_subscribes_to_the_three_rows_that_had_no_consumer() -> None:
    """§9.1.3 lists an ``Observability`` subscriber against these three and nothing had ever
    registered one — ``state.transitioned``'s row is even tagged ``(M10)``."""
    names = {sub.name for sub in ObservabilityService().subscriptions()}
    assert names == {
        "ObservabilityService.state_transitioned",
        "ObservabilityService.trigger_fired",
        "ObservabilityService.trigger_disabled",
    }


async def test_it_does_not_also_consume_the_proactive_outcomes() -> None:
    """⚠️ AC-3, and the reason is not tidiness.

    ``proactive_delivered``/``_suppressed`` are already written to ``proactive_log`` by
    ``BehaviorService``, and §10.6 tunes the policy by grouping over *that table*. A second consumer
    here would be a second count of one fact — and the moment the two disagreed, neither would be
    trustworthy. One fact, one instrument.
    """
    subscribed = {sub.event_type for sub in ObservabilityService().subscriptions()}
    assert BehaviorProactiveDelivered not in subscribed
    assert BehaviorProactiveSuppressed not in subscribed


async def test_a_transition_is_logged_as_parseable_json_with_its_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§3.12.2: *"one grep on a correlation ID reconstructs the entire turn"*. A structured line
    without it is a fact with no way back to the conversation that caused it."""
    clock = FakeClock()
    service = ObservabilityService()
    with caplog.at_level(logging.INFO, logger="avid.observability"):
        await service._on_transition(  # noqa: SLF001 - the handler is the unit
            StateTransitioned(
                **_env(clock),  # type: ignore[arg-type]
                from_=RobotState.IDLE,
                to=RobotState.THINKING,
                trigger=Trigger.BEHAVIOR_TRIGGER_FIRED,
            )
        )

    (line,) = _lines(caplog)
    assert line["event"] == "state.transitioned"
    assert line["from_"] == "IDLE"
    assert line["to"] == "THINKING"
    assert line["trigger"] == "behavior.trigger_fired"
    assert line["correlation_id"] == str(_CORR)


async def test_a_disabled_trigger_is_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§10.5: *"Disabling is logged loudly, never silent."* Loudly means a level someone reading a
    week of journal will actually see — a trigger that turned itself off is the robot reporting that
    one of your ideas was bad, and INFO is where that goes to die."""
    clock = FakeClock()
    service = ObservabilityService()
    with caplog.at_level(logging.INFO, logger="avid.observability"):
        await service._on_trigger_disabled(  # noqa: SLF001 - the handler is the unit
            BehaviorTriggerDisabled(
                **_env(clock),  # type: ignore[arg-type]
                trigger_id=7,
                ignore_streak=3,
            )
        )

    (record,) = [r for r in caplog.records if r.name == "avid.observability"]
    assert record.levelno == logging.WARNING
    line = json.loads(record.getMessage())
    assert (line["trigger_id"], line["ignore_streak"]) == (7, 3)


async def test_the_counters_count(caplog: pytest.LogCaptureFixture) -> None:
    """Plain attributes, like ``ExpressionService``'s staleness counters — read by a future
    ``GET /metrics`` (§9.5), and meanwhile the reason "how often did a trigger disable itself?" has
    an answer that is a number rather than a grep."""
    clock = FakeClock()
    service = ObservabilityService()
    assert (service.transitions, service.triggers_fired, service.triggers_disabled) == (
        0,
        0,
        0,
    )

    for _ in range(3):
        await service._on_transition(  # noqa: SLF001 - as above
            StateTransitioned(
                **_env(clock),  # type: ignore[arg-type]
                from_=RobotState.IDLE,
                to=RobotState.LISTENING,
                trigger=Trigger.AUDIO_SPEECH_STARTED,
            )
        )
    await service._on_trigger_fired(  # noqa: SLF001 - as above
        BehaviorTriggerFired(**_env(clock), trigger_id=1)  # type: ignore[arg-type]
    )
    await service._on_trigger_disabled(  # noqa: SLF001 - as above
        BehaviorTriggerDisabled(**_env(clock), trigger_id=1, ignore_streak=3)  # type: ignore[arg-type]
    )

    assert (service.transitions, service.triggers_fired, service.triggers_disabled) == (
        3,
        1,
        1,
    )


async def test_it_owns_no_task_and_stops_idempotently() -> None:
    """Purely reactive, like the two faces: kept alive by its bound-method subscriptions, with
    nothing to start and nothing to leak."""
    service = ObservabilityService()
    await service.start()
    await service.stop()
    await service.stop()
    assert service.name == "ObservabilityService"
