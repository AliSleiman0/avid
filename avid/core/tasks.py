"""Owned-task spawning — the one place a background task's death becomes visible (AVID-174).

Services own long-lived tasks: the mic loop, the Realtime pump, the mic forwarder, the idle and
first-token timers. Until this module existed **nothing observed their exceptions.** A task that
raised died quietly, its loop simply gone, and the only trace was CPython's
``Task exception was never retrieved`` at interpreter shutdown — long after the run that mattered.

That is not a theoretical gap. AVID-174 killed ``ConversationService._pump`` with an
``AssertionError`` mid-conversation: the socket stayed open, the robot went permanently deaf to the
model, and **the log said nothing**. systemd's watchdog cannot see it either — the process is alive
and still pinging (§3.11.3). A supervisor that cannot see a dead limb is not a supervisor.

The contract here is deliberately **visibility, not policy**:

* Every death is logged at ERROR with the task's name and a full traceback, so a bench log or a
  ``journalctl`` dump names the failure at the moment it happens.
* ``CancelledError`` is *not* a death. Every teardown path in this system cancels its owned tasks
  (``_teardown_locked``, ``stop``), so treating cancellation as failure would make a clean shutdown
  indistinguishable from a crash — the precise confusion this module exists to remove.
* An optional ``on_death`` hook lets a caller *react*. Nothing uses it yet, and that is on purpose:
  deciding what a service should **do** about a dead pump (degrade? restart? tear the session down?)
  is a policy question with state-table consequences, and it belongs in its own issue with its own
  acceptance criteria rather than smuggled into a race fix.

⚠️ **This module does not publish anything.** ``system.handler_failed`` is the *bus's* fact about a
failing subscriber, and both SDS §9.1.3 and :class:`~avid.domain.SystemHandlerFailed` say so
explicitly — a service minting it would be a lie about who observed what. A bus fact for a dead
owned task would need a new event, a §9.1.3 catalog row and a subscriber: a deliberate SDS change,
not a quiet one. Until then a loud log is the honest ceiling, and it is a great deal better than the
silence it replaces.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

_log = logging.getLogger("avid.tasks")


def spawn(
    coro: Coroutine[Any, Any, None],
    *,
    name: str,
    on_death: Callable[[BaseException], None] | None = None,
) -> asyncio.Task[None]:
    """``asyncio.create_task`` with *name*, plus a done-callback that makes a death loud.

    Use this for every **owned** task — one a service creates, holds a handle to and cancels on
    teardown. The event bus's subscriber workers are deliberately excluded: the bus already owns
    its own failure semantics (log, swallow, republish as ``system.handler_failed``), and routing
    them through here would double-report.

    *on_death* fires only for a genuine exception, never for cancellation, and runs inside the
    done-callback — so it must not block and must not raise. It is a seam for a future supervision
    policy, not a mechanism this module implements.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(lambda done: _report_death(done, on_death))
    return task


def _report_death(
    task: asyncio.Task[None], on_death: Callable[[BaseException], None] | None
) -> None:
    """Log an owned task's exception at ERROR, then hand it to *on_death* if one was given."""
    if task.cancelled():
        return  # a normal teardown, not a failure — see the module docstring
    exc = task.exception()
    if exc is None:
        return
    _log.error("owned task %s died", task.get_name(), exc_info=exc)
    if on_death is not None:
        on_death(exc)
