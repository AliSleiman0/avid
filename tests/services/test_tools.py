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
from avid.domain import (
    FACT_KINDS,
    SEMANTIC_AFFECTS,
    Affect,
    Direction,
    Fact,
    LookAtResult,
    RoutineSpec,
)
from avid.services.tools import (
    CAPABILITY_INSTRUCTIONS,
    FORGET,
    LOOK_AT,
    RECALL,
    REMEMBER_FACT,
    SET_AFFECT,
    SET_QUIET,
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
        self.schedule: RoutineSpec | None = None
        self._deleted = deleted

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        schedule: RoutineSpec | None = None,
        correlation_id: UUID | None = None,
    ) -> int:
        self.remembered.append((text, kind, importance, correlation_id))
        self.schedule = schedule
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
        schedule: RoutineSpec | None = None,
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


# The [behavior] timezone the composition root injects (P7). Named once here so the fixture is
# obviously a stand-in for injected config rather than a literal anyone should copy.
_TEST_TZ = "Asia/Beirut"


class _RecordingBehavior:
    """A :class:`~avid.core.ports.BehaviorTools` double — a fake, not a mock (SDS §14.3)."""

    def __init__(self, *, until: int = 1_800_003_600) -> None:
        self.quiets: list[tuple[int, UUID]] = []
        self._until = until

    async def set_quiet(self, duration_s: int, *, correlation_id: UUID) -> int:
        self.quiets.append((duration_s, correlation_id))
        return self._until


class _RecordingGesture:
    """A :class:`~avid.core.ports.GestureTools` double (#204) — a fake, not a mock (SDS §14.3).

    ``outcome`` is settable so a test can drive the two decline paths through the dispatcher
    without owning a servo: what the dispatcher must get right is the *shape* of each answer —
    ``{ok: true}`` versus ``{ok: false, reason}`` — while whether to decline is
    ``MotionService``'s judgement, tested there."""

    def __init__(self, *, outcome: LookAtResult = LookAtResult.ACCEPTED) -> None:
        self.looks: list[tuple[Direction, UUID]] = []
        self.outcome = outcome

    async def look_at(
        self, direction: Direction, *, correlation_id: UUID
    ) -> LookAtResult:
        self.looks.append((direction, correlation_id))
        return self.outcome


class _BoomGesture:
    """A ``GestureTools`` that raises — AC-6's rule applied to the new route."""

    async def look_at(
        self, direction: Direction, *, correlation_id: UUID
    ) -> LookAtResult:
        raise OSError("[Errno 121] Remote I/O error")


async def _dispatch(
    memory: object,
    call: ToolCallRequested,
    *,
    approximate: bool = False,
    affect: object | None = None,
    behavior: object | None = None,
    gesture: object | None = None,
    default_timezone: str = _TEST_TZ,
) -> dict[str, object]:
    """Dispatch and parse the tool output back to a dict for assertion."""
    output = await dispatch_tool_call(
        memory,  # type: ignore[arg-type]
        call,
        affect=affect or _RecordingAffect(),  # type: ignore[arg-type]
        behavior=behavior or _RecordingBehavior(),  # type: ignore[arg-type]
        gesture=gesture or _RecordingGesture(),  # type: ignore[arg-type]
        correlation_id=uuid4(),
        approximate=approximate,
        default_timezone=default_timezone,
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
        behavior=_RecordingBehavior(),  # type: ignore[arg-type]
        gesture=_RecordingGesture(),  # type: ignore[arg-type]
        correlation_id=corr,
        approximate=False,
        default_timezone=_TEST_TZ,
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


def test_capability_instructions_both_invite_and_bound_set_affect() -> None:
    """AC-5, with the emphasis corrected by measurement.

    The first version of this test asserted only the *constraint* ("not on ordinary replies"),
    because §6.5's finding is that negative constraints are followed far more reliably than
    encouragements. Measured live at the M6 gate, that finding operated against us: across 20 turns
    including an unambiguously sad utterance and an unambiguously delighted one, set_affect fired
    **zero** times. The suppression was doing all the work.

    So both halves are asserted now — the invitation *and* the bound — because a clause with only
    one of them is what produced a tool nothing ever called."""
    assert "set_affect" in CAPABILITY_INSTRUCTIONS
    assert "call set_affect so your face matches your words" in CAPABILITY_INSTRUCTIONS
    assert "Skip it for neutral replies" in CAPABILITY_INSTRUCTIONS


# --- the shipped declarations (AC-2/AC-4) ------------------------------------------------------


def test_tool_schemas_declare_the_six_tools() -> None:
    """Three memory tools, the face, §10.4's manual override (#243), and — since #204 — the body.

    An exact-set assertion rather than a subset: the declarations are the *cached prefix* (§6.2.2),
    so a tool appearing here that nothing dispatches is billed on every turn of every session for
    a capability the robot does not have. ``look_at`` grows that prefix slightly on every session;
    it is cached, so the cost is negligible, but it is not zero."""
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {REMEMBER_FACT, RECALL, FORGET, SET_AFFECT, SET_QUIET, LOOK_AT}
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


# --- remember_fact's schedule argument (#314, SDS §6.6, §10.3) -------------------------------


async def test_the_schema_declares_schedule_as_optional_with_a_required_shape() -> None:
    """Optional at the top level — most facts are not routines — but ``rrule`` and ``local_time``
    are required *within* it, because a schedule missing either is not a schedule and would fail at
    the resolver instead of at the model, one layer too late to be correctable."""
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == REMEMBER_FACT)
    params = schema["parameters"]
    assert "schedule" not in params["required"]
    schedule = params["properties"]["schedule"]
    assert schedule["required"] == ["rrule", "local_time"]
    assert set(schedule["properties"]) == {"rrule", "local_time", "timezone"}


