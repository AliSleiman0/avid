"""Tests for owned-task death visibility (:mod:`avid.core.tasks`, AVID-174).

The property under test is not "the task runs" — ``asyncio.create_task`` already does that. It is
that a task which **dies** says so. AVID-174 killed ``ConversationService._pump`` with an
``AssertionError`` mid-conversation and the log was silent: the socket stayed open, the robot went
permanently deaf to the model, and the only evidence was a traceback CPython prints at interpreter
shutdown. These tests pin the three cases that distinguish a supervisor from a wrapper — a crash is
loud, a cancellation is not a crash, and a caller can react.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from avid.core.tasks import spawn

_LOGGER = "avid.tasks"


async def test_a_dying_task_logs_its_name_and_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point: a raise inside an owned task reaches the log, with a traceback.

    Asserted on the *name* as well as the level, because "something died" is not actionable on a
    system running five owned tasks — the bench needs to know it was the pump."""

    async def boom() -> None:
        raise RuntimeError("the pump fell over")

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        task = spawn(boom(), name="ConversationService.pump")
        with pytest.raises(RuntimeError):
            await task

    assert "owned task ConversationService.pump died" in caplog.text
    assert "the pump fell over" in caplog.text  # exc_info=... carried the traceback


async def test_a_cancelled_task_is_not_reported_as_a_death(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancellation is how *every* teardown path in this system stops its owned tasks
    (``_teardown_locked``, ``stop``). Logging it as a failure would make a clean shutdown
    indistinguishable from a crash — exactly the confusion this module exists to remove."""

    async def forever() -> None:
        await asyncio.Event().wait()

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        task = spawn(forever(), name="AudioService.mic_loop")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert caplog.text == ""


async def test_a_task_that_returns_cleanly_says_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finite owned task (the idle timer, a cue) completing is not an event worth a line."""

    async def done() -> None:
        return None

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        await spawn(done(), name="ConversationService.idle")

    assert caplog.text == ""


async def test_on_death_receives_the_exception() -> None:
    """The seam a future supervision policy hangs off (#174 AC-5).

    Nothing in the system passes ``on_death`` yet, deliberately: deciding what a service should
    *do* about a dead pump — degrade, restart, tear the session down — is a policy question with
    state-table consequences and belongs in its own issue. This proves the hook works so that
    issue is ten lines rather than a redesign."""
    seen: list[BaseException] = []

    async def boom() -> None:
        raise ValueError("nope")

    task = spawn(boom(), name="EpisodeRecorder.prune", on_death=seen.append)
    with pytest.raises(ValueError):
        await task

    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)


async def test_on_death_does_not_fire_for_a_cancellation() -> None:
    """The hook inherits the same rule as the log: teardown is not death."""
    seen: list[BaseException] = []

    async def forever() -> None:
        await asyncio.Event().wait()

    task = spawn(forever(), name="ConversationService.mic", on_death=seen.append)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == []
