"""The memory tool contract — schemas, capability text, and dispatch (#125, SDS §6.6/§7.6).

Pure unit tests over :func:`~avid.services.tools.dispatch_tool_call` and the shipped
:data:`~avid.services.tools.TOOL_SCHEMAS` / :data:`~avid.services.tools.CAPABILITY_INSTRUCTIONS`.
The memory port is a small **recording double** (a fake, not a mock — ``unittest.mock`` is banned
outside ``tests/adapters/``, SDS §14.3): the properties under test are which port call a tool maps
to, what the model-facing output looks like, and that no bad call escapes as an exception (AC-6).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from avid.core.realtime import ToolCallRequested
from avid.domain import FACT_KINDS, SEMANTIC_AFFECTS, Affect, Fact
from avid.services.tools import (
    CAPABILITY_INSTRUCTIONS,
    FORGET,
    RECALL,
    REMEMBER_FACT,
    SET_AFFECT,
    TOOL_SCHEMAS,
    dispatch_tool_call,
)


class _RecordingMemory:
    """A :class:`~avid.core.ports.MemoryTools` double that records its calls and returns canned
    results — so a test can assert the dispatcher routed correctly and passed the right arguments."""

    def __init__(
        self,
        *,
        recall_result: Sequence[Fact] = (),
        fact_id: int = 1,
        deleted: int = 0,
    ) -> None:
        self.remembered: list[tuple[str, str, int, UUID | None]] = []
        self.recalled: list[tuple[str, int, UUID | None]] = []
        self.forgotten: list[tuple[str, UUID | None]] = []
        self._recall_result = recall_result
        self._fact_id = fact_id
        self._deleted = deleted

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        correlation_id: UUID | None = None,
    ) -> int:
        self.remembered.append((text, kind, importance, correlation_id))
        return self._fact_id

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> Sequence[Fact]:
        self.recalled.append((query, k, correlation_id))
        return self._recall_result

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        self.forgotten.append((query, correlation_id))
        return self._deleted


class _RecordingAffect:
    """An ``AffectTools`` double that records what the model asked for.

    Note the signature: ``correlation_id`` is **required and keyword-only**, matching the real
    ``AffectService.set_affect`` rather than ``MemoryTools``' optional one. An overlay with no
    turn behind it is a face change nothing can be traced to (§9.1.1)."""

    def __init__(self) -> None:
        self.applied: list[tuple[Affect, UUID]] = []

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        self.applied.append((affect, correlation_id))


class _BoomAffect:
    """The affect port's failure mode — AC-3 says a raising call is still a tool error."""

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        raise RuntimeError("face fell off")


class _BoomMemory:
    """A memory port whose every call raises — the real ``MemoryService`` failure modes (a store
    error, a supersession-judge blow-up) surface here; AC-6 says the dispatcher must catch them."""

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        correlation_id: UUID | None = None,
    ) -> int:
        raise RuntimeError("store exploded")

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> Sequence[Fact]:
        raise RuntimeError("store exploded")

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        raise RuntimeError("store exploded")


def _call(name: str, arguments: str, *, call_id: str = "call_0") -> ToolCallRequested:
    return ToolCallRequested(call_id=call_id, name=name, arguments=arguments)


def _fact(text: str, *, kind: str = "other", importance: int = 5) -> Fact:
    return Fact(
        id=1,
        text=text,
        kind=kind,  # type: ignore[arg-type]
        importance=importance,
        created_at=0,
        last_accessed_at=0,
    )


async def _dispatch(
    memory: object,
    call: ToolCallRequested,
    *,
    approximate: bool = False,
    affect: object | None = None,
) -> dict[str, object]:
    """Dispatch and parse the tool output back to a dict for assertion."""
    output = await dispatch_tool_call(
        memory,  # type: ignore[arg-type]
        call,
        affect=affect or _RecordingAffect(),  # type: ignore[arg-type]
        correlation_id=uuid4(),
        approximate=approximate,
    )
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    return parsed


# --- remember_fact (AC-3/AC-4) -----------------------------------------------------------------


