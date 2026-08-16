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

from dataclasses import dataclass, replace
from typing import ClassVar, TypeAlias
from uuid import UUID

from avid.domain.events import Event
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


@dataclass(frozen=True, slots=True, kw_only=True)
class TriggerRecord:
    """One ``triggers`` row (SDS §8.3), as a value.

    Everything a trigger has done to itself. ``ignore_streak`` and ``cooldown_s`` are the two
    columns §10.5's backoff writes, and they are **persisted rather than held in memory** for a
    reason worth stating: a backoff that resets on reboot is not a backoff, and a robot that is
    politely ignored every morning would go on being ignored every morning forever.

    ``kind`` is ``'schedule'``, ``'presence'`` or ``'condition'`` — §8.3's CHECK constraint. Only
    ``schedule`` has a ``routines`` row behind it; the other two are driven by events.
    """

    id: int
    fact_id: int | None
    kind: str
    enabled: bool
    next_fire_at: int | None  # epoch seconds; NULL keeps it out of idx_triggers_due
    last_fired_at: int | None
    fire_count: int
    ignore_streak: int
    cooldown_s: int


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


# -- The four facts (SDS 9.1.3) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorTriggerFired(Event):
    """A trigger passed the policy gate and the robot is about to speak first (SDS §9.1.3).

    **One of exactly two events that mint a ``correlation_id``** — the other is
    ``audio.speech_started`` (§9.1.1). This is the head of a proactive turn, so every downstream
    event in it carries the id minted here, and one ``grep`` reconstructs the whole thing.

    A fact, not a request (P4): it says the gate passed, not that anyone should open a socket.
    ``StateManager`` concludes ``IDLE → THINKING`` from it and ``ConversationService`` concludes a
    session is wanted; neither is instructed. Queue policy is DROP_NEWEST — an older proposal that
    has been sitting in a queue is one whose policy context has gone stale.

    ``fact_id`` is ``None`` for a trigger with no fact behind it (a presence greeting, §3.7.5).
    """

    name: ClassVar[str] = "behavior.trigger_fired"

    trigger_id: int
    fact_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorProactiveDelivered(Event):
    """The robot spoke unprompted, and this is what it said (SDS §9.1.3, §10.6).

    Published alongside the ``proactive_log`` row rather than instead of it. The row is the durable
    audit §10.6 tunes the policy from and is the one that must not be lost (§4: the bus carries
    notifications, not obligations); this is the same fact told on the bus for anything watching
    live.
    """

    name: ClassVar[str] = "behavior.proactive_delivered"

    trigger_id: int
    utterance: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorProactiveSuppressed(Event):
    """A proposal was considered and vetoed (SDS §9.1.3, §10.4, §10.6).

    ⚠️ ``rule`` here is the same value the ``proactive_log`` column calls ``reason``. Both
    spellings are normative and both shipped before either had a writer; :data:`POLICY_RULES` is
    the single vocabulary they are fed from, so the histogram §10.6 groups by cannot split into two
    buckets over a name.

    ``would_have_said`` is ``None`` in the ordinary case, and that is not an omission: the gate
    runs *before* a session exists, so on almost every suppression there is no utterance yet —
    §10.8 composes the words only once the model is on the line.
    """

    name: ClassVar[str] = "behavior.proactive_suppressed"

    trigger_id: int
    rule: str
    would_have_said: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorTriggerDisabled(Event):
    """A trigger switched itself off after being ignored too often (SDS §9.1.3, §10.5).

    §10.5 requires this be *"logged loudly, never silent"*, for a diagnostic reason rather than an
    operational one: *"a trigger that turned itself off is diagnostic information about the design,
    and if you don't surface it you'll never learn which of your ideas were bad."*

    ⚠️ This event was **missing from §3.5.3's taxonomy** until M10 — invented in §10.5 when the
    backoff needed it, catalogued in §9.1.3, never added to the domain list. §9.1.5's drift check
    exists for exactly that gap.
    """

    name: ClassVar[str] = "behavior.trigger_disabled"

    trigger_id: int
    ignore_streak: int


