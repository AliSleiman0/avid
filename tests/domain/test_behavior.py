"""§10.4's interruption policy — the highest-value test file in the project (#235).

SDS §14.2 says so in as many words, and it says why: R-08 ("proactive robot is annoying, user
disables it, core value dies") scores 15 and is *"the risk this project is least equipped to notice
in the field."* Because §10.2 made the gate a pure function over a value object, the whole question
collapses into a table — and every future "it interrupted me during X" becomes one new row here
rather than an investigation across three services.

The table below is §14.2's, in the shipped field names, extended in the two directions the sketch in
the SDS could not cover because it predates the implementation: the per-trigger half of rule 5, and
**first-veto-wins across every ordered pair of rules** rather than the single quiet-vs-presence case.
That exhaustive pass is the one that matters. A gate that vetoes correctly but *reports* the wrong
rule still suppresses the right turns — and then §10.6's histogram, the only instrument R-08 has,
quietly attributes them to the wrong cause.
"""

from __future__ import annotations

import math
from uuid import UUID, uuid4

import pytest

from avid.domain.behavior import (
    AMBIENT_SPEECH,
    COOLDOWN,
    DAILY_BUDGET,
    POLICY_RULES,
    PRESENCE,
    PROACTIVE_STATES,
    QUIET_HOURS,
    STATE,
    AmbientWindow,
    BehaviorProactiveDelivered,
    BehaviorProactiveSuppressed,
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    Delivered,
    PolicyContext,
    PolicyLimits,
    Suppressed,
    ambient_speech_s,
    attribute_speech,
    evaluate_policy,
    record_speech,
    within_quiet_window,
)
from avid.domain.events import Event
from avid.domain.state import RobotState

# The shipped [behavior] numbers (SDS §9.6, config/pi.toml). Written out rather than loaded, because
# a table whose expectations move when someone edits a TOML is not a table — it would assert that the
# gate agrees with itself. `test_these_limits_are_the_shipped_ones` below is the tripwire that keeps
# the mirror honest, so this is a restatement with a guard rather than the drift CLAUDE.md §7.1 warns
# about.
_LIMITS = PolicyLimits(
    quiet_start_minutes=22 * 60,
    quiet_end_minutes=7 * 60 + 30,
    presence_window_s=300,
    ambient_speech_threshold_s=60,
    global_cooldown_s=900,
    daily_budget=5,
)


def at(wall_clock: str) -> int:
    """``"07:55"`` → minutes since local midnight. §14.2's helper, spelled out."""
    hours, _, minutes = wall_clock.partition(":")
    return int(hours) * 60 + int(minutes)


def ctx(**overrides: object) -> PolicyContext:
    """A context that **delivers**, with the one thing under test overridden.

    The baseline is UC-03's own moment: 07:55, the robot idle, the user right there, nothing said
    yet today. Every case below is that morning minus exactly one thing, which is what makes a
    failure point at a rule rather than at a fixture.
    """
    base: dict[str, object] = {
        "now": 1_800_000_000,
        "local_minutes": at("07:55"),
        "state": RobotState.IDLE,
        "presence_age_s": 5.0,
        "ambient_speech_s": 0.0,
        "last_proactive_s": math.inf,
        "delivered_today": 0,
        "trigger_last_fired_s": math.inf,
        "trigger_cooldown_s": 3600.0,
    }
    base.update(overrides)
    return PolicyContext(**base)  # type: ignore[arg-type]  # a test fixture's kwargs, checked by construction


def veto(rule: str) -> Suppressed:
    return Suppressed(rule=rule)


# ── §14.2's table ────────────────────────────────────────────────────────────────────────────

POLICY_CASES = [
    ("quiet_hours_blocks", {"local_minutes": at("23:30")}, veto(QUIET_HOURS)),
    ("mid_conversation", {"state": RobotState.SPEAKING}, veto(STATE)),
    ("empty_room", {"presence_age_s": 900.0}, veto(PRESENCE)),
    ("user_on_a_call", {"ambient_speech_s": 90.0}, veto(AMBIENT_SPEECH)),
    ("global_cooldown", {"last_proactive_s": 300.0}, veto(COOLDOWN)),
    ("budget_spent", {"delivered_today": 5}, veto(DAILY_BUDGET)),
    ("coffee_morning", {"local_minutes": at("07:55")}, Delivered()),
    (
        "quiet_beats_everything",
        {"local_minutes": at("23:30"), "presence_age_s": 1.0},
        veto(QUIET_HOURS),
    ),
    # Beyond §14.2's sketch: rule 5's per-trigger half, which the sketch omits because it only
    # shows the global 15-minute clock. A trigger inside its *own* cooldown is vetoed even when
    # no proactive turn has ever been delivered — that is the half §10.5's backoff doubles.
    (
        "own_cooldown",
        {"trigger_last_fired_s": 600.0, "trigger_cooldown_s": 3600.0},
        veto(COOLDOWN),
    ),
    # ...and the boundary: a trigger exactly at its cooldown has served it.
    (
        "own_cooldown_served",
        {"trigger_last_fired_s": 3600.0, "trigger_cooldown_s": 3600.0},
        Delivered(),
    ),
    # SLEEPING is a veto since M10 reversed the rule-2 cell — proactivity does not wake a
    # sleeping robot, it speaks to someone already there (§10.4 rule 2).
    ("asleep", {"state": RobotState.SLEEPING}, veto(STATE)),
    # The manual override (§10.4), which is why rule 1 is not merely the static window.
    (
        "set_quiet_override",
        {"now": 1_800_000_000, "quiet_until": 1_800_003_600},
        veto(QUIET_HOURS),
    ),
    (
        "set_quiet_expired",
        {"now": 1_800_000_000, "quiet_until": 1_799_999_999},
        Delivered(),
    ),
]


@pytest.mark.parametrize(
    ("case_id", "overrides", "expected"),
    POLICY_CASES,
    ids=[case[0] for case in POLICY_CASES],
)
def test_policy_cases(
    case_id: str, overrides: dict[str, object], expected: Delivered | Suppressed
) -> None:
    assert evaluate_policy(ctx(**overrides), limits=_LIMITS) == expected


# ── First veto wins, exhaustively ────────────────────────────────────────────────────────────

# One override per rule, each sufficient on its own. Order matches §10.4's table, which is the
# order under test — this list IS the claim.
_VIOLATIONS: list[tuple[str, dict[str, object]]] = [
    (QUIET_HOURS, {"local_minutes": at("23:30")}),
    (STATE, {"state": RobotState.SPEAKING}),
    (PRESENCE, {"presence_age_s": 900.0}),
    (AMBIENT_SPEECH, {"ambient_speech_s": 90.0}),
    (COOLDOWN, {"last_proactive_s": 300.0}),
    (DAILY_BUDGET, {"delivered_today": 5}),
]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_VIOLATIONS[i], _VIOLATIONS[j])
        for i in range(len(_VIOLATIONS))
        for j in range(i + 1, len(_VIOLATIONS))
    ],
    ids=[
        f"{_VIOLATIONS[i][0]}_before_{_VIOLATIONS[j][0]}"
        for i in range(len(_VIOLATIONS))
        for j in range(i + 1, len(_VIOLATIONS))
    ],
)
def test_the_earlier_rule_always_wins(
    first: tuple[str, dict[str, object]], second: tuple[str, dict[str, object]]
) -> None:
    """All fifteen ordered pairs, not just §14.2's quiet-vs-presence sample.

    A gate that vetoes correctly but names the wrong rule suppresses exactly the right turns, so
    nothing looks broken — and then §10.6's ``GROUP BY reason`` attributes them to the wrong cause,
    and the number you tune is the wrong number. The reason is the product here, not the verdict.
    """
    expected_rule, first_overrides = first
    _, second_overrides = second
    result = evaluate_policy(
        ctx(**{**first_overrides, **second_overrides}), limits=_LIMITS
    )
    assert result == veto(expected_rule)