async def test_the_capability_instruction_scopes_the_schedule_rather_than_urging_it() -> (
    None
):
    """The M6 finding cuts the other way here. ``set_affect`` needed encouragement because the
    model would not call the tool at all; ``schedule`` rides a tool the model already calls
    reliably, so the risk is over-attachment — "I usually get coffee in the mornings" has no clock
    time, and a schedule invented for it fires at a moment nobody chose. Under-filling is
    recoverable; a wrong hour is a reminder at the wrong hour."""
    assert "schedule argument" in CAPABILITY_INSTRUCTIONS
    assert "do not guess" in CAPABILITY_INSTRUCTIONS.lower()


async def test_a_schedule_reaches_the_port_as_a_domain_value() -> None:
    memory = _RecordingMemory(fact_id=7)
    out = await _dispatch(
        memory,
        _call(
            REMEMBER_FACT,
            '{"text": "coffee at 8", "kind": "routine", "importance": 6, '
            '"schedule": {"rrule": "FREQ=DAILY", "local_time": "08:00", '
            '"timezone": "Europe/London"}}',
        ),
    )
    assert out == {"ok": True, "fact_id": 7}
    assert memory.schedule == RoutineSpec(
        rrule="FREQ=DAILY", local_time="08:00", timezone="Europe/London"
    )


async def test_an_omitted_timezone_falls_back_to_the_injected_default() -> None:
    """A default rather than a required field: the common case is a user in their own timezone
    describing their own morning, and making the model restate it every time is one more field it
    can get wrong for no benefit. The value is injected (P7), never read here."""
    memory = _RecordingMemory(fact_id=7)
    await _dispatch(
        memory,
        _call(
            REMEMBER_FACT,
            '{"text": "coffee at 8", "kind": "routine", "importance": 6, '
            '"schedule": {"rrule": "FREQ=DAILY", "local_time": "08:00"}}',
        ),
    )
    assert memory.schedule is not None
    assert memory.schedule.timezone == _TEST_TZ


async def test_no_schedule_means_none_not_an_empty_spec() -> None:
    """Absent and empty are different, and only one of them means "this fact has no time"."""
    memory = _RecordingMemory(fact_id=7)
    await _dispatch(
        memory,
        _call(
            REMEMBER_FACT,
            '{"text": "the user likes tea", "kind": "preference", "importance": 4}',
        ),
    )
    assert memory.schedule is None


@pytest.mark.parametrize(
    "bad",
    [
        '"schedule": "every day"',  # a string, not an object
        '"schedule": {"local_time": "08:00"}',  # no rrule
        '"schedule": {"rrule": "FREQ=DAILY"}',  # no local_time
    ],
)
async def test_a_malformed_schedule_is_a_tool_error_not_a_crash(bad: str) -> None:
    """Same discipline as ``kind`` and ``importance``: the turn continues and the model is told,
    because a bad tool call must never reach the pump (AC-6)."""
    memory = _RecordingMemory(fact_id=7)
    out = await _dispatch(
        memory,
        _call(
            REMEMBER_FACT,
            '{"text": "x", "kind": "routine", "importance": 5, ' + bad + "}",
        ),
    )
    assert out["ok"] is False
    assert memory.remembered == [], "nothing may be written on a malformed schedule"


# --- set_quiet (#243, SDS §6.6, §10.4) ------------------------------------------------------