async def test_remember_fact_maps_to_the_port_and_returns_the_id() -> None:
    memory = _RecordingMemory(fact_id=42)
    corr = uuid4()
    out = await dispatch_tool_call(
        memory,  # type: ignore[arg-type]
        _call(
            REMEMBER_FACT,
            '{"text": "the user likes tea", "kind": "preference", "importance": 6}',
        ),
        affect=_RecordingAffect(),
        correlation_id=corr,
        approximate=False,
    )
    assert json.loads(out) == {"ok": True, "fact_id": 42}
    assert memory.remembered == [("the user likes tea", "preference", 6, corr)]


async def test_remember_fact_is_declined_on_an_approximate_turn() -> None:
    # The §6.2.4/§7.6 barge-in guard: an approximate transcript tail is unreliable, so remember_fact
    # does not write and the model is told, honestly, that nothing was stored (confabulation guard).
    memory = _RecordingMemory()
    out = await _dispatch(
        memory,
        _call(REMEMBER_FACT, '{"text": "x", "kind": "other", "importance": 5}'),
        approximate=True,
    )
    assert out["ok"] is False
    assert "approximate" in out["error"]  # type: ignore[operator]
    assert memory.remembered == []  # nothing written


async def test_remember_fact_missing_a_required_field_is_a_tool_error() -> None:
    memory = _RecordingMemory()
    out = await _dispatch(
        memory, _call(REMEMBER_FACT, '{"text": "x", "kind": "other"}')
    )
    assert out["ok"] is False
    assert memory.remembered == []


# --- recall (AC-1) -----------------------------------------------------------------------------


async def test_recall_maps_to_the_port_and_projects_facts() -> None:
    memory = _RecordingMemory(
        recall_result=(_fact("the user's sister is maya", kind="relationship"),)
    )
    out = await _dispatch(memory, _call(RECALL, '{"query": "sister", "k": 3}'))
    assert out == {
        "facts": [
            {
                "text": "the user's sister is maya",
                "kind": "relationship",
                "importance": 5,
            }
        ]
    }
    assert memory.recalled == [("sister", 3, memory.recalled[0][2])]


async def test_recall_defaults_k_when_the_model_omits_it() -> None:
    memory = _RecordingMemory()
    await _dispatch(memory, _call(RECALL, '{"query": "anything"}'))
    assert memory.recalled[0][1] == 5  # the §7.7 default cutoff


# --- forget (AC-5) -----------------------------------------------------------------------------


async def test_forget_maps_to_the_port_and_returns_the_count() -> None:
    memory = _RecordingMemory(deleted=2)
    out = await _dispatch(memory, _call(FORGET, '{"query": "my address"}'))
    assert out == {"deleted": 2}
    assert memory.forgotten[0][0] == "my address"


# --- AC-6: no bad call escapes as an exception -------------------------------------------------


async def test_an_unknown_tool_name_is_a_tool_error() -> None:
    memory = _RecordingMemory()
    # ⚠️ This used to use "set_affect" as its unknown name. AVID-214 routes that tool, so the
    # test would have silently changed meaning — passing for the wrong reason, or failing for a
    # reason unrelated to what it is named for. It needs a name nothing will ever implement.
    out = await _dispatch(memory, _call("teleport", '{"where": "mars"}'))
    assert out["ok"] is False
    assert "unknown tool" in out["error"]  # type: ignore[operator]


@pytest.mark.parametrize(
    "arguments",
    ['{"query": ', "not json at all", "[1, 2, 3]", '"a bare string"', "42"],
)
async def test_malformed_arguments_never_raise(arguments: str) -> None:
    memory = _RecordingMemory()
    out = await _dispatch(memory, _call(RECALL, arguments))
    assert out["ok"] is False  # a tool error, not an exception
    assert memory.recalled == []


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (REMEMBER_FACT, '{"text": "x", "kind": "other", "importance": 5}'),
        (RECALL, '{"query": "anything"}'),
        (FORGET, '{"query": "anything"}'),
    ],
)
async def test_a_raising_handler_becomes_a_tool_error(
    name: str, arguments: str
) -> None:
    # A port call that blows up (a store error, a judge timeout) is caught per tool, never raised
    # into the pump — the model is told the tool failed and the turn continues (AC-6).
    out = await _dispatch(_BoomMemory(), _call(name, arguments))
    assert out["ok"] is False
    assert f"{name} failed" in out["error"]  # type: ignore[operator]


# --- set_affect (AVID-214) ---------------------------------------------------------------------


