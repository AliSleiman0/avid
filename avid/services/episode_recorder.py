"""``EpisodeRecorder`` — the write-only transcript observer (#123, SDS §7.5).

Every other consumer of ``conversation.*`` *acts* on a turn; this one only **watches** it. It
subscribes to the four conversation facts that carry a turn's shape — the two turn boundaries
(``turn_started`` / ``turn_ended``) and the two that carry text (``user_transcribed`` /
``assistant_responded``) — and mirrors each into the ``episodes`` table, keyed by the turn's
``correlation_id`` (§3.12.2), so *one grep on a correlation id reconstructs what the robot
actually heard*. That is the whole point of §7.5: the tier that pays off when something breaks
on the Pi at 11pm.

It is **write-only with respect to the conversation flow** (AC-4): it publishes nothing (it is
handed no bus), and nothing in any retrieval path reads ``episodes``, so a failure inside a
handler is swallowed by the bus (``system.handler_failed``) and can never affect a turn. It owns
one task — the §7.5 90-day prune, on a schedule driven by the injected :class:`~avid.core.ports.Clock`
so the retention test runs in milliseconds, not a quarter — and each pass is **bounded** so it
never stalls the loop on a large table (P8, §2.7.1: the SD card is the binding constraint).

Depends only on the :class:`~avid.core.ports.Clock` and :class:`~avid.core.ports.EpisodeStore`
Protocols (P2); constructed once by the composition root (P3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import cast

from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import Clock, EpisodeStore
from avid.domain import (
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
)

_log = logging.getLogger(__name__)

_SOURCE = "EpisodeRecorder"
_SECONDS_PER_DAY = 86_400


class EpisodeRecorder:
    """Mirror every turn's transcript into the ``episodes`` table; prune the old (SDS §7.5).

    Satisfies the :class:`~avid.core.ports.Service` shape (``name``/``start``/``stop``/
    ``subscriptions``). Reactive **and** task-owning, like ``MemoryService``: its subscriptions do
    the recording, and ``start`` launches the prune loop, so the composition root hands it to the
    lifecycle to ``start``/``stop``. It names only the ``Clock`` and ``EpisodeStore`` ports (P2)
    and never publishes — the recorder is an observer, not a participant (AC-4).
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        clock: Clock,
        store: EpisodeStore,
        retention_days: int,
        prune_interval_s: float,
        prune_batch: int,
    ) -> None:
        self._clock = clock
        self._store = store
        self._retention_days = retention_days
        self._prune_interval_s = prune_interval_s
        self._prune_batch = prune_batch
        self._prune_task: asyncio.Task[None] | None = None

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Launch the §7.5 prune loop (AC-3). Its first pass fires only *after* the interval, not
        at ``start`` — so the episode store's first DB touch happens well after ``MemoryService``
        has migrated the shared file at boot, and the retention schedule is honest."""
        self._prune_task = asyncio.create_task(
            self._prune_loop(), name="EpisodeRecorder.prune"
        )

    async def stop(self) -> None:
        """Cancel the prune loop and close the store within the §9.2 5 s budget. Idempotent."""
        task = self._prune_task
        self._prune_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._store.aclose()

    def subscriptions(self) -> Sequence[Subscription]:
        """The four ``conversation.*`` facts a transcript is made of (AC-1, SDS §9.1.3): the two
        turn boundaries plus the two text-carrying facts. All static, all named so the §9.1.5 drift
        check sees them; DROP_OLDEST to match the sibling ``conversation.*`` subscribers
        (``ConversationService`` / ``CostMeterService``) — episode writes are low-frequency, so the
        bounded queue is headroom, never a tuning surface."""
        return (
            Subscription(
                event_type=ConversationTurnStarted,
                handler=cast(Handler, self._on_turn_started),
                name="EpisodeRecorder.turn_started",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=ConversationUserTranscribed,
                handler=cast(Handler, self._on_user_transcribed),
                name="EpisodeRecorder.user_transcribed",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=ConversationAssistantResponded,
                handler=cast(Handler, self._on_assistant_responded),
                name="EpisodeRecorder.assistant_responded",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=ConversationTurnEnded,
                handler=cast(Handler, self._on_turn_ended),
                name="EpisodeRecorder.turn_ended",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- recording (bus handlers) --------------------------------------------------------

    async def _on_turn_started(self, event: ConversationTurnStarted) -> None:
        """Open (or ensure) the episode for this turn's ``correlation_id`` (§3.12.2, AC-2)."""
        await self._store.start_episode(event.correlation_id, at=self._clock.now())

    async def _on_user_transcribed(self, event: ConversationUserTranscribed) -> None:
        """Record the user's line — **with the ``is_approximate`` flag** made visible (AC-5). A
        barge-in truncation leaves the tail unreliable (§6.2.4), and anything reading this transcript
        later must be able to tell, so the marker is greppable, not silently dropped."""
        speaker = "user approximate" if event.is_approximate else "user"
        await self._store.append(
            event.correlation_id, f"[{speaker}] {event.text}", at=self._clock.now()
        )

    async def _on_assistant_responded(
        self, event: ConversationAssistantResponded
    ) -> None:
        """Record the assistant's reply transcript (text only — PCM is a direct call, §9.1.4)."""
        await self._store.append(
            event.correlation_id, f"[assistant] {event.text}", at=self._clock.now()
        )

    async def _on_turn_ended(self, event: ConversationTurnEnded) -> None:
        """Close the turn: bump ``turn_count`` and advance ``ended_at`` (AC-2)."""
        await self._store.end_turn(event.correlation_id, at=self._clock.now())

    # --- the prune loop (§7.5, AC-3) -----------------------------------------------------

    async def _prune_loop(self) -> None:
        """Delete episodes past the 90-day retention, on a schedule, until cancelled (AC-3).

        Sleeps on the injected clock (fakeable — the test steps virtual time instead of waiting a
        quarter), then prunes a **bounded** batch: the cutoff is ``now - retention`` in epoch
        seconds (§8.2), and the store caps the delete at ``prune_batch`` rows, so a large backlog
        drains over successive passes rather than stalling the loop (P8)."""
        while True:
            await self._clock.sleep(self._prune_interval_s)
            cutoff = self._clock.now() - self._retention_days * _SECONDS_PER_DAY
            deleted = await self._store.prune(
                older_than=cutoff, limit=self._prune_batch
            )
            if deleted:
                _log.info(
                    "pruned %d episode(s) older than %d days",
                    deleted,
                    self._retention_days,
                )


__all__ = ["EpisodeRecorder"]
