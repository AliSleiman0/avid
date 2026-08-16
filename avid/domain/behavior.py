"""The interruption policy — R-08's mitigation, as a pure function (SDS §10.2, §10.4).

**A trigger does not decide to speak. It decides to *propose* speaking.** Everything that decides
whether the robot is about to be annoying lives here, in :func:`evaluate_policy`, and it is a total
function of its arguments: no clock, no bus, no database, no config file. §10.2 is explicit about why
that matters — *"the entire 'is the robot annoying' question is a table-driven unit test that runs in
milliseconds on a laptop, and every future 'it interrupted me during X' bug becomes a new row in that
table rather than an archaeology expedition through three services."*

R-08 scores 15, the highest in PMP §9.2's register, and it is the risk this project is least equipped
to notice in the field: a robot that talks over a call gets unplugged, and an unplugged robot has a
recall rate of zero. §10.1's asymmetry is therefore the whole design — **default to under-firing**.
A missed coffee reminder is disappointing; the other error is terminal.

The six rules are evaluated in order and **the first veto wins**. That ordering is not cosmetic: it
is what makes a suppression reason diagnostic. §10.6 tunes the policy by asking "what vetoed, and how
often" — if a proposal blocked by both quiet hours and the daily budget reported ``daily_budget``,
the histogram would say the budget is too tight when the truth is that it was half past eleven.

Note what is *not* here. Assembling a :class:`PolicyContext` — reading the clock, converting to the
routine's timezone, counting today's deliveries, aging the last presence event — is
``BehaviorService``'s job, because every one of those touches the world. The split is the reason this
module has no imports beyond the stdlib and one sibling enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from avid.domain.state import RobotState

# ── Rule identities ──────────────────────────────────────────────────────────────────────────

QUIET_HOURS = "quiet_hours"
STATE = "state"
PRESENCE = "presence"
AMBIENT_SPEECH = "ambient_speech"
COOLDOWN = "cooldown"
DAILY_BUDGET = "daily_budget"

#: Every rule that can veto, pinned in one place (SDS §14.2 names these exactly).
#:
#: These strings are not internal. They travel two ways — as ``rule`` on
#: ``behavior.proactive_suppressed`` (§9.1.3) and as ``reason`` in the ``proactive_log`` table
#: (§8.3) — and §10.6's tuning query **groups by them**. A rename on one side and not the other
#: silently splits a histogram bucket in two, which is the single failure R-08's only instrument
#: cannot afford. One vocabulary, fed to both, asserted at the write site.
POLICY_RULES: frozenset[str] = frozenset(
    {QUIET_HOURS, STATE, PRESENCE, AMBIENT_SPEECH, COOLDOWN, DAILY_BUDGET}
)

#: The states a proactive turn may begin from (§10.4 rule 2, §3.10.3).
#:
#: **IDLE alone**, and the SDS says so since M10. The cell read ``∉ {IDLE, SLEEPING}`` until this
#: was implemented and the SLEEPING half turned out to be unreachable: presence is what wakes the
#: robot — ``(SLEEPING, VISION_PRESENCE_GAINED) → IDLE`` is a shipped row — and rule 3 requires
#: presence newer than ``presence_window_s`` (300 s), which cannot coexist with the ten minutes of
#: absence that produce SLEEPING. Admitting SLEEPING would have obliged a
#: ``(SLEEPING, behavior.trigger_fired)`` transition that nothing could ever drive.
#:
#: Read plainly: **proactivity does not wake a sleeping robot.** It speaks to someone already there.
PROACTIVE_STATES: frozenset[RobotState] = frozenset({RobotState.IDLE})


# ── The quiet window ─────────────────────────────────────────────────────────────────────────


def within_quiet_window(
    local_minutes: int, *, start_minutes: int, end_minutes: int
) -> bool:
    """Whether a local wall-clock time falls inside a daily do-not-disturb window (§10.4 rule 1).

    All three arguments are **minutes since local midnight**, in the routine's own timezone.
    Half-open ``[start, end)``: a window ending at 07:30 does not cover 07:30, which is what lets
    a 07:30 trigger fire on the boundary rather than being swallowed by an off-by-one.

    **The window wraps midnight**, and that is the shipped case rather than an edge case: the
    default is 22:00 → 07:30, so ``start > end`` and a naive ``start <= now < end`` is false all
    night — quiet hours would simply never apply, on the one rule §10.4 calls "hard, non-negotiable".

    This lives in ``domain/`` and ``core.config.QuietHours.covers`` delegates to it, rather than the
    other way round, because the layer rule only permits that direction (P1) and two implementations
    of a midnight wrap is exactly one too many.
    """
    if start_minutes <= end_minutes:
        return start_minutes <= local_minutes < end_minutes
    return local_minutes >= start_minutes or local_minutes < end_minutes


# ── Values ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyLimits:
    """The thresholds the six rules compare against — injected from ``[behavior]``, never read.

    Parameters rather than constants for the same reason :class:`~avid.domain.vision.PresenceParams`
    is: §10.6's whole argument is that these numbers get tuned against a real suppression histogram
    after the robot has been living with someone for a week, and a threshold baked into a function
    body makes that a code change instead of a config edit.
    """

    quiet_start_minutes: int  # local midnight offset of the quiet window's start
    quiet_end_minutes: int  # ...and its end; start > end means it wraps midnight
    presence_window_s: float  # rule 3: presence must be newer than this
    # rule 4: un-attributed speech above this suppresses
    ambient_speech_threshold_s: float
    global_cooldown_s: float  # rule 5: no proactive turn within this of the last one
    daily_budget: int  # rule 6: a ceiling on deliveries per local day


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyContext:
    """Everything the six rules read, and nothing else (SDS §10.4, §14.2 pins these names).

    Assembled by ``BehaviorService`` at the moment a trigger comes due. Ages are **seconds since**,
    not timestamps, so the rules never subtract and never need to know what kind of clock produced
    them — ``inf`` is the honest value for "never happened", and it makes every ``>`` comparison
    read correctly without a ``None`` branch per rule.

    ``local_minutes`` rather than deriving from ``now``: rule 1 is wall-clock, in the routine's IANA
    zone, and resolving a zone is neither pure nor available in ``domain/`` (§10.3 — and on a
    Windows dev box ``zoneinfo`` cannot resolve an IANA name at all). The service converts; the gate
    compares. ``now`` is carried only for the ``quiet_until`` override, which is an absolute instant.
    """

    now: int  # epoch seconds, UTC (§8.2) — used only against quiet_until
    local_minutes: int  # minutes since local midnight, in [behavior] timezone
    state: RobotState
    presence_age_s: float  # inf if nobody has ever been seen
    ambient_speech_s: float  # un-attributed speech inside the window (§10.4 rule 4)
    last_proactive_s: float  # inf if none has been delivered yet
    delivered_today: int
    trigger_last_fired_s: float  # inf if this trigger has never fired
    trigger_cooldown_s: float  # this trigger's own cooldown, doubled by §10.5's backoff
    quiet_until: int | None = None  # set_quiet override (§10.4), epoch seconds


@dataclass(frozen=True, slots=True, kw_only=True)
class Delivered:
    """No rule vetoed: the proposal becomes a turn.

    Carries nothing. The utterance does not exist yet — §10.8 composes it from a context block once
    a session is open, and the model writes the words — so there is nothing to report here but the
    verdict itself.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Suppressed:
    """A rule vetoed. ``rule`` is always a member of :data:`POLICY_RULES`.

    This is not a failure and is never silent: §10.6 requires a ``proactive_log`` row for *every*
    considered proposal, delivered or not, because "it never fired" and "it fired and was vetoed
    forty times" are indistinguishable from outside the database — and only one of those means rule
    4 is too aggressive.
    """

    rule: str