def test_every_rule_that_can_veto_is_reachable() -> None:
    """Each member of POLICY_RULES is produced by at least one context.

    The mirror of ``test_every_trigger_drives_at_least_one_row`` in the state table: a rule name in
    the vocabulary that nothing can emit is not documentation, it is a bucket in §10.6's histogram
    that will read zero forever and be believed."""
    produced = {
        rule
        for rule, overrides in _VIOLATIONS
        for result in [evaluate_policy(ctx(**overrides), limits=_LIMITS)]
        if isinstance(result, Suppressed)
        for rule in [result.rule]
    }
    assert produced == POLICY_RULES


def test_a_suppression_never_invents_a_rule_name() -> None:
    """Whatever vetoes, the string is one §10.6 can group by. This is the guard that keeps the
    event's ``rule`` field and the ``proactive_log.reason`` column speaking one vocabulary."""
    for _, overrides in _VIOLATIONS:
        result = evaluate_policy(ctx(**overrides), limits=_LIMITS)
        assert isinstance(result, Suppressed)
        assert result.rule in POLICY_RULES


# ── The quiet window ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("minutes", "inside"),
    [
        (at("23:30"), True),  # after start, before midnight
        (at("02:00"), True),  # after midnight, before end
        (at("22:00"), True),  # inclusive at the start
        (at("07:29"), True),  # ...right up to
        (at("07:30"), False),  # exclusive at the end
        (at("07:55"), False),  # UC-03's coffee reminder must clear rule 1 to exist
        (at("12:00"), False),
        (at("21:59"), False),
    ],
)
def test_the_shipped_window_wraps_midnight(minutes: int, inside: bool) -> None:
    """22:00 → 07:30 is the default, and it crosses midnight: the case where a naive
    ``start <= now < end`` is false all night and the one rule §10.4 calls non-negotiable
    silently never applies."""
    assert (
        within_quiet_window(minutes, start_minutes=at("22:00"), end_minutes=at("07:30"))
        is inside
    )


@pytest.mark.parametrize(
    ("minutes", "inside"),
    [
        (at("12:00"), True),
        (at("09:00"), True),
        (at("17:00"), False),
        (at("08:00"), False),
    ],
)
def test_a_daytime_window_is_not_inverted(minutes: int, inside: bool) -> None:
    """The wrap is the shipped case, not the only one — a focus block during the working day must
    not come out inside-out from the same branch."""
    assert (
        within_quiet_window(minutes, start_minutes=at("09:00"), end_minutes=at("17:00"))
        is inside
    )


def test_the_override_stacks_on_the_window_rather_than_replacing_it() -> None:
    """``set_quiet`` extends quiet, never ends it. "Leave me alone for an hour" at 21:30 must not
    turn into "and then you may talk at 22:30", which is what replacing rule 1's window would do."""
    during_static_quiet = ctx(local_minutes=at("23:00"), quiet_until=1_799_999_999)
    assert evaluate_policy(during_static_quiet, limits=_LIMITS) == veto(QUIET_HOURS)


def test_presence_at_the_window_boundary_still_passes() -> None:
    """Rule 3 vetoes when presence is *older* than the window, not at it. An off-by-one here costs
    a delivery every time someone has been still for exactly five minutes."""
    assert evaluate_policy(ctx(presence_age_s=300.0), limits=_LIMITS) == Delivered()
    assert evaluate_policy(ctx(presence_age_s=300.1), limits=_LIMITS) == veto(PRESENCE)


def test_ambient_speech_at_the_threshold_still_passes() -> None:
    """§10.4 says ">60 s", and the difference matters: at exactly the threshold the user has been
    quiet enough, and a `>=` here would suppress the boundary case every time."""
    assert evaluate_policy(ctx(ambient_speech_s=60.0), limits=_LIMITS) == Delivered()
    assert evaluate_policy(ctx(ambient_speech_s=60.1), limits=_LIMITS) == veto(
        AMBIENT_SPEECH
    )


def test_the_budget_is_a_ceiling_not_a_target() -> None:
    """G3 asks for ≥1 useful proactive event per day against a budget of 5, so the fifth delivery
    is allowed and the sixth is not."""
    assert evaluate_policy(ctx(delivered_today=4), limits=_LIMITS) == Delivered()
    assert evaluate_policy(ctx(delivered_today=5), limits=_LIMITS) == veto(DAILY_BUDGET)


