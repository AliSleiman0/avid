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
from typing import Any
from uuid import UUID

from avid.core.ports import AffectTools, MemoryTools
from avid.core.realtime import ToolCallRequested
from avid.domain import FACT_KINDS, SEMANTIC_AFFECTS, Affect, Fact

_log = logging.getLogger(__name__)

# The three tool names (§6.6). Constants, not literals, so the schema declaration below and the
# dispatch routing cannot disagree about a spelling.
REMEMBER_FACT = "remember_fact"
RECALL = "recall"
FORGET = "forget"
SET_AFFECT = "set_affect"

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
    # §6.5's finding governs the shape of this: the clause that STOPS it firing matters more
    # than the one that enables it. An affect set on every reply is a flickering face, and the
    # Tier-1 baseline is already correct without any help (§6.8).
    "Your face already shows whether you are listening, thinking or speaking, so call "
    "set_affect only when the emotional tone of a reply is genuinely different — happy, sad "
    "or confused — and not on ordinary replies."
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
    correlation_id: UUID,
    approximate: bool,
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

        if call.name == SET_AFFECT:
            args = _load_object(call.arguments)
            await affect.set_affect(
                _parse_affect(str(args["affect"])), correlation_id=correlation_id
            )
            # {ok} and nothing else, immediately: §6.6 classifies this async/fire-and-forget,
            # and §6.8's argument for tolerating Tier 2's ~400 ms is that the Tier-1 baseline is
            # never wrong. Awaiting a render would import that latency into the turn for nothing.
            return _result({"ok": True})

        return _error(f"unknown tool {call.name!r}")
    except Exception as exc:  # noqa: BLE001 — AC-6: no tool failure may reach the pump
        _log.warning(
            "tool %r failed [%s]: %s", call.name, correlation_id, exc, exc_info=True
        )
        return _error(f"{call.name} failed: {exc}")


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
    "TOOL_SCHEMAS",
    "dispatch_tool_call",
]