PolicyResult: TypeAlias = Delivered | Suppressed


# ── The gate ─────────────────────────────────────────────────────────────────────────────────


def evaluate_policy(ctx: PolicyContext, *, limits: PolicyLimits) -> PolicyResult:
    """Apply §10.4's six rules in order; the first veto wins.

    Pure — no I/O, no clock, no globals (§9.1.4 lists this function by name as having "no I/O by
    construction"). Returns :class:`Delivered` or :class:`Suppressed`; the caller logs either way.

    The order is normative and each rule earns its position:

    1. **Quiet hours** — hard, and first because nothing overrides it. A manual ``set_quiet``
       override stacks *on top of* the static window rather than replacing it, so "leave me alone
       for an hour" at 21:30 does not accidentally end quiet hours at 22:30.
    2. **State** — never interrupt a turn in progress. IDLE only; see :data:`PROACTIVE_STATES`.
    3. **Presence** — a robot talking to an empty room is not proactive, it is a robot talking to
       itself, and it will do so two hundred times before anyone notices.
    4. **Ambient speech** — §2.4's meeting requirement. Sustained speech that never became a
       conversation is speech directed at someone who is not the robot.
    5. **Cooldown** — per-trigger *or* global. Both, because five different well-behaved triggers
       are still five interruptions in a row.
    6. **Daily budget** — a ceiling, not a target. G3 asks for ≥1 per day against a budget of 5.
    """
    if _is_quiet(ctx, limits=limits):
        return Suppressed(rule=QUIET_HOURS)
    if ctx.state not in PROACTIVE_STATES:
        return Suppressed(rule=STATE)
    if ctx.presence_age_s > limits.presence_window_s:
        return Suppressed(rule=PRESENCE)
    if ctx.ambient_speech_s > limits.ambient_speech_threshold_s:
        return Suppressed(rule=AMBIENT_SPEECH)
    if (
        ctx.trigger_last_fired_s < ctx.trigger_cooldown_s
        or ctx.last_proactive_s < limits.global_cooldown_s
    ):
        return Suppressed(rule=COOLDOWN)
    if ctx.delivered_today >= limits.daily_budget:
        return Suppressed(rule=DAILY_BUDGET)
    return Delivered()


def _is_quiet(ctx: PolicyContext, *, limits: PolicyLimits) -> bool:
    """Rule 1: the configured window, or an active manual override (§10.4's "manual override").

    The override is checked first and independently. §10.4 is blunt about why it exists — *"'leave
    me alone for an hour' is a thing people say to companions, and it must work the first time,
    without configuration, or rule 1's static window carries the whole load."* It is an absolute
    instant, so it is the one comparison in this module that reads ``now`` rather than an age.
    """
    if ctx.quiet_until is not None and ctx.now < ctx.quiet_until:
        return True
    return within_quiet_window(
        ctx.local_minutes,
        start_minutes=limits.quiet_start_minutes,
        end_minutes=limits.quiet_end_minutes,
    )


__all__ = [
    "AMBIENT_SPEECH",
    "COOLDOWN",
    "DAILY_BUDGET",
    "POLICY_RULES",
    "PRESENCE",
    "PROACTIVE_STATES",
    "QUIET_HOURS",
    "STATE",
    "Delivered",
    "PolicyContext",
    "PolicyLimits",
    "PolicyResult",
    "Suppressed",
    "evaluate_policy",
    "within_quiet_window",
]
