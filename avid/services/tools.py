"""The memory tool contract — schemas, capability instructions, and dispatch (#125, SDS §6.6).

Per ADR-004 the model does not own memory; **it gets tools.** This module is that contract,
in one testable place: the three tool *declarations* the session is seeded with (§6.6), the
§7.6 capability instruction text that tells the model *when* to call them, and the pure
:func:`dispatch_tool_call` that turns one :class:`~avid.core.realtime.ToolCallRequested` into
a result the model can read.

It names only the :class:`~avid.core.ports.MemoryTools` port (never the concrete
``MemoryService``, P2/P5), the neutral :class:`~avid.core.realtime.ToolCallRequested`, and the
domain ``FACT_KINDS``/``Fact`` — no vendor. The JSON-Schema declarations are vendor-neutral
dicts (a plain dict names no vendor, exactly like ``[ai.turn_detection]`` in config); the
composition root hands them to the ``openai`` adapter's ``tools=`` param, and the adapter puts
them in ``session.update`` as the cached prefix (§6.2.2). ``ConversationService`` uses the same
constants to *dispatch* — one source, so the schema the model is told about and the code that
executes its calls cannot drift.

The §6.6 return leg (``send_tool_output`` → the mandatory ``response.create``) is the adapter's
job; this module only produces the ``output`` string.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from uuid import UUID

from avid.core.ports import AffectTools, BehaviorTools, GestureTools, MemoryTools
from avid.core.realtime import ToolCallRequested
from avid.domain import (
    FACT_KINDS,
    SEMANTIC_AFFECTS,
    Affect,
    Direction,
    Fact,
    LookAtResult,
    RoutineSpec,
)

_log = logging.getLogger(__name__)

# The tool names (§6.6). Constants, not literals, so the schema declaration below and the
# dispatch routing cannot disagree about a spelling.
REMEMBER_FACT = "remember_fact"
RECALL = "recall"
FORGET = "forget"
SET_AFFECT = "set_affect"
SET_QUIET = "set_quiet"
LOOK_AT = "look_at"

# The default recall cutoff (§7.7, k=5) when the model omits ``k``.
_DEFAULT_RECALL_K = 5

# The §7.6 capability instruction text, verbatim from SDS §7.6 (the capability layer, §6.4 layer
# 3). Its final clause is load-bearing: without "…anything you inferred rather than were told"
# the model confabulates facts from context and memory fills with confident fiction — far worse
# than an empty memory (§7.6). Seeded into the session's static instructions by the composition
# root, alongside :data:`TOOL_SCHEMAS`.
CAPABILITY_INSTRUCTIONS = (
    "When the user tells you something durable about themselves — their name, "
    "preferences, routines, relationships, or significant events — call remember_fact. "
    "Do this silently and continue the conversation naturally; do not announce that you "
    "are remembering. Rate importance 1-10, where 1 is trivia and 10 is core identity. "
    "Do not store passing remarks, questions, or anything you inferred rather than were told. "
    "When the user asks about something they told you before that is not already in your "
    "context, call recall. When the user asks you to forget something, call forget. "
    # M10 (#314). Deliberately phrased as a scope rather than an encouragement: `schedule` is an
    # argument on a tool the model already calls reliably, so the risk here is not that it goes
    # unused but that it gets attached to every routine-shaped sentence — "I usually get coffee in
    # the mornings" has no clock time, and a schedule invented for it fires at a moment nobody
    # chose. Under-filling is recoverable; a wrong time is a reminder at the wrong hour.
    "If the routine happens at a specific time on a repeating schedule, fill in remember_fact's "
    "schedule argument as well. Leave it out when the user gave no clear time or no repetition — "
    "do not guess one. "
    # ⚠️ REWEIGHTED at the M6 gate (AVID-214/216). The first version led with the constraint —
    # "call set_affect ONLY when ... and not on ordinary replies" — on §6.5's finding that
    # negative constraints are followed far more strongly than encouragements. Measured live, that
    # is exactly what happened: across 20 turns including an unambiguously sad utterance (the model
    # replied "I'm really sorry to hear that") and an unambiguously delighted one ("WOO-HOO! That's
    # huge"), set_affect fired **zero** times. The model was emotionally engaged in its words and
    # never touched the tool.
    #
    # So the suppression was doing all the work, which is §6.5's finding operating against us. The
    # instruction now LEADS with the action and keeps a single short constraint behind it.
    "When your reply carries a clear emotional tone, call set_affect so your face matches your "
    "words — happy for good news, sad for bad, confused when you do not follow. Do this as well "
    "as replying, not instead of it. Skip it for neutral replies. "
    # M10 (#243) — §10.4's manual override. Scoped hard, and the *constraint* is the load-bearing
    # half here, which is the opposite of the set_affect clause above. That one needed
    # encouragement because the model would not call the tool at all; this one rides a request the
    # user makes explicitly, so the risk is a model that self-quiets speculatively — because the
    # user sounded busy, or answered curtly — producing a robot that goes silent for reasons the
    # user never asked for and cannot see. Under-firing is §10.1's cheap error; **unexplained**
    # silence is not, because the user has no way to tell it from a broken robot.
    "If the user asks to be left alone or says they are busy right now, call set_quiet with "
    "roughly how long they asked for. Only when they ask — never because you think they might "
    "want it. "
    # M9 (#204). Weighted like set_quiet rather than like set_affect, and for the same reason:
    # this one rides an explicit request, so the failure mode is not under-firing but a model
    # that decorates every reply with a gesture — which would run the servos continuously,
    # contradict the gate's relax clause and load the rail #206 measures. §6.5's finding says a
    # negative constraint is followed far more strongly than an encouragement, so the sentence
    # that stops it gesturing constantly matters more than the one that enables it. The cooldown
    # is a hard backstop underneath, but a backstop the model keeps hitting is a model that
    # spends its turns being told no.
    "If the user asks you to look somewhere — left, right, up, down, or back at them — call "
    "look_at. Only when they ask; never as decoration on an ordinary reply."
)


# The tool declarations (§6.6), JSON Schema at session level. ``remember_fact.kind`` enum is
# derived from the domain ``FACT_KINDS`` tuple so it cannot drift from the §8.3 ``CHECK`` the
# database enforces — the model is structurally prevented from inventing a seventh kind (AC-2).
TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": REMEMBER_FACT,
        "description": (
            "Store a durable fact the user told you about themselves. Durable before it "
            "returns — call it silently, do not announce remembering."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact, in the user's own words.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(FACT_KINDS),
                    "description": "The category of fact.",
                },
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "1 is trivia, 10 is core identity.",
                },
                # §10's machine-readable half (M10). The `routines` table exists only because
                # §10.3 needs a time it can put in a min-heap, and this is where that time comes
                # from: the model has already parsed "every day at 8 AM" out of speech in order
                # to write `text`, so this asks for the structured form of what it had in hand.
                # Reading `text` back and re-deriving it locally was the alternative, and §6.8's
                # own objection applies — a heuristic that reads "8" as 20:00 delivers the coffee
                # reminder at night.
                "schedule": {
                    "type": "object",
                    "description": (
                        "For recurring routines with a time of day: when it happens. "
                        "Omit for anything that is not a scheduled routine."
                    ),
                    "properties": {
                        "rrule": {
                            "type": "string",
                            "description": (
                                "An RFC 5545 RRULE, e.g. 'FREQ=DAILY' or "
                                "'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR'."
                            ),
                        },
                        "local_time": {
                            "type": "string",
                            "description": "Time of day as HH:MM, 24-hour, e.g. '08:00'.",
                        },
                        "timezone": {
                            "type": "string",
                            "description": (
                                "IANA timezone name, e.g. 'Asia/Beirut'. "
                                "Omit unless the user names a different one."
                            ),
                        },
                    },
                    "required": ["rrule", "local_time"],
                },
            },
            "required": ["text", "kind", "importance"],
        },
    },
    {
        "type": "function",
        "name": RECALL,
        "description": (
            "Retrieve facts the user told you earlier that are not in your current context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, in natural language.",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many facts to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": FORGET,
        "description": (
            "Permanently delete the facts matching a query. Use when the user withdraws "
            "consent ('forget that') — a hard delete, not a correction."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Which facts to delete, in natural language.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": SET_AFFECT,
        "description": (
            "Set the robot's facial expression to match the emotional tone of what you "
            "are about to say. Use it only when the tone genuinely changes; leave it alone "
            "for ordinary replies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "affect": {
                    "type": "string",
                    # Derived from the domain's Tier-2 tuple, exactly as `remember_fact.kind`
                    # is derived from FACT_KINDS, so the model is structurally prevented from
                    # inventing an expression — or from reaching a Tier-1 baseline that is the
                    # state machine's to own (§6.8).
                    "enum": [a.name.lower() for a in SEMANTIC_AFFECTS],
                    "description": "The expression to show.",
                },
            },
            "required": ["affect"],
        },
    },
    {
        "type": "function",
        "name": SET_QUIET,
        "description": (
            "Stop speaking up on your own for a while, when the user asks to be left "
            "alone or says they are busy. Ordinary conversation is unaffected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration_s": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How long to stay quiet, in seconds.",
                },
            },
            "required": ["duration_s"],
        },
    },
    {
        "type": "function",
        "name": LOOK_AT,
        "description": (
            "Turn to look in a direction, when the user asks you to look somewhere. "
            "Your body turns left and right; your head tilts up and down."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    # Derived from the domain enum, not spelled out, for the reason
                    # remember_fact's `kind` derives from FACT_KINDS: the model is then
                    # structurally prevented from inventing a sixth direction, and the schema it
                    # is told about cannot drift from the code that executes its calls.
                    "enum": [d.name.lower() for d in Direction],
                    "description": (
                        "Where to look: left, right, up, down, or center to face forward."
                    ),
                },
            },
            "required": ["direction"],
        },
    },
)


def _result(payload: dict[str, Any]) -> str:
    """Serialise one tool-output payload to the string ``send_tool_output`` echoes back."""
    return json.dumps(payload)


def _error(message: str) -> str:
    """A tool-error output (AC-6): the turn continues, the model is told the tool failed."""
    return _result({"ok": False, "error": message})


async def dispatch_tool_call(
    memory: MemoryTools,
    call: ToolCallRequested,
    *,
    affect: AffectTools,
    behavior: BehaviorTools,
    gesture: GestureTools,
    correlation_id: UUID,
    approximate: bool,
    default_timezone: str,
) -> str:
    """Execute one tool call against the memory port and return the model's tool output (AC-1/AC-6).

    Pure orchestration over the injected :class:`~avid.core.ports.MemoryTools` port: route by
    ``call.name``, parse ``call.arguments`` (the model's raw JSON string), execute, and shape the
    result to the §6.6 tool contract — ``remember_fact`` → ``{ok, fact_id}``, ``recall`` →
    ``{facts: [...]}``, ``forget`` → ``{deleted: n}``. **Every** failure path — an unknown tool
    name, malformed JSON, a missing or ill-typed field, an invalid ``kind``/``importance``, or a
    raising port call — is caught and returned as a tool **error** output, never raised: a bad tool
    call must let the turn continue, not crash the pump or silently swallow the turn (AC-6).

    ``approximate`` guards the §6.2.4/§7.6 barge-in trap: on a turn whose user transcript is
    approximate (the tail after a truncation is unreliable), ``remember_fact`` is **declined
    without writing** — a half-heard sentence stored as fact is exactly the confabulation §7.6
    guards against, and "confidently wrong" is worse than an empty memory. The model is told,
    honestly, that nothing was stored."""
    try:
        if call.name == REMEMBER_FACT:
            if approximate:
                return _error(
                    "not stored: the transcript is approximate after a barge-in, so this "
                    "may be misheard (§7.6) — say it again and I'll remember it"
                )
            args = _load_object(call.arguments)
            fact_id = await memory.remember_fact(
                text=str(args["text"]),
                kind=str(args["kind"]),
                importance=int(args["importance"]),
                schedule=_parse_schedule(args.get("schedule"), default_timezone),
                correlation_id=correlation_id,
            )
            return _result({"ok": True, "fact_id": fact_id})

        if call.name == RECALL:
            args = _load_object(call.arguments)
            k = int(args.get("k", _DEFAULT_RECALL_K))
            facts = await memory.recall(
                str(args["query"]), k=k, correlation_id=correlation_id
            )
            return _result({"facts": [_fact_view(f) for f in facts]})

        if call.name == FORGET:
            args = _load_object(call.arguments)
            deleted = await memory.forget(
                str(args["query"]), correlation_id=correlation_id
            )
            return _result({"deleted": deleted})

        if call.name == SET_QUIET:
            args = _load_object(call.arguments)
            until = await behavior.set_quiet(
                int(args["duration_s"]), correlation_id=correlation_id
            )
            return _result({"ok": True, "until": until})

        if call.name == SET_AFFECT:
            args = _load_object(call.arguments)
            await affect.set_affect(
                _parse_affect(str(args["affect"])), correlation_id=correlation_id
            )
            # {ok} and nothing else, immediately: §6.6 classifies this async/fire-and-forget,
            # and §6.8's argument for tolerating Tier 2's ~400 ms is that the Tier-1 baseline is
            # never wrong. Awaiting a render would import that latency into the turn for nothing.
            return _result({"ok": True})

        if call.name == LOOK_AT:
            args = _load_object(call.arguments)
            outcome = await gesture.look_at(
                _parse_direction(str(args["direction"])), correlation_id=correlation_id
            )
            # {ok} immediately, never after the sweep: §6.6 classifies this fire-and-forget
            # because a gesture is the better part of a second, and step 5's response.create
            # landing behind it is audible as dead air.
            #
            # A decline is a *successful* tool call reporting a refusal, not an error — the
            # model needs to say "I just did" or "I cannot look up", and an error output invites
            # it to apologise for a malfunction that did not happen.
            if outcome is LookAtResult.ACCEPTED:
                return _result({"ok": True})
            return _result({"ok": False, "reason": _LOOK_AT_REASONS[outcome]})

        return _error(f"unknown tool {call.name!r}")
    except Exception as exc:  # noqa: BLE001 — AC-6: no tool failure may reach the pump
        _log.warning(
            "tool %r failed [%s]: %s", call.name, correlation_id, exc, exc_info=True
        )
        return _error(f"{call.name} failed: {exc}")


# What the model is told when a look_at is declined. Phrased for a listener rather than a log:
# the model reads these and speaks, so "I just looked over there a moment ago" has to be
# derivable from the string. Keyed on the enum so a fourth outcome cannot be added without an
# answer to "and what does the robot say about it".
_LOOK_AT_REASONS: Mapping[LookAtResult, str] = MappingProxyType(
    {
        LookAtResult.COOLING_DOWN: "just moved a moment ago — ask again shortly",
        LookAtResult.NO_AXIS: "this robot has no axis that can look that way",
    }
)


def _parse_direction(name: str) -> Direction:
    """Map the model's string to a :class:`~avid.domain.Direction`, or raise.

    The raise becomes a tool error in the dispatcher, so an invented direction is told to the
    model and the turn continues (AC-6) — the same treatment ``kind`` and ``affect`` get. The
    JSON-Schema ``enum`` should make this unreachable; it is here because *should* is not a
    guarantee about a model, and the alternative to raising is a ``KeyError`` in the pump.
    """
    try:
        return Direction[name.strip().upper()]
    except KeyError:
        allowed = ", ".join(d.name.lower() for d in Direction)
        raise ValueError(
            f"unknown direction {name!r} — expected one of {allowed}"
        ) from None


def _parse_affect(name: str) -> Affect:
    """Map the model's string to a Tier-2 :class:`~avid.domain.Affect`, or raise (AC-3).

    Only the semantic overlays are reachable. A Tier-1 baseline — IDLE, LISTENING, THINKING,
    SPEAKING — is the state machine's to set, and SLEEPING is presence's; letting the model reach
    one would let it overwrite the baseline §6.8 depends on being never wrong. The raise becomes a
    tool error in the dispatcher, so the model is told and the turn continues."""
    wanted = name.strip().lower()
    for affect in SEMANTIC_AFFECTS:
        if affect.name.lower() == wanted:
            return affect
    allowed = ", ".join(a.name.lower() for a in SEMANTIC_AFFECTS)
    raise ValueError(f"unknown affect {name!r} — expected one of: {allowed}")


def _load_object(arguments: str) -> dict[str, Any]:
    """Parse the model's raw JSON argument string into an object, or raise :class:`ValueError`.

    A non-object (a bare list/number/string) is as malformed as invalid JSON for our tools, so it
    is rejected the same way — the caller turns either into a tool error (AC-6)."""
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_schedule(raw: object, default_timezone: str) -> RoutineSpec | None:
    """``remember_fact``'s optional ``schedule`` object → a :class:`RoutineSpec`, or ``None``.

    Raises :class:`ValueError` on anything malformed, which the dispatcher turns into a tool error
    — the same treatment ``kind`` and ``importance`` get. It deliberately does **not** validate the
    RRULE, the ``HH:MM`` or the zone: :func:`avid.core.schedule.next_occurrence` is the authority on
    all three, ``MemoryService`` resolves the spec before writing it, and a second copy of those
    rules here would be a copy free to drift.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object with rrule and local_time")
    spec = RoutineSpec(
        rrule=str(raw["rrule"]),
        local_time=str(raw["local_time"]),
        timezone=str(raw.get("timezone") or default_timezone),
    )
    return spec


def _fact_view(fact: Fact) -> dict[str, Any]:
    """The model-facing projection of a recalled fact: what it needs to answer, nothing else.

    Just the text, kind and importance — the store's ids, timestamps and supersession pointers are
    ours, not the model's, so they never cross into the tool output."""
    return {"text": fact.text, "kind": fact.kind, "importance": fact.importance}


__all__ = [
    "CAPABILITY_INSTRUCTIONS",
    "FORGET",
    "RECALL",
    "REMEMBER_FACT",
    "SET_AFFECT",
    "SET_QUIET",
    "TOOL_SCHEMAS",
    "dispatch_tool_call",
]
