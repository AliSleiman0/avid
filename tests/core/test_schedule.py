"""RFC 5545 resolution and the DST boundary (#236, SDS §10.3).

§10.3 names one failure mode by hand: *"storing '08:00 = epoch X, +86400 each day' breaks on the DST
boundary and delivers your coffee reminder at 07:00 for six months."* Six months is the point — the
bug is silent, self-correcting at the next transition, and by then nobody remembers what changed. So
these tests do not check that a library was called; they check the **wall clock on both sides of a
real transition in a real zone**, which is the only assertion that can tell the two implementations
apart.

Everything here runs in microseconds with no clock, because the resolver takes ``after`` as an
argument. "Waiting until 07:55 to test the 07:55 code path is not a testing strategy" (§10.3).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from avid.core.schedule import InvalidRoutine, Routine, next_occurrence

# 'America/New_York' rather than the shipped 'Asia/Beirut': its transitions are the ones every
# reader can check by hand, and the zone is stable in the IANA database. Beirut's 2023 transition
# was changed by government decree five days before it happened, which makes it a poor fixture and
# an excellent argument for never hard-coding an offset.
_NY = "America/New_York"
# DTSTART is the day the user told us about the routine — `facts.created_at`, in practice.
_TOLD_US = int(datetime(2026, 1, 5, 12, 0, tzinfo=ZoneInfo(_NY)).timestamp())
_COFFEE = Routine(
    rrule="FREQ=DAILY", local_time="08:00", timezone=_NY, dtstart_epoch=_TOLD_US
)


def epoch(wall: str, *, zone: str = _NY) -> int:
    """``"2026-03-07 06:00"`` in ``zone`` → UTC epoch seconds."""
    return int(
        datetime.strptime(wall, "%Y-%m-%d %H:%M")
        .replace(tzinfo=ZoneInfo(zone))
        .timestamp()
    )


def local(epoch_s: int, *, zone: str = _NY) -> str:
    """UTC epoch seconds → ``"YYYY-MM-DD HH:MM"`` on ``zone``'s wall clock. What a human would see."""
    return datetime.fromtimestamp(epoch_s, tz=ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


# ── The shipped rules ────────────────────────────────────────────────────────────────────────


def test_daily_fires_at_the_lead_time_before_the_occurrence() -> None:
    """UC-03, exactly: an 08:00 routine with the schema's 300 s lead fires at **07:55**."""
    fire = next_occurrence(_COFFEE, after=epoch("2026-06-10 22:00"))
    assert fire is not None
    assert local(fire) == "2026-06-11 07:55"


def test_the_next_occurrence_is_strictly_after_the_cursor() -> None:
    """Asked at exactly its own fire time, a routine returns *tomorrow*. Otherwise a trigger that
    just fired would immediately be due again and the scheduler would spin on it."""
    first = next_occurrence(_COFFEE, after=epoch("2026-06-10 22:00"))
    assert first is not None
    again = next_occurrence(_COFFEE, after=first)
    assert again is not None
    assert local(again) == "2026-06-12 07:55"


def test_weekdays_only_skips_the_weekend() -> None:
    """``FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR`` — §10.3's second named example. Friday's next is
    Monday, which is the assertion a hand-rolled 'every N days' cannot make."""
    weekday = Routine(
        rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        local_time="08:00",
        timezone=_NY,
        dtstart_epoch=_TOLD_US,
    )
    # 2026-06-12 is a Friday.
    fire = next_occurrence(weekday, after=epoch("2026-06-12 09:00"))
    assert fire is not None
    assert local(fire) == "2026-06-15 07:55"  # Monday


def test_monthly_bysetpos_resolves() -> None:
    """``FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO`` — first Monday of the month. §10.3's third example, and
    the one it says every invented DSL gets wrong."""
    monthly = Routine(
        rrule="FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO",
        local_time="09:00",
        timezone=_NY,
        dtstart_epoch=_TOLD_US,
        lead_time_s=0,
    )
    fire = next_occurrence(monthly, after=epoch("2026-06-15 12:00"))
    assert fire is not None
    assert local(fire) == "2026-07-06 09:00"  # first Monday of July 2026


def test_a_finite_rule_that_has_run_out_returns_none() -> None:
    """``COUNT=``/``UNTIL=`` exhausted is not an error. The caller leaves ``next_fire_at`` NULL and
    ``idx_triggers_due``'s partial predicate drops the row from the scheduler's only query."""
    finite = Routine(
        rrule="FREQ=DAILY;COUNT=2",
        local_time="08:00",
        timezone=_NY,
        dtstart_epoch=_TOLD_US,
        lead_time_s=0,
    )
    assert next_occurrence(finite, after=epoch("2026-06-20 12:00")) is None


# ── DST: the reason this module exists ───────────────────────────────────────────────────────


def test_spring_forward_keeps_the_wall_clock_and_moves_the_epoch() -> None:
    """The headline. On 2026-03-08 New York jumps 02:00 → 03:00, so the day is 23 hours long.

    A correct resolver keeps **08:00 local** on both sides and the UTC epoch between the two
    occurrences differs by 23 hours. The '+86400 each day' implementation §10.3 warns about keeps
    the epoch gap at 24 hours and silently serves the reminder at 07:00 local — for six months,
    until the autumn transition puts it back and nobody ever learns why.
    """
    saturday = next_occurrence(_COFFEE, after=epoch("2026-03-06 12:00"))
    assert saturday is not None
    assert local(saturday) == "2026-03-07 07:55"

    sunday = next_occurrence(_COFFEE, after=saturday)  # the transition is this night
    assert sunday is not None
    assert local(sunday) == "2026-03-08 07:55", "the wall clock must not move"
    assert sunday - saturday == 23 * 3600, (
        "...and the epoch gap must, by exactly one hour"
    )


def test_fall_back_keeps_the_wall_clock_and_lengthens_the_day() -> None:
    """The mirror: 2026-11-01 has 25 hours. Same wall clock, epoch gap one hour longer."""
    saturday = next_occurrence(_COFFEE, after=epoch("2026-10-30 12:00"))
    assert saturday is not None
    assert local(saturday) == "2026-10-31 07:55"

    sunday = next_occurrence(_COFFEE, after=saturday)
    assert sunday is not None
    assert local(sunday) == "2026-11-01 07:55"
    assert sunday - saturday == 25 * 3600


def test_a_nonexistent_local_time_is_late_never_skipped() -> None:
    """02:30 does not occur on 2026-03-08 — the clocks jump straight from 02:00 to 03:00.

    Policy: take the pre-transition offset, which is the instant the old timeline would have
    reached. The reminder lands at 03:30 local rather than evaporating. A once-a-year silent
    disappearance is the worse failure: nothing logs, nothing errors, and the user simply notices
    one morning that the robot has stopped mentioning something.
    """
    early = Routine(
        rrule="FREQ=DAILY",
        local_time="02:30",
        timezone=_NY,
        dtstart_epoch=_TOLD_US,
        lead_time_s=0,
    )
    fire = next_occurrence(early, after=epoch("2026-03-07 12:00"))
    assert fire is not None
    assert local(fire) == "2026-03-08 03:30"


def test_an_ambiguous_local_time_fires_once_on_the_earlier_pass() -> None:
    """01:30 happens twice on 2026-11-01. Policy is ``fold=0`` — the first of the two.

    Pinned rather than trusted: the default is what delivers this, so a future change that starts
    passing ``fold=1`` "for correctness" moves a reminder by an hour with nothing else to notice.
    The assertion is on the UTC instant, because both passes have the same wall clock and only the
    epoch can tell them apart.
    """
    ambiguous = Routine(
        rrule="FREQ=DAILY",
        local_time="01:30",
        timezone=_NY,
        dtstart_epoch=_TOLD_US,
        lead_time_s=0,
    )
    fire = next_occurrence(ambiguous, after=epoch("2026-10-31 12:00"))
    assert fire is not None
    assert local(fire) == "2026-11-01 01:30"
    # EDT (-04:00) is the first pass; EST (-05:00) would be 05:30 UTC — an hour later.
    assert datetime.fromtimestamp(fire, tz=ZoneInfo("UTC")).strftime("%H:%M") == "05:30"


def test_the_zone_is_the_routines_own_not_the_hosts() -> None:
    """``routines.timezone`` is per-row (§8.3) — the resolver never reads a process default, so a
    Pi running on UTC and a laptop running on Europe/London resolve the same routine identically."""
    beirut = Routine(
        rrule="FREQ=DAILY",
        local_time="08:00",
        timezone="Asia/Beirut",
        dtstart_epoch=_TOLD_US,
    )
    fire = next_occurrence(beirut, after=epoch("2026-06-10 22:00", zone="Asia/Beirut"))
    assert fire is not None
    assert local(fire, zone="Asia/Beirut") == "2026-06-11 07:55"


# ── Registration-time failures ───────────────────────────────────────────────────────────────


def test_a_malformed_rrule_fails_at_registration() -> None:
    """Not at 07:55, six months later, inside a scheduler loop."""
    with pytest.raises(InvalidRoutine, match="not a usable RFC 5545 RRULE"):
        next_occurrence(
            Routine(
                rrule="EVERY DAY PLEASE",
                local_time="08:00",
                timezone=_NY,
                dtstart_epoch=_TOLD_US,
            ),
            after=0,
        )


def test_an_unknown_timezone_fails_with_the_name_in_it() -> None:
    with pytest.raises(InvalidRoutine, match="Mars/Olympus_Mons"):
        next_occurrence(
            Routine(
                rrule="FREQ=DAILY",
                local_time="08:00",
                timezone="Mars/Olympus_Mons",
                dtstart_epoch=_TOLD_US,
            ),
            after=0,
        )


@pytest.mark.parametrize("bad", ["8:00", "08:00:00", "24:00", "08:60", "morning", ""])
def test_a_malformed_local_time_fails_at_registration(bad: str) -> None:
    """``local_time`` is the one field here a language model authored — it arrives from
    ``remember_fact``'s ``schedule`` argument (§6.6). Strictness turns "the model emitted a
    slightly-off format" into a logged failure rather than a reminder that never fires."""
    with pytest.raises(InvalidRoutine):
        next_occurrence(
            Routine(
                rrule="FREQ=DAILY",
                local_time=bad,
                timezone=_NY,
                dtstart_epoch=_TOLD_US,
            ),
            after=0,
        )


def test_the_iana_database_is_actually_available() -> None:
    """A canary, not a tautology. Without the ``tzdata`` wheel every IANA lookup on a Windows dev
    box raises ``ZoneInfoNotFoundError`` — and because ``_zone`` converts that into
    ``InvalidRoutine``, a missing tz database would otherwise present as "your routine is invalid",
    sending the next reader after the wrong bug entirely."""
    assert (
        ZoneInfo(_NY).utcoffset(datetime(2026, 1, 1, tzinfo=ZoneInfo(_NY))) is not None
    )


def test_a_moving_anchor_would_change_what_the_rule_means() -> None:
    """Why ``dtstart_epoch`` is required rather than defaulted to "now" — the defect this file
    caught before the scheduler was built on top of it.

    A bare ``FREQ=WEEKLY`` takes its weekday from DTSTART. Anchored at the moment the scheduler
    happens to ask, "weekly" would mean "a week from whenever I last looked", and the rule would
    still resolve, still return a plausible time, and silently not be the schedule the user
    described. Anchored properly it means *the same weekday, every week*.
    """
    # 2026-01-05 is a Monday — the day the user told us.
    weekly = Routine(
        rrule="FREQ=WEEKLY", local_time="08:00", timezone=_NY, dtstart_epoch=_TOLD_US
    )
    # Ask on a Wednesday. The answer must still be a Monday.
    fire = next_occurrence(weekly, after=epoch("2026-06-17 12:00"))
    assert fire is not None
    assert local(fire) == "2026-06-22 07:55"
    assert datetime.fromtimestamp(fire, tz=ZoneInfo(_NY)).weekday() == 0
