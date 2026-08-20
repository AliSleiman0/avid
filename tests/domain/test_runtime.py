"""Tier-1 tests for the boot record and O5's uptime arithmetic (#379, SDS §12.6).

Pure, milliseconds (§14.2). Uptime is a number a milestone turns on, so the cases that earn their
keep are the ones where a plausible implementation would be **wrong in the optimistic direction** —
crediting an unclean run with downtime it cannot prove, or dropping the run that was already going
when the window opened.
"""

from __future__ import annotations

from avid.domain import BootRecord, uptime_ratio

_DAY = 86_400


def _rec(start: int, *, seen: int, stopped: int | None = None) -> BootRecord:
    return BootRecord(
        boot_id=f"b{start}",
        build="1.0.0",
        started_at=start,
        started_mono=0,
        last_seen_at=seen,
        stopped_at=stopped,
        stop_reason="signal" if stopped is not None else None,
    )


def test_a_clean_run_ends_at_its_stop() -> None:
    assert _rec(0, seen=50, stopped=100).ended_at == 100
    assert _rec(0, seen=50, stopped=100).was_clean is True


def test_an_unclean_run_ends_at_its_last_heartbeat_not_at_the_next_boot() -> None:
    """The load-bearing conservatism. A crashed run knows it was alive at its last heartbeat and
    nothing after; crediting it further would count downtime as uptime, which is the one direction
    an availability figure must never be wrong in."""
    record = _rec(0, seen=50)
    assert record.ended_at == 50
    assert record.was_clean is False


def test_a_full_window_of_uptime_is_one() -> None:
    assert (
        uptime_ratio([_rec(0, seen=100, stopped=100)], window_start=0, window_end=100)
        == 1.0
    )


def test_a_gap_between_runs_is_downtime() -> None:
    # up 0-40, down 40-60, up 60-100 → 80% of the window.
    records = [_rec(0, seen=40, stopped=40), _rec(60, seen=100, stopped=100)]
    assert uptime_ratio(records, window_start=0, window_end=100) == 0.8


def test_a_run_straddling_the_window_is_clipped_not_dropped() -> None:
    """The run already going when a soak window opens carries the window's first seconds.

    Dropping it (containment instead of overlap) would understate availability at both ends of
    every window — and a soak has exactly two ends, so the error is not marginal.
    """
    records = [_rec(-1000, seen=5_000, stopped=5_000)]
    assert uptime_ratio(records, window_start=0, window_end=1_000) == 1.0


def test_an_empty_history_is_zero_not_one() -> None:
    """No records means nothing is known to have been up. An availability metric that reads 100%
    because its source was empty is the M4 failure wearing a percentage sign."""
    assert uptime_ratio([], window_start=0, window_end=100) == 0.0


def test_a_non_positive_window_returns_a_floor_rather_than_raising() -> None:
    """A gate that crashes while reporting is worse than one that reports a floor (§7.1)."""
    assert (
        uptime_ratio([_rec(0, seen=10, stopped=10)], window_start=50, window_end=50)
        == 0.0
    )
    assert (
        uptime_ratio([_rec(0, seen=10, stopped=10)], window_start=50, window_end=10)
        == 0.0
    )


def test_thirty_days_with_one_hour_down_clears_the_o5_bar() -> None:
    """O5 in its own units: 99% of 30 days permits 7 h 12 m of downtime."""
    window = 30 * _DAY
    records = [
        _rec(0, seen=10 * _DAY, stopped=10 * _DAY),
        _rec(10 * _DAY + 3_600, seen=window, stopped=window),
    ]
    ratio = uptime_ratio(records, window_start=0, window_end=window)
    assert ratio > 0.99
    # ...and the same window with eight hours down does not, which is the half that matters.
    worse = [
        _rec(0, seen=10 * _DAY, stopped=10 * _DAY),
        _rec(10 * _DAY + 8 * 3_600, seen=window, stopped=window),
    ]
    assert uptime_ratio(worse, window_start=0, window_end=window) < 0.99


def test_the_record_is_frozen() -> None:
    import dataclasses

    import pytest

    record = _rec(0, seen=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.started_at = 5  # type: ignore[misc]
