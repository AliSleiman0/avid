"""The ``conversation.*`` events and the ``TokenUsage`` value — pure conversation-domain
facts (#99).

The five events are the facts ``ConversationService`` (#102) will publish as it turns
one Realtime session into domain events (SDS §9.1.3, SDS:1990–1994). They are
**normative and CI-enforced**: the ``event-catalog-drift`` check (§9.1.5) diffs live
subscriptions against this catalog, so the names and payloads here are fixed, not a
design. Each is a frozen/slotted/kw-only :class:`~avid.domain.events.Event` subclass
carrying a validated ``<domain>.<past_tense_verb>`` name (P4), exactly like the
``audio.*`` events next door.

``TokenUsage`` is a plain domain value (not an ``Event``): the payload
``conversation.turn_ended`` carries, and the **sole feed** for §6.10.6's cost meter —
the smoke detector for the silent $12-vs-$85 caching failure (§6.10.3). It counts
tokens only; **rates and dollars stay out of the domain** (they are vendor/config, and
live in the adapter/observability cost meter, #105). Co-locating it with
``ConversationTurnEnded`` keeps ``conversation.py -> events.py`` a one-way import edge,
so an ``.importlinter`` independence contract can never be tripped — the same reasoning
that keeps ``StateTransitioned`` in ``state.py`` (AVID-69), simpler here because nothing
imports this module back.

Pure by construction (P1): no I/O, no clock, no async, stdlib only. No
``conversation.*`` event carries assistant PCM — audio is a **direct call** (§9.1.4),
never a bus event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from avid.domain.events import Event


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """Per-turn token counts from the Realtime ``response.done`` usage payload (§6.10.6).

    The **only** feed for the cost meter, which must break spend out by
    **cached / uncached / output** (§6.10.6). ``input_tokens`` is the whole input
    billed for the turn and ``cached_input_tokens`` is the subset of it that hit the
    prompt cache — ~98.75% cheaper (§6.10.2, Fact 1) — so the expensive slice is the
    difference, exposed as :attr:`uncached_input_tokens`. Output tokens are the audio
    floor no cache can remove (§6.10.2, Fact 2).

    Counts only: no rates, no currency — those are vendor/config and belong to the cost
    meter (#105), not the domain. :meth:`__add__` lets that meter accumulate a running
    daily total from per-turn values.
    """

    input_tokens: int  # total input billed this turn (§6.10.1)
    cached_input_tokens: int  # subset served from the prompt cache (~98.75% cheaper)
    output_tokens: int  # assistant tokens — the audio-out floor (§6.10.2)

    @property
    def uncached_input_tokens(self) -> int:
        """Input tokens billed at the full (uncached) rate — the expensive slice."""
        return self.input_tokens - self.cached_input_tokens

    @property
    def total_tokens(self) -> int:
        """All tokens billed this turn: input (cached + uncached) plus output."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Combine two usages field-wise, so the cost meter can accumulate a daily total."""
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationTurnStarted(Event):
    """A conversational turn began (SDS §9.1.3, SDS:1990).

    ``initiator`` says whether the user opened the turn (VAD-gated speech) or the robot
    did (a proactive behaviour) — the two turn origins (SDS §3.12.2). Published by
    ``ConversationService``; the ``correlation_id`` is propagated from the
    ``audio.speech_started`` / ``behavior.trigger_fired`` that minted it. Queue policy on
    the bus is DROP_NEWEST.
    """

    name: ClassVar[str] = "conversation.turn_started"

    initiator: Literal["user", "proactive"]  # which of the two turn origins opened this


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationUserTranscribed(Event):
    """The user's speech was transcribed (SDS §9.1.3, SDS:1991).

    Drives ``LISTENING -> THINKING`` (``domain/state.py``). ``is_approximate`` is not a
    hedge: it is §6.2.4's truncation consequence made explicit. A barge-in truncates the
    Realtime item, dropping the transcript for unplayed audio, and audio/transcript
    alignment is imprecise — so the tail is unreliable. Anything that treats this text as
    ground truth (fact extraction, episode recording) **must** consult the flag. Queue
    policy DROP_NEWEST.
    """

    name: ClassVar[str] = "conversation.user_transcribed"

    text: str  # the recognized utterance
    is_approximate: bool  # barge-in truncation left the tail unreliable (§6.2.4)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationAssistantResponded(Event):
    """The assistant produced a response item's transcript (SDS §9.1.3, SDS:1992).

    Carries only the **text** of the reply; the spoken audio (PCM) is delivered by a
    direct call, never on the bus (§9.1.4). ``item_id`` identifies the Realtime response
    item so a later ``audio.playback_finished`` — and M5 barge-in truncation — can refer
    to the same item. Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "conversation.assistant_responded"

    text: str  # the reply transcript (no PCM — audio is a direct call, §9.1.4)
    item_id: str  # the Realtime response item this transcript belongs to


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationTurnEnded(Event):
    """A conversational turn completed (SDS §9.1.3, SDS:1993).

    ``usage`` is the ``response.done`` token payload and the **sole** feed for §6.10.6's
    cost meter — the smoke detector for the silent caching failure (§6.10.3): broken
    caching looks exactly like working caching until the invoice. ``duration_ms`` is the
    turn's wall length. Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "conversation.turn_ended"

    duration_ms: int  # how long the turn lasted
    usage: TokenUsage  # response.done token counts — the only feed for the cost meter (§6.10.6)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationSessionLost(Event):
    """The Realtime session dropped (SDS §9.1.3, SDS:1994).

    Drives *any* state ``-> DEGRADED`` (``domain/state.py``), so the robot can play a
    canned CueBank phrase and wait to reconnect. ``was_mid_turn`` says whether a turn was
    in flight when the session died — the difference between a quiet reconnect and a
    dropped reply. ``cause`` is a short human-readable reason (e.g. ``"network"``,
    ``"timeout"``). Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "conversation.session_lost"

    cause: str  # short human-readable reason the session dropped
    was_mid_turn: bool  # was a turn in flight when the session died?