@pytest.mark.parametrize("affect", SEMANTIC_AFFECTS)
async def test_every_tier_two_affect_dispatches(affect: Affect) -> None:
    """AC-7: each overlay the schema offers reaches the port, carrying the turn's id."""
    memory = _RecordingMemory()
    face = _RecordingAffect()

    out = await _dispatch(
        memory, _call(SET_AFFECT, f'{{"affect": "{affect.name.lower()}"}}'), affect=face
    )

    assert out == {"ok": True}
    assert [applied for applied, _ in face.applied] == [affect]


async def test_a_tier_one_baseline_is_not_settable_by_the_model() -> None:
    """The schema's enum is the Tier-2 subset, and the parser enforces the same bound.

    IDLE/LISTENING/THINKING/SPEAKING are the state machine's to own and SLEEPING is presence's.
    §6.8's whole argument for tolerating Tier 2's ~400 ms latency is that *"the baseline is never
    wrong"* — a model that could overwrite the baseline would spend exactly that property, and it
    would do it invisibly: a THINKING face while the robot is speaking is odd, not an error."""
    memory = _RecordingMemory()
    face = _RecordingAffect()

    for baseline in ("idle", "listening", "thinking", "speaking", "sleeping"):
        out = await _dispatch(
            memory, _call(SET_AFFECT, f'{{"affect": "{baseline}"}}'), affect=face
        )
        assert out["ok"] is False
        assert "unknown affect" in str(out["error"])

    assert face.applied == [], "a Tier-1 baseline reached the affect port"


async def test_an_unknown_affect_is_a_tool_error_not_a_crash() -> None:
    memory = _RecordingMemory()
    face = _RecordingAffect()

    out = await _dispatch(memory, _call(SET_AFFECT, '{"affect": "smug"}'), affect=face)

    assert out["ok"] is False
    assert face.applied == []


async def test_a_raising_affect_port_becomes_a_tool_error() -> None:
    """AC-3: every failure path returns a tool error. A face that fell off must not end a turn."""
    out = await _dispatch(
        _RecordingMemory(),
        _call(SET_AFFECT, '{"affect": "happy"}'),
        affect=_BoomAffect(),
    )

    assert out["ok"] is False
    assert "face fell off" in str(out["error"])


def test_the_set_affect_enum_is_the_domain_tier_two_tuple() -> None:
    """AC-2: derived, not re-listed — the same drift guard ``remember_fact.kind`` has.

    A ninth affect added to ``SEMANTIC_AFFECTS`` appears in the schema for free; one added to
    ``Affect`` alone stays out of the model's reach, which is the correct default."""
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == SET_AFFECT)
    enum = schema["parameters"]["properties"]["affect"]["enum"]  # type: ignore[index]

    assert enum == [a.name.lower() for a in SEMANTIC_AFFECTS]
    assert "idle" not in enum


def test_capability_instructions_tell_the_model_when_not_to_set_an_affect() -> None:
    """AC-5, and the emphasis is deliberate. §6.5's finding is that negative constraints are
    followed far more reliably than encouragements, so the clause that stops it firing every turn
    matters more than the one that enables it — an affect that changes on every reply is a
    flickering face."""
    assert "set_affect" in CAPABILITY_INSTRUCTIONS
    assert "not on ordinary replies" in CAPABILITY_INSTRUCTIONS


# --- the shipped declarations (AC-2/AC-4) ------------------------------------------------------


def test_tool_schemas_declare_the_four_tools() -> None:
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {REMEMBER_FACT, RECALL, FORGET, SET_AFFECT}
    assert all(schema["type"] == "function" for schema in TOOL_SCHEMAS)


def test_remember_fact_kind_enum_is_the_domain_fact_kinds() -> None:
    # AC-2: the schema enum is derived from FACT_KINDS, so the model cannot invent a seventh kind
    # that the §8.3 CHECK then rejects at write time — schema and constraint cannot drift.
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == REMEMBER_FACT)
    kind_enum = schema["parameters"]["properties"]["kind"]["enum"]
    assert kind_enum == list(FACT_KINDS)


def test_capability_instructions_carry_the_load_bearing_clause() -> None:
    # AC-4: without this final clause the model confabulates facts from context (§7.6).
    assert "anything you inferred rather than were told" in CAPABILITY_INSTRUCTIONS
    assert "remember_fact" in CAPABILITY_INSTRUCTIONS