async def test_set_quiet_reaches_the_port_and_returns_the_instant() -> None:
    """The return value is what the model tells the user, so it is the *resolved instant* rather
    than an echo of the request: "until 3 o'clock" is checkable, "for an hour" is not."""
    behavior = _RecordingBehavior(until=1_800_003_600)
    out = await _dispatch(
        _RecordingMemory(fact_id=1),
        _call(SET_QUIET, '{"duration_s": 3600}'),
        behavior=behavior,
    )
    assert out == {"ok": True, "until": 1_800_003_600}
    assert [duration for duration, _ in behavior.quiets] == [3600]


@pytest.mark.parametrize(
    "arguments",
    ['{"duration_s": "an hour"}', "{}", "not json", '{"duration_s": null}'],
)
async def test_a_malformed_quiet_duration_is_a_tool_error(arguments: str) -> None:
    """Same discipline as every other tool: the turn continues and the model is told, because a bad
    tool call must never reach the pump (AC-3)."""
    behavior = _RecordingBehavior()
    out = await _dispatch(
        _RecordingMemory(fact_id=1), _call(SET_QUIET, arguments), behavior=behavior
    )
    assert out["ok"] is False
    assert behavior.quiets == []


async def test_a_raising_behavior_port_becomes_a_tool_error() -> None:
    """A port that rejects the duration (zero, negative) surfaces as a tool error, not a crash."""

    class _Boom:
        async def set_quiet(self, duration_s: int, *, correlation_id: UUID) -> int:
            raise ValueError("quiet duration must be positive")

    out = await _dispatch(
        _RecordingMemory(fact_id=1),
        _call(SET_QUIET, '{"duration_s": 0}'),
        behavior=_Boom(),
    )
    assert out["ok"] is False


def test_the_capability_instruction_only_permits_an_explicit_request() -> None:
    """⚠️ AC-4, and the constraint is the load-bearing half.

    A model that self-quiets speculatively — because the user sounded busy, or answered curtly —
    produces a robot that goes silent for reasons the user never asked for and cannot see.
    Under-firing is §10.1's cheap error; **unexplained** silence is not, because the user has no way
    to tell it from a broken robot.
    """
    assert "call set_quiet" in CAPABILITY_INSTRUCTIONS
    assert "Only when they ask" in CAPABILITY_INSTRUCTIONS
    assert "never because you think they might want it" in CAPABILITY_INSTRUCTIONS


# --- look_at (#204, SDS §6.6) ------------------------------------------------


def test_the_direction_enum_is_the_domain_direction() -> None:
    """Derived from the domain enum, not spelled out — the same rule ``kind`` follows.

    The model is then **structurally prevented** from inventing a sixth direction, and the
    schema it is told about cannot drift from the code that executes its calls. A hand-written
    list here would be a second copy of the vocabulary, free to diverge the first time one grows.
    """
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == LOOK_AT)
    enum = schema["parameters"]["properties"]["direction"]["enum"]
    assert enum == [d.name.lower() for d in Direction]
    assert schema["parameters"]["required"] == ["direction"]


def test_the_direction_is_an_enum_and_never_an_angle() -> None:
    """§3.9.3, as a property of the schema rather than a paragraph.

    ``move(channel, degrees)`` is the shape this deliberately is not: an intent keeps one tool
    working across the 2-servo robot, the 1-servo fallback and the fake, and it is what stops
    the model being handed a lever it can jam. A numeric parameter appearing here would be that
    regression, and it would look like a feature in the diff."""
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == LOOK_AT)
    properties = schema["parameters"]["properties"]
    assert set(properties) == {"direction"}
    assert properties["direction"]["type"] == "string"


@pytest.mark.parametrize("direction", list(Direction), ids=lambda d: d.name.lower())
async def test_every_direction_dispatches_to_the_port(direction: Direction) -> None:
    """Parametrised over the domain enum, so a sixth direction is covered the day it exists."""
    gesture = _RecordingGesture()
    out = await _dispatch(
        _RecordingMemory(),
        _call(LOOK_AT, json.dumps({"direction": direction.name.lower()})),
        gesture=gesture,
    )
    assert out == {"ok": True}
    assert [d for d, _ in gesture.looks] == [direction]


async def test_the_turn_carries_its_correlation_id_into_the_gesture() -> None:
    """§3.12.2: the movement traces back to the turn that caused it.

    Propagated, never minted — a gesture with no turn behind it is a fact nothing can be
    grepped back to the conversation that produced it."""
    gesture = _RecordingGesture()
    corr = uuid4()
    await dispatch_tool_call(
        _RecordingMemory(),  # type: ignore[arg-type]
        _call(LOOK_AT, '{"direction": "left"}'),
        affect=_RecordingAffect(),  # type: ignore[arg-type]
        behavior=_RecordingBehavior(),  # type: ignore[arg-type]
        gesture=gesture,  # type: ignore[arg-type]
        correlation_id=corr,
        approximate=False,
        default_timezone=_TEST_TZ,
    )
    assert [c for _, c in gesture.looks] == [corr]