def test_only_idle_may_begin_a_proactive_turn() -> None:
    """Every other operational state is a veto, including SLEEPING (§10.4 rule 2, reversed at M10)
    and DEGRADED — a robot that cannot reach the API has nothing to say unprompted."""
    assert PROACTIVE_STATES == {RobotState.IDLE}
    for state in RobotState:
        result = evaluate_policy(ctx(state=state), limits=_LIMITS)
        if state is RobotState.IDLE:
            assert result == Delivered()
        else:
            assert result == veto(STATE), f"{state} must not begin a proactive turn"


def test_these_limits_are_the_shipped_ones() -> None:
    """The tripwire on the table's mirror of ``[behavior]``.

    "Read config, never restate it" (CLAUDE.md §7.1) exists because a literal in a banner is drift
    with a delay fuse. The table above restates deliberately — expectations that move when a TOML is
    edited would assert only that the gate agrees with itself — so the restatement is pinned here
    instead. Edit ``config/pi.toml`` and this fails, which is the whole point."""
    from pathlib import Path

    from avid.core.config import load_config

    behavior = load_config(
        Path(__file__).resolve().parents[2] / "config" / "pi.toml"
    ).behavior
    assert _LIMITS.quiet_start_minutes == behavior.quiet_hours.start_minutes
    assert _LIMITS.quiet_end_minutes == behavior.quiet_hours.end_minutes
    assert _LIMITS.presence_window_s == behavior.presence_window_s
    assert _LIMITS.ambient_speech_threshold_s == behavior.ambient_speech_threshold_s
    assert _LIMITS.global_cooldown_s == behavior.global_cooldown_s
    assert _LIMITS.daily_budget == behavior.daily_budget


def test_the_gate_reads_nothing_but_its_arguments() -> None:
    """§9.1.4 lists this function as having "no I/O by construction". The cheap mechanical proof:
    the same context evaluated twice, a thousand times apart in wall-clock terms, is the same
    verdict — and ``import-linter``'s domain-purity contract holds the rest."""
    context = ctx()
    assert evaluate_policy(context, limits=_LIMITS) == evaluate_policy(
        context, limits=_LIMITS
    )


# ── The four facts, and rule 4's accumulator (#237, SDS §9.1.3, §10.4) ───────────────────────

_CORR = UUID("11111111-1111-1111-1111-111111111111")
_OTHER = UUID("22222222-2222-2222-2222-222222222222")


def _envelope() -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "correlation_id": _CORR,
        "timestamp_ms": 1_800_000_000_000,
        "monotonic_ns": 0,
        "source": "BehaviorService",
    }


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (BehaviorTriggerFired, "behavior.trigger_fired"),
        (BehaviorProactiveDelivered, "behavior.proactive_delivered"),
        (BehaviorProactiveSuppressed, "behavior.proactive_suppressed"),
        (BehaviorTriggerDisabled, "behavior.trigger_disabled"),
    ],
)
def test_the_names_match_the_catalog(event_type: type[Event], expected: str) -> None:
    """§9.1.3's spellings, and the four `tests/domain/test_events.py` has asserted are valid names
    since before any of these classes existed — the catalog was written first, deliberately."""
    assert event_type.name == expected


def test_the_events_are_frozen_and_slotted() -> None:
    """Handlers dispatch concurrently (§3.5), so a mutable event is a data race with extra steps."""
    fired = BehaviorTriggerFired(**_envelope(), trigger_id=7, fact_id=42)  # type: ignore[arg-type]
    with pytest.raises((AttributeError, TypeError)):
        fired.trigger_id = 8  # type: ignore[misc]


def test_a_trigger_with_no_fact_behind_it_is_expressible() -> None:
    """A presence greeting (§3.7.5) has no routine and therefore no fact. ``None`` rather than a
    sentinel id, so the schema's nullable FK and the event agree."""
    fired = BehaviorTriggerFired(**_envelope(), trigger_id=7)  # type: ignore[arg-type]
    assert fired.fact_id is None