# -- Rule 4's accumulator (SDS 10.4) ----------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SpeechEntry:
    """One utterance the VAD heard, and whether it turned out to be aimed at the robot."""

    correlation_id: UUID
    at_s: float  # monotonic seconds, from the owning service's clock
    duration_s: float
    attributed: bool = False  # became a conversation turn -> not ambient


@dataclass(frozen=True, slots=True, kw_only=True)
class AmbientWindow:
    """Speech heard recently, for §10.4 rule 4. Frozen; every operation returns a new one.

    Rule 4 as written is *"> 60 s of VAD speech in the last 5 min **that opened no session**"* —
    and that is not a state this architecture can observe. Per §6.3 **every** Silero detection opens
    a session; that is what the gate is for. The rule was written against a system that does not
    exist.

    What *is* observable is the same idea one step later: **speech that opened a session and never
    became a conversation.** So this counts everything and subtracts what turned out to be a
    conversation — :func:`record_speech` on ``audio.speech_ended``, :func:`attribute_speech` on
    ``conversation.user_transcribed``, matched by ``correlation_id``, which spans both edges.

    **Retraction rather than a grace timer.** The obvious alternative holds each utterance pending
    for a few seconds and promotes it if no transcript arrives. That needs a timer, a config knob
    and a defensible value for it — and §3.10.3's own trace evidence has transcripts landing after
    the assistant's audio and sometimes after ``conversation.turn_ended``, so the grace would have
    to be seconds long and would be a guess. This needs none of it, and is a pure fold.

    It is transiently wrong for a second or two after a genuine user utterance, before the
    transcript retracts it. That window is unreachable: rule 2 has already vetoed, because the
    machine is not IDLE while a turn is in flight.
    """

    entries: tuple[SpeechEntry, ...] = ()


def record_speech(
    window: AmbientWindow,
    *,
    correlation_id: UUID,
    at_s: float,
    duration_s: float,
    keep_s: float,
) -> AmbientWindow:
    """Fold one finished utterance in, dropping anything older than ``keep_s``.

    Pruning on write keeps the tuple bounded with no second pass and no timer — an always-on VAD in
    a busy room is the case that would otherwise grow this forever.
    """
    kept = tuple(entry for entry in window.entries if at_s - entry.at_s <= keep_s)
    return AmbientWindow(
        entries=(
            *kept,
            SpeechEntry(
                correlation_id=correlation_id, at_s=at_s, duration_s=duration_s
            ),
        )
    )


def attribute_speech(window: AmbientWindow, correlation_id: UUID) -> AmbientWindow:
    """Mark every entry for ``correlation_id`` as directed at the robot — the retraction.

    An unknown id is a no-op: a transcript can arrive for an utterance already pruned out of the
    window, and that is not an error.
    """
    return AmbientWindow(
        entries=tuple(
            replace(entry, attributed=True)
            if entry.correlation_id == correlation_id
            else entry
            for entry in window.entries
        )
    )


def ambient_speech_s(window: AmbientWindow, *, now_s: float, window_s: float) -> float:
    """Seconds of **un-attributed** speech inside the last ``window_s`` — rule 4's input.

    Excluding attributed speech is what makes §10.6's histogram diagnostic. Counting all speech
    would collapse *"the user was on a call"* and *"the user was talking to me"* into one
    ``ambient_speech`` bucket and destroy the only signal that tells them apart.
    """
    return sum(
        entry.duration_s
        for entry in window.entries
        if not entry.attributed and now_s - entry.at_s <= window_s
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
    "AmbientWindow",
    "BehaviorProactiveDelivered",
    "BehaviorProactiveSuppressed",
    "BehaviorTriggerDisabled",
    "BehaviorTriggerFired",
    "SpeechEntry",
    "ambient_speech_s",
    "attribute_speech",
    "record_speech",
    "Delivered",
    "PolicyContext",
    "PolicyLimits",
    "PolicyResult",
    "Suppressed",
    "TriggerRecord",
    "evaluate_policy",
    "within_quiet_window",
]