async def test_it_returns_immediately_rather_than_awaiting_the_sweep() -> None:
    """§6.6 classifies this async/fire-and-forget, and the reason is audible.

    A gesture is the better part of a second. Awaiting one before returning
    ``function_call_output`` stalls the turn, and step 5's ``response.create`` then lands late
    enough to be heard as dead air — the same argument §6.8 makes for ``set_affect``'s ~400 ms.
    The port's contract carries the "returns on acceptance" half; what this asserts is that the
    dispatcher does not add a wait of its own."""
    out = await _dispatch(_RecordingMemory(), _call(LOOK_AT, '{"direction": "center"}'))
    assert out == {"ok": True}
    assert "duration_ms" not in out


@pytest.mark.parametrize(
    ("outcome", "fragment"),
    [
        (LookAtResult.COOLING_DOWN, "moment ago"),
        (LookAtResult.NO_AXIS, "no axis"),
    ],
    ids=["cooldown", "no_axis"],
)
async def test_a_decline_is_a_successful_call_reporting_a_refusal(
    outcome: LookAtResult, fragment: str
) -> None:
    """⚠️ ``{ok: false, reason}``, **not** a tool error — and the distinction is not pedantry.

    A tool *error* tells the model something malfunctioned, and it apologises for a breakage
    that did not happen. A declined call is the robot working correctly and saying so, which the
    model can turn into *"I just looked over there"* or *"I cannot look up"*. The reason string
    is written to be spoken, not logged.

    The silent third option — returning ``{ok: true}`` and doing nothing — is the one that must
    never exist: it leaves the model believing it moved, and a robot that describes motion that
    never happened is worse than one that says it cannot."""
    out = await _dispatch(
        _RecordingMemory(),
        _call(LOOK_AT, '{"direction": "up"}'),
        gesture=_RecordingGesture(outcome=outcome),
    )
    assert out["ok"] is False
    assert "error" not in out, "a decline is not a malfunction"
    assert fragment in str(out["reason"])


async def test_an_invented_direction_is_a_tool_error_and_the_turn_continues() -> None:
    """The JSON-Schema ``enum`` should make this unreachable. *Should* is not a guarantee about
    a model, and the alternative to catching it is a ``KeyError`` reaching the Realtime pump
    (AC-6). The message names what was allowed, so the model can correct itself."""
    gesture = _RecordingGesture()
    out = await _dispatch(
        _RecordingMemory(),
        _call(LOOK_AT, '{"direction": "sideways"}'),
        gesture=gesture,
    )
    assert out["ok"] is False
    assert "sideways" in str(out["error"])
    assert gesture.looks == [], "an invalid direction reached the port"


async def test_malformed_arguments_are_a_tool_error() -> None:
    """Same treatment as every other tool: the turn continues (AC-6)."""
    out = await _dispatch(_RecordingMemory(), _call(LOOK_AT, "{not json"))
    assert out["ok"] is False
    assert "error" in out


async def test_a_raising_port_becomes_a_tool_error_rather_than_reaching_the_pump() -> (
    None
):
    """A servo fault must not crash a conversation.

    ``MotionService`` already handles a fault mid-sweep (#203) and stays live; this is the other
    end — a port that raises on the way in. Both roads lead to the same place: the model is told,
    and the turn goes on."""
    out = await _dispatch(
        _RecordingMemory(),
        _call(LOOK_AT, '{"direction": "left"}'),
        gesture=_BoomGesture(),
    )
    assert out["ok"] is False
    assert "look_at failed" in str(out["error"])


def test_the_instruction_leads_with_the_constraint_not_the_encouragement() -> None:
    """§6.5's finding, applied in the direction this tool needs it.

    ``set_affect`` had to be **re-weighted toward encouragement** (AVID-214/216) because the
    model would not call it at all. ``look_at`` is the opposite case and is weighted like
    ``set_quiet``: it rides an explicit request, so the failure mode is not under-firing but a
    model that decorates every reply with a gesture — which would run the servos continuously,
    contradict the gate's relax clause, and load the rail #206 measures.

    So the sentence that *stops* it gesturing constantly matters more than the one that enables
    it, and this asserts the negative clause is actually present rather than trusting that
    someone kept it."""
    assert "call look_at" in CAPABILITY_INSTRUCTIONS
    assert "never as decoration" in CAPABILITY_INSTRUCTIONS
