"""The spend ceiling — the one limit that does not care *why* a turn started (#472).

Everything else that can refuse a turn asks a question about the room. §10.4's policy gate asks
whether it is quiet hours, whether anyone is there, whether the robot just spoke. §6.2.4's
admission gate asks whether the frame was louder than the robot's own echo. Both are good
questions and both are *proxies*: they refuse turns that look wrong, in the hope that the ones
left are cheap.

This asks the only question that is not a proxy — **how much has actually been spent** — and it
exists because on 2026-08-24 every proxy in the system was satisfied while the robot spent $0.50
in eight minutes with nobody in the room (#467). The acoustic gate now refuses that particular
defect; a different defect producing genuine-looking origins would walk straight past it. A
ceiling on money is the backstop that does not depend on guessing the failure in advance.

⚠️ **Measured, never modelled.** ``CostMeterService.projected_monthly_usd`` extrapolates
``cost_per_turn x 20 turns/day x 30 days`` (§6.10.3) and **ignores the observed rate entirely** —
during the runaway it read ~$11/month, comfortably inside O7, while real spend ran at roughly
$3.75/hour. A tripwire on that figure would not have fired. The quantity here is dollars that were
actually billed inside a rolling window, and nothing else.

Pure by construction (P1): no I/O, no clock, no globals. The window and the wall-clock live in the
adapter behind :class:`~avid.core.ports.SpendSource`; this compares two numbers.
"""

from __future__ import annotations

#: The reason string a refused turn carries, pinned here the way ``POLICY_RULES`` and
#: ``ADMISSION_RULES`` are pinned in their own modules.
#:
#: It travels to the ``spend_refusals`` counter on ``/metrics`` (§3.12.2) and into the log line
#: that names it, so a rename in one place and not the other splits the only histogram that can
#: say how often the ceiling actually bit.
SPEND_CEILING = "spend_ceiling"


def over_ceiling(spent_usd: float, *, ceiling_usd: float) -> bool:
    """Has *spent_usd* reached *ceiling_usd*? Then no further turn may be started.

    ``>=`` rather than ``>``: the ceiling is a limit on what may be spent, so reaching it is
    already the end of the allowance. The off-by-one direction matters more than it looks — a
    ``>`` here lets every runaway spend one extra turn past the bar, and the whole point of this
    guard is that the turn it refuses is one nobody asked for.

    A ceiling of ``0.0`` therefore refuses everything, which is a legitimate configuration: it is
    "this robot may not open a paid session at all", reachable by config with no code change. That
    is the same escape hatch ``barge_in_margin_db`` offers for barge-in (SDS §6.3), and it exists
    for the same reason — a knob that can be turned all the way is one that can be used in an
    emergency at 3 a.m. by someone who is not going to edit Python.
    """
    return spent_usd >= ceiling_usd
