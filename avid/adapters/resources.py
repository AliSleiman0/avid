"""Process and machine memory, read from procfs, for ``GET /metrics`` (#404, SDS §3.12.2).

M11's thirty-day soak is the only instrument this project has that can find a **slow leak** — an
unbounded deque, a growing page cache, an ONNX arena that never returns. Every other gate here runs
for minutes. On the 8 GB the spec used to claim that would be a curiosity; on the **2 GB board the
rig actually is** (AVID-401, SDS §2.7.1) it is the failure mode. Until this module the registry had
no memory of any kind, so the window would have closed with no answer (SDS §12.6.1).

**Two numbers, and the second is not redundant.**

* ``rss_bytes`` — *this process's* resident set. The robot's own footprint.
* ``mem_available_bytes`` — *the machine's* headroom. Deliberately machine-wide: a leak in a
  sibling process (the sampler, journald, an ssh session left open) ends the soak just as
  effectively, and the robot's own RSS would never show it.

⚠️ **Absent is not zero, and the distinction here has two levels.** Off procfs — a Windows dev box,
a macOS laptop, a CI runner — both providers return ``None`` for the life of the process, which
:meth:`avid.core.metrics.MetricsRegistry.snapshot` routes into ``absent``. A read that *fails on a
machine that has procfs* is left to raise, so the registry logs it and names it. "Not applicable
here" and "broken here" are different statements and a soak reader needs both, for the same reason
``absent`` exists at all: a ``0`` from an unwired instrument reads exactly like a real ``0``.

⚠️ **These are file reads, and the port's docstring says providers are in-memory reads.** That is a
real relaxation of :class:`~avid.core.ports.MetricsSource`'s stated constraint, recorded rather
than slipped in. Procfs is generated in the kernel with no disk behind it, so the cost is
microseconds — but *"it should be cheap"* is the sentence this project has been burned by twice
(SDS §11.3), so it is measured rather than asserted: ``tests/test_p8_gate.py`` drives
``snapshot()`` on the loop under ``PYTHONASYNCIODEBUG=1``, and if these ever stop being cheap the
P8 gate is what fails. No caching layer — the soak samples once per 60 s, so every read would be a
cache miss and the cache would buy nothing but code.
"""

from __future__ import annotations

import os
from pathlib import Path

# procfs, as the kernel exposes it. Constructor arguments rather than module constants so tests
# can point at fixtures — the pattern `tests/contract/_hardware.py` already uses for the
# device-tree node.
_STATM = Path("/proc/self/statm")
_MEMINFO = Path("/proc/meminfo")

# ``/proc/self/statm`` is a single line of page counts: size resident shared text lib data dt.
# Field 1 (0-indexed) is the resident set — cheaper to parse than `/proc/self/status`'s VmRSS,
# which is a 50-line key/value file we would scan for one entry.
_STATM_RESIDENT_FIELD = 1

# ``MemAvailable`` is the kernel's own estimate of what a new workload could claim without
# swapping. It is the right number here and ``MemFree`` is the wrong one: free memory on a healthy
# Linux box trends toward zero because the page cache uses everything going spare, so a soak
# graded on MemFree would report a catastrophe on every night it ran.
_MEM_AVAILABLE_KEY = "MemAvailable:"


class ProcResources:
    """Memory readings from procfs, or ``None`` everywhere procfs is not.

    Not behind a ``Protocol``, on purpose. :class:`~avid.core.ports.MetricsSource` is already the
    port ``HealthServer`` depends on, and the registry's contract *is* a zero-argument callable —
    so a ``ResourceProbe`` Protocol would have exactly one consumer, the composition root, which is
    the one place allowed to name a concrete adapter (P3). ``core/ports.py`` draws that line
    itself, between Protocols that are load-bearing and ones that are ceremonial.
    """

    def __init__(self, *, statm: Path = _STATM, meminfo: Path = _MEMINFO) -> None:
        self._statm = statm
        self._meminfo = meminfo
        # Decided once. Whether this host has procfs is a property of the host, not of the moment,
        # and re-deciding it on every read would mean a `stat` per metric per scrape forever.
        self._has_procfs = statm.exists()
        self._page_size = _page_size()

    def rss_bytes(self) -> int | None:
        """This process's resident set, in bytes. ``None`` off procfs."""
        if not self._has_procfs:
            return None
        fields = self._statm.read_text().split()
        # A statm that exists but cannot be parsed is a broken instrument, not a zero. Raising
        # puts the name in the snapshot's `absent` list with a logged traceback, which is the
        # honest report; returning 0 here would claim the robot uses no memory.
        return int(fields[_STATM_RESIDENT_FIELD]) * self._page_size

    def mem_available_bytes(self) -> int | None:
        """The machine's available memory, in bytes. ``None`` off procfs."""
        if not self._has_procfs:
            return None
        for line in self._meminfo.read_text().splitlines():
            if line.startswith(_MEM_AVAILABLE_KEY):
                # "MemAvailable:    1543210 kB"
                return int(line.split()[1]) * 1024
        raise ValueError(f"{_MEM_AVAILABLE_KEY} absent from {self._meminfo}")


def _page_size() -> int:
    """Bytes per page, for turning ``statm``'s page counts into something a human reads.

    ``os.sysconf`` is POSIX-only and raises ``ValueError`` for an unknown name, so the 4 KiB
    fallback covers the hosts where this class returns ``None`` anyway and keeps construction from
    being a platform hazard.
    """
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (
        AttributeError,
        ValueError,
        OSError,
    ):  # pragma: no cover - non-POSIX construction
        return 4096


__all__ = ["ProcResources"]
