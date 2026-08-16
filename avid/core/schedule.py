"""RFC 5545 recurrence → the next fire time, in UTC (SDS §10.3).

The scheduler's arithmetic, and the only module in the tree permitted to import ``dateutil``.

§10.3 mandates the library by name and says why: *"Do not invent a schedule DSL — recurrence rules
are a solved problem with a standard and a library, and every DSL anyone has ever invented for this
has been re-derived, badly, up to about ``BYSETPOS``."* It also names the failure this module exists
to avoid — *"storing '08:00 = epoch X, +86400 each day' breaks on the DST boundary and delivers your
coffee reminder at 07:00 for six months."*

**The fix is where the arithmetic happens, not which library does it.** ``dateutil.rrule`` is run
over **naive local** datetimes; the zone is attached afterwards and the result converted to UTC:

    after (UTC epoch) → local wall clock in routines.timezone → drop tzinfo
                      → rrule occurrence (naive local, so 08:00 stays 08:00 across a transition)
                      → attach ZoneInfo → UTC epoch → minus lead_time_s

Let rrule work in UTC instead and you get exactly the "+86400 each day" bug, wearing a library's
clothes: the occurrences stay 24 hours apart and the wall clock drifts by an hour twice a year.

This module is pure — a total function of its arguments, with no clock — which is why it lives in
``core/`` rather than behind a port. A ``RecurrenceResolver`` Protocol would oblige P6 to supply a
*fake* resolver, and a fake recurrence engine is a second, worse RRULE implementation grading itself
against the first: the DSL §10.3 forbids, arrived at sideways.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

__all__ = ["InvalidRoutine", "Routine", "next_occurrence"]


class InvalidRoutine(ValueError):
    """A routine that cannot be resolved: bad RRULE, bad ``HH:MM``, or unknown IANA zone.

    Raised at *registration*, never at fire time. A trigger that cannot be scheduled is a fact the
    user gave us that we silently failed to act on, and discovering that at 07:55 — inside a
    scheduler loop, six months later — is the shape of bug §10.6 exists to make visible.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Routine:
    """A ``routines`` row (SDS §8.3), as a value.

    ``lead_time_s`` is what makes UC-03 fire at **07:55** for an 08:00 coffee: the schedule names
    the event, the lead names how far ahead the robot mentions it. It defaults to the schema's 300.
    """

    rrule: str  # RFC 5545, e.g. 'FREQ=DAILY' or 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR'
    local_time: str  # 'HH:MM' wall clock, in `timezone`
    timezone: str  # IANA, e.g. 'Asia/Beirut'
    # RFC 5545's DTSTART, as a UTC epoch second. Carries no column in §8.3's `routines` table
    # because it does not need one: it is the `facts.created_at` of the fact this routine belongs
    # to, which is the day the user told us about the routine.
    #
    # ⚠️ Required, and not defaulted to "now", because an anchor that moves changes what the rule
    # MEANS. `COUNT=2` re-counts from today on every call and never exhausts. A bare `FREQ=WEEKLY`
    # takes its weekday from DTSTART, so it would fire on whichever day the scheduler happened to
    # ask. Both are silent: the rule still resolves, still returns a plausible time, and is simply
    # not the schedule the user described.
    dtstart_epoch: int
    lead_time_s: int = 300  # fire this many seconds before the occurrence


