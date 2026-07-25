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
from avid.domain import FACT_KINDS, Fact
from avid.services.tools import (
    CAPABILITY_INSTRUCTIONS,
    FORGET,
    RECALL,
    REMEMBER_FACT,
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
    memory: object, call: ToolCallRequested, *, approximate: bool = False
) -> dict[str, object]:
    """Dispatch and parse the tool output back to a dict for assertion."""
    output = await dispatch_tool_call(
        memory,  # type: ignore[arg-type]
        call,
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
    out = await _dispatch(memory, _call("set_affect", '{"affect": "happy"}'))
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


# --- the shipped declarations (AC-2/AC-4) ------------------------------------------------------


def test_tool_schemas_declare_the_three_tools() -> None:
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {REMEMBER_FACT, RECALL, FORGET}
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