def test_a_suppression_carries_no_utterance_by_default() -> None:
    """Not an omission: the gate runs *before* a session exists, so on almost every suppression
    there is nothing the robot would have said yet — §10.8 composes the words only once the model
    is on the line."""
    vetoed = BehaviorProactiveSuppressed(**_envelope(), trigger_id=7, rule=QUIET_HOURS)  # type: ignore[arg-type]
    assert vetoed.would_have_said is None
    assert vetoed.rule in POLICY_RULES


# ── AmbientWindow ────────────────────────────────────────────────────────────────────────────


def test_speech_that_never_became_a_conversation_counts_as_ambient() -> None:
    """The user is on a call. The VAD hears them constantly and no transcript ever arrives."""
    window = AmbientWindow()
    for i in range(4):
        window = record_speech(
            window,
            correlation_id=uuid4(),
            at_s=float(i * 20),
            duration_s=20.0,
            keep_s=300.0,
        )
    assert ambient_speech_s(window, now_s=80.0, window_s=300.0) == 80.0


def test_speech_that_became_a_conversation_is_retracted() -> None:
    """The user was talking *to the robot*. Counting it would suppress the next proactive turn for
    the crime of having had a conversation — and §10.6's histogram would blame rule 4."""
    window = record_speech(
        AmbientWindow(), correlation_id=_CORR, at_s=0.0, duration_s=90.0, keep_s=300.0
    )
    assert ambient_speech_s(window, now_s=10.0, window_s=300.0) == 90.0

    window = attribute_speech(window, _CORR)
    assert ambient_speech_s(window, now_s=10.0, window_s=300.0) == 0.0


def test_retraction_touches_only_its_own_turn() -> None:
    """Both are needed at once: the user finishes a call (ambient) and then speaks to the robot
    (attributed). Only the second is retracted."""
    window = record_speech(
        AmbientWindow(), correlation_id=_OTHER, at_s=0.0, duration_s=70.0, keep_s=300.0
    )
    window = record_speech(
        window, correlation_id=_CORR, at_s=10.0, duration_s=5.0, keep_s=300.0
    )
    window = attribute_speech(window, _CORR)
    assert ambient_speech_s(window, now_s=20.0, window_s=300.0) == 70.0


def test_attributing_an_unknown_turn_is_a_no_op() -> None:
    """A transcript can arrive for an utterance already pruned out of the window. Not an error."""
    window = record_speech(
        AmbientWindow(), correlation_id=_OTHER, at_s=0.0, duration_s=30.0, keep_s=300.0
    )
    assert attribute_speech(window, _CORR) == window


def test_speech_outside_the_window_does_not_count() -> None:
    """Rule 4 asks about the *last five minutes*. A meeting that ended an hour ago is not a reason
    to stay quiet now."""
    window = record_speech(
        AmbientWindow(), correlation_id=_CORR, at_s=0.0, duration_s=200.0, keep_s=3600.0
    )
    assert ambient_speech_s(window, now_s=100.0, window_s=300.0) == 200.0
    assert ambient_speech_s(window, now_s=400.0, window_s=300.0) == 0.0


def test_the_window_stays_bounded_under_an_always_on_vad() -> None:
    """The case that would otherwise grow forever: a busy room, a VAD that never stops. Pruning
    happens on write, so there is no second pass and no timer."""
    window = AmbientWindow()
    for i in range(1000):
        window = record_speech(
            window,
            correlation_id=uuid4(),
            at_s=float(i),
            duration_s=0.5,
            keep_s=300.0,
        )
    assert len(window.entries) <= 302


def test_the_accumulator_is_a_pure_fold() -> None:
    """No clock, no I/O — time arrives as ``at_s``. An hour of a real room replays identically on
    a laptop, which is the same property ``domain/vision.py``'s presence filter has."""
    window = AmbientWindow()
    first = record_speech(
        window, correlation_id=_CORR, at_s=0.0, duration_s=10.0, keep_s=300.0
    )
    second = record_speech(
        window, correlation_id=_CORR, at_s=0.0, duration_s=10.0, keep_s=300.0
    )
    assert first == second
    assert window.entries == ()  # the input was not mutated
