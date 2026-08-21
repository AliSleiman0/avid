"""``ProcResources`` — the memory providers behind ``GET /metrics`` (#404).

⚠️ **These tests carry more weight than usual.** ``avid/adapters/*`` is omitted from the coverage
gate (``pyproject.toml`` — adapters are proven by contract tests, not line counts), and this
adapter has **no port**, so there is no contract suite either. Nothing else is watching this file.

The cases are written from both ends, the way ``tests/test_p8_gate.py`` argues for: every branch
that returns a number has a test, and every branch that returns *nothing* has one too. A class that
quietly returned ``None`` everywhere would satisfy the first set alone, and would produce a
thirty-day soak whose memory column was empty for a reason nobody noticed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from avid.adapters.resources import ProcResources
from avid.core.metrics import MetricsRegistry

# A real /proc/self/statm line: size resident shared text lib data dt, in pages.
_STATM_LINE = "68000 12345 4096 1 0 30000 0\n"

# /proc/meminfo, trimmed. MemAvailable deliberately NOT first and NOT adjacent to MemFree, so a
# parser that grabbed the wrong line or assumed an offset fails here.
_MEMINFO = """\
MemTotal:        1898752 kB
MemFree:           99164 kB
Buffers:           41208 kB
Cached:           363404 kB
MemAvailable:    1543210 kB
SwapTotal:             0 kB
"""


@pytest.fixture
def procfs(tmp_path: Path) -> Path:
    """A fake procfs whose files hold known values."""
    (tmp_path / "statm").write_text(_STATM_LINE)
    (tmp_path / "meminfo").write_text(_MEMINFO)
    return tmp_path


def _probe(root: Path) -> ProcResources:
    return ProcResources(statm=root / "statm", meminfo=root / "meminfo")


# ── the readings ─────────────────────────────────────────────────────────────────────────────


def test_rss_is_the_resident_field_in_bytes(procfs: Path) -> None:
    """Field 1 of statm, in pages, scaled to bytes.

    The *resident* field, not the first one. ``size`` (68000 pages) is virtual address space and is
    five times larger here on purpose — reading field 0 would report a robot using ~265 MiB when it
    uses ~48, and both are plausible enough that nobody would catch it by eye.
    """
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    assert _probe(procfs).rss_bytes() == 12345 * page


def test_mem_available_is_the_kernels_estimate_not_memfree(procfs: Path) -> None:
    """``MemAvailable``, in bytes — and specifically not ``MemFree``.

    On a healthy Linux box MemFree trends toward zero because the page cache takes everything
    going spare, so a soak graded on it would report a catastrophe every night it ran. The fixture
    puts MemFree (99164) well below MemAvailable (1543210) so the two cannot be confused.
    """
    assert _probe(procfs).mem_available_bytes() == 1543210 * 1024


# ── absent is not zero ───────────────────────────────────────────────────────────────────────


def test_both_are_none_when_there_is_no_procfs(tmp_path: Path) -> None:
    """A Windows dev box, a macOS laptop, a CI runner: no procfs, no reading, no exception.

    ``None`` rather than ``0`` is the whole point — see the registry test below for what that
    buys.
    """
    probe = _probe(tmp_path / "nothing-here")
    assert probe.rss_bytes() is None
    assert probe.mem_available_bytes() is None


def test_absent_providers_land_in_absent_and_not_in_metrics(tmp_path: Path) -> None:
    """The end-to-end statement AC-2 actually cares about, at the registry boundary.

    This is the guard against the M6 defect family: a `0` from an instrument that was never wired
    reads exactly like a real `0` from one that was. Asserting the names are missing from
    ``metrics`` matters as much as asserting they are present in ``absent`` — a provider that
    returned 0 *and* got listed as absent would pass a weaker version of this test.
    """
    probe = _probe(tmp_path / "nothing-here")
    registry = MetricsRegistry()
    registry.register("rss_bytes", probe.rss_bytes)
    registry.register("mem_available_bytes", probe.mem_available_bytes)

    snapshot = registry.snapshot()

    assert snapshot["absent"] == ["mem_available_bytes", "rss_bytes"]
    assert "rss_bytes" not in snapshot["metrics"]
    assert "mem_available_bytes" not in snapshot["metrics"]


# ── broken is not absent, and neither is zero ────────────────────────────────────────────────


def test_an_unparseable_statm_is_reported_broken_rather_than_zero(procfs: Path) -> None:
    """A statm that exists but does not parse is a broken instrument.

    It raises, so :meth:`MetricsRegistry.snapshot` logs a traceback and names it in ``absent``.
    The alternative — swallowing it and returning 0 — would claim the robot uses no memory, which
    is both false and indistinguishable from a real reading.
    """
    (procfs / "statm").write_text("this is not a statm line\n")
    probe = _probe(procfs)
    with pytest.raises(ValueError):
        probe.rss_bytes()

    registry = MetricsRegistry()
    registry.register("rss_bytes", probe.rss_bytes)
    snapshot = registry.snapshot()
    assert snapshot["absent"] == ["rss_bytes"]
    assert "rss_bytes" not in snapshot["metrics"]


def test_meminfo_without_the_key_raises_rather_than_returning_zero(
    procfs: Path,
) -> None:
    """A meminfo with no ``MemAvailable`` line — a kernel older than 3.14, or a truncated read.

    The loop falls through, and falling through must not mean 0. This is the branch a
    ``for … else: return 0`` would have got wrong.
    """
    (procfs / "meminfo").write_text("MemTotal: 1898752 kB\nMemFree: 99164 kB\n")
    with pytest.raises(ValueError, match="MemAvailable"):
        _probe(procfs).mem_available_bytes()


# ── the real thing, where there is one ───────────────────────────────────────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real procfs")
def test_real_procfs_reports_something_plausible() -> None:
    """On Linux — CI and the Pi — the default paths give a real, sane reading.

    A monkeypatched fixture proves the parsing; only this proves the *paths*. A typo in
    ``/proc/self/statm`` would pass every test above and return ``None`` forever in production,
    silently, which is precisely the failure this issue exists to prevent.
    """
    probe = ProcResources()
    rss = probe.rss_bytes()
    available = probe.mem_available_bytes()
    assert rss is not None and available is not None
    # A CPython process with pytest loaded is comfortably over 8 MiB and under 8 GiB. Wide on
    # purpose: this asserts "a real reading", not a budget, and a tight bound here would be a
    # flaky test wearing a measurement's name.
    assert 8 * 1024 * 1024 < rss < 8 * 1024 * 1024 * 1024
    assert available > 0


# ── P8: these are file reads on a loop that says it takes in-memory ones ─────────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real procfs")
async def test_snapshot_stays_cheap_enough_for_the_inline_path() -> None:
    """⚠️ ``MetricsSource``'s docstring says providers are *in-memory* reads. These are files.

    That relaxation is deliberate and recorded (see the module docstring), but *"procfs is cheap"*
    is an assumption, and this project's §11.3 rule is that an assumption about cost gets measured.
    ``snapshot()`` runs inline on the event loop while the robot may be mid-turn.

    Two mechanisms, not one. This asserts a **loose** ceiling — 5 ms per snapshot, a factor of ten
    under P8's 50 ms bar — chosen wide enough that runner jitter cannot flake it and tight enough
    that a provider which started touching a disk would fail. The real detector is the P8 gate
    itself: this coroutine runs on the loop, so under ``PYTHONASYNCIODEBUG=1`` a genuinely slow
    provider is convicted there with a traceback, which is a better report than a timing number.
    """
    import time

    registry = MetricsRegistry()
    probe = ProcResources()
    registry.register("rss_bytes", probe.rss_bytes)
    registry.register("mem_available_bytes", probe.mem_available_bytes)
    assert "rss_bytes" in registry.snapshot()["metrics"]  # warm, and prove it reads

    rounds = 200
    started = time.perf_counter()
    for _ in range(rounds):
        registry.snapshot()
    per_call_ms = (time.perf_counter() - started) / rounds * 1000

    # Named for what it is: a mean over 200 calls, not a percentile over five.
    assert per_call_ms < 5.0, f"mean {per_call_ms:.3f} ms/snapshot over n={rounds}"
