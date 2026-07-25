"""The neutral vocabulary the :class:`~avid.core.ports.RealtimeClient` port yields (#100).

The vendor boundary made a type (CLAUDE.md §3, SDS §6.10, R-10). A Realtime session is
a stream of provider-shaped messages; the ``openai``/``replay`` adapters translate that
stream into the frozen values here, and ``ConversationService`` (#102) translates *these*
into ``conversation.*`` domain events (:mod:`avid.domain.conversation`). No OpenAI message
shape ever crosses the port — if the Realtime API changes, exactly one adapter changes and
these values, the service, and the domain are untouched (PMP §9.2, risk R-10).

This is to the AI-client port what :mod:`avid.core.hal` is to the device ports: the
vocabulary that crosses the boundary, *defined by what the application needs*. It lives in
its own module rather than in ``hal`` because it is a distinct concern — an AI session, not
a device — and because it names two things ``hal`` deliberately does not: an
:class:`~avid.core.hal.AudioChunk` (assistant PCM) and a domain
:class:`~avid.domain.TokenUsage` (the turn's counts). Both imports point inward (core → core,
core → domain, P1), so ``hal`` stays the stdlib-only leaf it is.

Each value is frozen/slotted/kw-only, matching the HAL types and the ``Event`` envelope: an
adapter cannot mutate what it handed the service. These are **not** ``Event`` subclasses —
they carry no envelope and never touch the bus; they are the raw material the service mints
correlated events *from*.
"""

from __future__ import annotations

from dataclasses import dataclass

from avid.core.hal import AudioChunk
from avid.domain import TokenUsage


@dataclass(frozen=True, slots=True, kw_only=True)
class UserTranscript:
    """The user's speech, transcribed by the model (→ ``conversation.user_transcribed``).

    ``is_approximate`` mirrors the domain event's flag: a barge-in truncates the item and
    audio/transcript alignment is imprecise, so the tail is unreliable (§6.2.4). The adapter
    sets it; the service propagates it into the event so fact extraction can consult it.
    """

    text: str
    is_approximate: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantAudioChunk:
    """One delta of assistant speech (PCM), tagged with its response item (§6.2.4).

    The service hands ``chunk`` straight to :meth:`~avid.core.ports.TurnSink.play` — a
    **direct call, never a bus event** (§9.1.4): PCM does not belong on an at-most-once bus.
    ``item_id`` is the Realtime response item, so a later barge-in can truncate *this* item.
    """

    chunk: AudioChunk
    item_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantTranscript:
    """The assistant reply's transcript (→ ``conversation.assistant_responded``).

    Text only — the spoken audio arrives separately as :class:`AssistantAudioChunk`\\ s.
    ``item_id`` ties this transcript to the response item its audio deltas carry.
    """

    text: str
    item_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallRequested:
    """The model invoked a tool (→ dispatched by ``ConversationService``, §6.6, ADR-004).

    Per ADR-004 the model does not own memory — it *gets tools*, and this is the neutral value
    that carries one invocation across the port. The adapter surfaces it off the vendor's
    finalize frame (``response.output_item.done``, item type ``function_call``); the streaming
    ``response.function_call_arguments.delta`` acks are not modelled — we take the ``.done``
    rollup, exactly as we do for transcripts. The **return leg** is
    :meth:`~avid.core.ports.RealtimeClient.send_tool_output`, which must be followed by a
    ``response.create`` or the model silently sits (§6.6 — the step-5 trap).

    ``call_id`` is echoed back verbatim in the tool output so the model can correlate the
    result; ``arguments`` is the **raw JSON string** the model produced — the dispatcher parses
    it (the tool schemas + the dispatch to :class:`~avid.services.memory.MemoryService` land in
    #125). Kept a string here so the vendor boundary carries no opinion about a tool's shape.
    """

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnDone:
    """The turn's ``response.done`` token counts (→ ``conversation.turn_ended``).

    ``usage`` is the domain :class:`~avid.domain.TokenUsage` — the sole feed for §6.10.6's
    cost meter (the smoke detector for the silent caching failure, §6.10.3). Counts only:
    rates and dollars stay in the adapter/cost meter (#105), never here.
    """

    usage: TokenUsage


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionClosed:
    """The Realtime session ended (→ ``conversation.session_lost``).

    ``cause`` is a short human-readable reason (e.g. ``"network"``, ``"timeout"``). Whether a
    turn was mid-flight (``was_mid_turn``) is the *service's* to derive from its own state —
    the client only knows the socket closed — so it is not a field here.
    """

    cause: str


# The typed union the port yields (SDS §3.9.1). A closed set: adding a member is a
# deliberate widening every consumer's exhaustive match must then handle.
RealtimeEvent = (
    UserTranscript
    | AssistantAudioChunk
    | AssistantTranscript
    | ToolCallRequested
    | TurnDone
    | SessionClosed
)
