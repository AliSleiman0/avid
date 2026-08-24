"""The spend ceiling's arithmetic — the one guard that is not a proxy (#472)."""

from __future__ import annotations

from avid.domain.cost import SPEND_CEILING, over_ceiling


def test_spend_at_the_ceiling_is_already_over_it() -> None:
    """⚠️ ``>=``, not ``>``, and this test exists because nothing proved it.

    The boundary was written down in the docstring and asserted nowhere: swapping the operator
    left the whole suite green. A ``>`` lets every runaway spend one more turn past the bar, and
    the turn it would spend is by definition the one nobody asked for — so the off-by-one runs in
    exactly the wrong direction.
    """
    assert over_ceiling(1.00, ceiling_usd=1.00) is True
    assert over_ceiling(1.0001, ceiling_usd=1.00) is True
    assert over_ceiling(0.9999, ceiling_usd=1.00) is False


def test_a_zero_ceiling_refuses_everything_including_a_free_start() -> None:
    """The config-only emergency stop: "this robot may not open a paid session at all".

    Reachable without a code change, the same escape hatch a very large ``barge_in_margin_db``
    gives for barge-in (SDS §6.3), and for the same reason — a knob that can be turned all the way
    is one that can be used at 3 a.m. by someone who is not going to edit Python.
    """
    assert over_ceiling(0.0, ceiling_usd=0.0) is True


def test_an_unspent_window_is_never_over_a_real_ceiling() -> None:
    """A quiet robot is not a refused one. Zero spend must never trip a non-zero bar."""
    assert over_ceiling(0.0, ceiling_usd=1.00) is False


def test_the_reason_string_is_pinned() -> None:
    """It travels to `/metrics` and into the log line that names it — one vocabulary, both sides.

    The same discipline `POLICY_RULES` and `ADMISSION_RULES` get, for the same reason: a rename in
    one place and not the other splits the only histogram that can say how often the ceiling bit.
    """
    assert SPEND_CEILING == "spend_ceiling"