def next_occurrence(routine: Routine, *, after: int) -> int | None:
    """The next time this routine should **fire**, as a UTC epoch second strictly after ``after``.

    ``after`` is a UTC epoch second; so is the result. Returns ``None`` when the rule has no
    further occurrences — a finite ``COUNT=``/``UNTIL=`` that
    has run out. The caller drops it from the heap and leaves ``triggers.next_fire_at`` NULL, and
    ``idx_triggers_due``'s partial predicate then excludes it from the scheduler's only query for
    free (§8.3).

    Two DST edges have a policy rather than an accident, because both land at two in the morning
    where nobody is watching:

    **Nonexistent local times** (spring forward — 02:30 does not occur on the day the clocks jump
    02:00 → 03:00). The occurrence is taken on the *pre-transition* offset, which is the instant the
    old timeline would have reached: an hour later on the wall clock, but it happens. **Late, never
    skipped** — a reminder that silently evaporates once a year is worse than one that arrives at
    03:30.

    **Ambiguous local times** (fall back — 01:30 happens twice). The **first** is taken. It fires
    once, on the earlier pass, rather than twice or on the later one.

    Both fall out of ``fold=0``, which is the default. That is worth knowing rather than trusting:
    the tests pin both at real transition instants in a real zone, so a future refactor that starts
    passing ``fold=1`` "for correctness" fails loudly instead of quietly moving an hour.
    """
    zone = _zone(routine.timezone)
    hour, minute = _wall_clock(routine.local_time)

    # Search from the moment the *occurrence* would have to be for its fire time to beat `after`.
    # fire_at = occurrence - lead_time_s, so occurrence > after + lead_time_s.
    cursor_local = datetime.fromtimestamp(after + routine.lead_time_s, tz=zone).replace(
        tzinfo=None
    )
    # DTSTART: the routine's own start date at its wall-clock time — never the cursor's date, or
    # COUNT= would re-count from today and a bare FREQ=WEEKLY would take its weekday from whenever
    # the scheduler last asked. Naive throughout: this is the half of the pipeline that must not
    # know about offsets, or "08:00 daily" stops meaning 08:00 the moment the clocks move.
    anchor = datetime.fromtimestamp(routine.dtstart_epoch, tz=zone).replace(
        tzinfo=None, hour=hour, minute=minute, second=0, microsecond=0
    )

    try:
        rule = rrulestr(routine.rrule, dtstart=anchor)
    except (ValueError, TypeError) as exc:  # dateutil raises either on a malformed rule
        raise InvalidRoutine(
            f"{routine.rrule!r} is not a usable RFC 5545 RRULE (SDS §10.3): {exc}"
        ) from exc

    occurrence = rule.after(cursor_local, inc=False)
    if occurrence is None:
        return None  # a finite rule that has run out — not an error, just no next fire

    # Attach the zone *after* the recurrence math, never before. fold=0 is load-bearing; see above.
    return int(occurrence.replace(tzinfo=zone).timestamp()) - routine.lead_time_s


def _zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone, or say which one failed.

    ⚠️ On a Windows dev box with no ``tzdata`` wheel installed this raises for *every* IANA name,
    including correct ones — the system has no tz database at all. That is why ``tzdata`` is in the
    dev dependency group: without it these tests pass in CI and fail on a laptop, which is the worst
    possible way for a test to behave.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidRoutine(
            f"{name!r} is not a resolvable IANA timezone (SDS §8.3 routines.timezone): {exc}"
        ) from exc


def _wall_clock(local_time: str) -> tuple[int, int]:
    """``"08:00"`` → ``(8, 0)``. Strict, for the same reason ``[behavior] quiet_hours`` is strict.

    ``routines.local_time`` arrives from the model via ``remember_fact``'s ``schedule`` argument
    (§6.6), so it is the one field here a language model authored. Rejecting ``"8:00"`` at
    registration is what turns "the model emitted a slightly-off format" into a logged failure
    rather than a reminder that never fires.
    """
    hh, sep, mm = local_time.partition(":")
    if sep != ":" or len(hh) != 2 or len(mm) != 2 or not (hh + mm).isdigit():
        raise InvalidRoutine(
            f"{local_time!r} is not HH:MM wall clock (SDS §8.3 routines.local_time)."
        )
    hour, minute = int(hh), int(mm)
    if hour > 23 or minute > 59:
        raise InvalidRoutine(
            f"{local_time!r} is not a valid time of day (SDS §8.3 routines.local_time)."
        )
    return hour, minute
