"""Test-suite–wide fixtures. Home of the P8 async-debug gate.

P8 (SDS §725, §14.9; CLAUDE.md): "no blocking I/O on the loop" — the CI
async-debug job runs the suite under ``PYTHONASYNCIODEBUG=1`` and a slow callback
must fail the run.

The gate is CI-opt-in: it arms only when ``PYTHONASYNCIODEBUG`` is set, so
day-to-day ``pytest`` stays quiet. When armed, every test loop is created in debug
mode with ``slow_callback_duration`` lowered from asyncio's 100 ms default to
P8's **50 ms**, so the loop reports everything at or past the bar. A handler on the
``asyncio`` logger records those warnings and ``pytest_sessionfinish`` grades them.

**What counts as a failure, and why it is not simply "any warning" (#328).** The
50 ms bar detects; it no longer convicts on its own. A warning fails the run when
it is either

* **gross** — at or past :data:`_GROSS_MULTIPLE` × the bar, i.e. **100 ms**, which
  is asyncio's *own* default threshold. Anything asyncio would have warned about
  unprompted is a stall, full stop; or
* **corroborated** — the same callback grazed :data:`_REPEAT_THRESHOLD` times or
  more in one session. Blocking I/O is a property of code, so it recurs; a shared
  CI runner descheduling a coroutine for 3 ms does not pick the same victim twice.

Anything else is a **graze**: printed loudly, in its own block, above the verdict —
never dropped silently, because a disarmed check that looks like a passing one is
this project's most expensive recurring mistake (CLAUDE.md §7.1).

⚠️ **This is a real, bounded loss of sensitivity and it is worth stating plainly.**
A genuine one-off blocking call between 50 and 100 ms now passes the gate. What
was traded for it: seven consecutive false positives across seven unrelated PRs
(0.052–0.083 s, five different untouched subscribers, zero code defects), three of
them on one M9 branch, each costing a rerun *and* a judgement call about whether
the red was real — and a gate whose reds are usually wrong is a gate people learn
to rerun without reading. The defects P8 exists for are not marginal: #168's ONNX
pool starved the loop for hundreds of milliseconds on **every** embed, so it is
both gross and corroborated twice over. Widening a bar to fit a measurement is a
last resort (CLAUDE.md §7); the 50 ms bar is therefore **unchanged and still
reported against** — what changed is that a single graze is now evidence rather
than a verdict.

Two things the env var does *not* do on its own, which is why ``pytest_configure``
handles them when armed:

* pytest-asyncio (1.x) runs every test through ``asyncio.Runner(debug=...)`` whose
  flag comes from *its own* ``--asyncio-debug`` option (default off) — and
  ``Runner(debug=False)`` forces ``set_debug(False)`` after loop creation, so
  ``PYTHONASYNCIODEBUG`` never reaches the test loops. We bridge it by flipping
  that option on (``config.option.asyncio_debug = True``).
* asyncio's default slow-callback threshold is 100 ms, not P8's 50 ms. We lower it
  at loop creation via a global event-loop policy — the seam pytest-asyncio builds
  test loops from — since a sync autouse fixture runs *outside* the test loop
  (``get_running_loop()`` raises there) and overriding the ``event_loop_policy``
  fixture is deprecated in pytest-asyncio 1.x.

**The one carve-out — real-hardware device init (AVID-57).** On the Pi the port
contract suites run their ``real`` params, and opening a real device (``picamera2``
starting the libcamera pipeline, ALSA opening a card) is a one-time cost that the
adapters correctly offload via ``asyncio.to_thread`` — the loop runs *no* blocking
code. But under ``PYTHONASYNCIODEBUG`` the wall-clock time of the fixture's *own*
trivial resume callback is inflated by scheduler/GIL contention while the device's
multi-threaded init saturates the Pi's cores, so asyncio reports a false
slow-callback on the fixture setup/teardown coroutine. We exempt **only** that
narrow boundary, and **only** on a real-hardware run: a slow callback whose frame is
pytest-asyncio's fixture machinery is reported but does not fail. This does not blunt
P8 — the identical ``start()``/``stop()``/``capture()`` also run inside gated test
bodies (``test_start_and_stop_are_idempotent_safe``,
``test_capture_does_not_block_the_loop``), so an adapter that genuinely blocks the
loop still fails there. Fake/CI runs stay fully strict (``_ON_HARDWARE`` is False),
so e.g. a FakeMicrophone that burns CPU in a fixture is still caught.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

# P8's threshold, in seconds. Below asyncio's 100 ms default so the loop reports
# anything slower than the SDS §725 bar. Detection, not conviction — see below.
_SLOW_CALLBACK_DURATION_S = 0.05

# A warning at or past this multiple of the bar fails on its own. 2x is 100 ms, which
# is asyncio's own default slow_callback_duration: a callback the library would have
# complained about unprompted is a stall by anybody's reckoning, and no runner hiccup
# doubles the bar.
_GROSS_MULTIPLE = 2.0

# How many times one callback must graze before the run fails. Blocking I/O is a
# property of code and recurs; scheduler jitter does not pick the same victim twice.
_REPEAT_THRESHOLD = 2

# asyncio's debug messages: "Executing <Task ... coro=<f() running at PATH:LINE>> took
# N.NNN seconds", and the sibling "Handle ... took ...". The duration is what we grade;
# the first frame is the callback's own, and is what identifies it across occurrences
# (task names like `Task-781` and `eventbus:X` are per-run, so they cannot).
#
# ⚠️ Both spellings, and the second was found by probing rather than by reading: a task
# still *pending* when the warning fires reports "running at", while one that has already
# finished reports "defined at". Matching only the first silently gave every completed
# coroutine a unique identity — which cannot corroborate with itself, so the whole
# corroboration rule would have been dead for exactly the callbacks that stall inside a
# test body rather than inside a service.
_TOOK_RE = re.compile(r"took ([0-9]+(?:\.[0-9]+)?) seconds")
_FRAME_RE = re.compile(r"(?:running|defined) at ([^\s>,]+)")

# Arm only when asyncio debug is requested — mirrors how CI invokes the gate
# (`PYTHONASYNCIODEBUG=1 pytest`) and keeps ordinary local runs silent.
_ARMED = bool(os.environ.get("PYTHONASYNCIODEBUG"))

# The device-tree node the kernel exposes on a Pi. Absent on CI/laptops, which is
# exactly how those hosts read as not-real-hardware.
_MODEL_NODE = Path("/proc/device-tree/model")

# Env values that read as "off"; anything else present means "on".
_FALSEY = frozenset({"", "0", "false", "False"})

# Substring asyncio's debug message carries when the slow callback is pytest-asyncio's
# fixture setup/teardown coroutine — e.g. "...running at .../pytest_asyncio/plugin.py:403".
# A test-body callback names the test module instead, so this alone separates the
# one-time device-init boundary from steady-state operation.
_FIXTURE_FRAME_MARKER = "pytest_asyncio"


def _on_real_hardware() -> bool:
    """Whether this run drives real device adapters — mirrors contract ``on_pi()``.

    An explicit ``AVID_HARDWARE`` override wins (so the seal run forces it and both
    branches stay testable); otherwise the Pi is detected via its device-tree model
    node. Only on such a run is the device-init carve-out active; everywhere else the
    P8 gate stays fully strict. See the module docstring.
    """
    override = os.environ.get("AVID_HARDWARE")
    if override is not None:
        return override not in _FALSEY
    try:
        return "raspberry pi" in _MODEL_NODE.read_text(errors="ignore").lower()
    except OSError:
        return False


_ON_HARDWARE = _on_real_hardware()


class _P8LoopPolicy(asyncio.DefaultEventLoopPolicy):
    """Builds every test loop at P8's 50 ms slow-callback threshold.

    Debug mode itself is turned on via pytest-asyncio's Runner (see
    ``pytest_configure``); this only tightens *how slow* counts as slow.
    """

    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        loop = super().new_event_loop()
        loop.slow_callback_duration = _SLOW_CALLBACK_DURATION_S
        return loop


@dataclass(frozen=True, slots=True)
class _SlowCallback:
    """One slow-callback warning, parsed into the two things the verdict needs."""

    message: str
    seconds: float | None
    identity: str

    @classmethod
    def parse(cls, message: str) -> _SlowCallback:
        took = _TOOK_RE.search(message)
        frame = _FRAME_RE.search(message)
        return cls(
            message=message,
            seconds=float(took.group(1)) if took else None,
            # Falling back to the whole message means an unparseable warning can only
            # ever match itself — it is never silently merged with another callback's
            # count, which would be a way to manufacture corroboration.
            identity=frame.group(1) if frame else message,
        )


class _SlowCallbackCatcher(logging.Handler):
    """Records asyncio's slow-callback warnings without swallowing them, then grades them.

    Three outcomes, and every one of them is printed (CLAUDE.md §7.1 — a silently
    disarmed check is indistinguishable from a passing one):

    * :attr:`exempt` — on a real-hardware run, a warning attributed to pytest-asyncio's
      fixture setup/teardown is the one-time device open/close boundary, which is
      threaded and so P8-clean (see the module docstring).
    * :attr:`hits` — **gated**: gross (≥ :data:`_GROSS_MULTIPLE` × the bar) or
      corroborated (the same callback ≥ :data:`_REPEAT_THRESHOLD` times). These fail.
    * :attr:`grazed` — over the bar, once, by less than double. Reported, not fatal.

    ``on_hardware`` defaults to the detected environment; it is a parameter so both
    branches are unit-testable.
    """

    def __init__(self, *, on_hardware: bool = _ON_HARDWARE) -> None:
        super().__init__(level=logging.WARNING)
        self._on_hardware = on_hardware
        self.seen: list[_SlowCallback] = []
        self.exempt: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        # asyncio logs "Executing <...> took N.NNN seconds" / "Handle ... took ..."
        # for slow callbacks and slow selector polls; both mean the loop stalled.
        if "took" not in message or "seconds" not in message:
            return
        if self._on_hardware and _FIXTURE_FRAME_MARKER in message:
            self.exempt.append(message)
        else:
            self.seen.append(_SlowCallback.parse(message))

    def _counts(self) -> Counter[str]:
        return Counter(warning.identity for warning in self.seen)

    def verdict(self, warning: _SlowCallback, counts: Counter[str]) -> str | None:
        """Why *warning* fails the run, or ``None`` if it is only a graze.

        Returns the *reason* rather than a bool so the report can state which rule
        convicted — a verdict whose grounds are not printed is one nobody can argue
        with, and #328 exists because seven of these were argued with.
        """
        if warning.seconds is None:
            # An instrument that cannot read its own measurement must not downgrade
            # it. Louder than the alternative, and it has never fired.
            return "unparseable duration — graded as a stall rather than assumed benign"
        gross = _GROSS_MULTIPLE * _SLOW_CALLBACK_DURATION_S
        if warning.seconds >= gross:
            return f"{warning.seconds:.3f}s >= {gross:.3f}s, asyncio's own default bar"
        repeats = counts[warning.identity]
        if repeats >= _REPEAT_THRESHOLD:
            return f"grazed {repeats}x at the same frame - corroborated"
        return None

    @property
    def hits(self) -> list[str]:
        """The warnings that fail the run, in the order they were seen."""
        counts = self._counts()
        return [w.message for w in self.seen if self.verdict(w, counts) is not None]

    @property
    def grazed(self) -> list[str]:
        """Over the bar once, by less than double: reported, not gated."""
        counts = self._counts()
        return [w.message for w in self.seen if self.verdict(w, counts) is None]


_catcher = _SlowCallbackCatcher()


def pytest_configure(config: pytest.Config) -> None:
    if _ARMED:
        # Turn on pytest-asyncio's own debug mode (its Runner otherwise forces it
        # off), lower the threshold to 50 ms, and start listening.
        config.option.asyncio_debug = True
        asyncio.set_event_loop_policy(_P8LoopPolicy())
        logging.getLogger("asyncio").addHandler(_catcher)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    # Report the exempt device-init boundary loudly but without failing — a silent
    # carve-out is a debugging trap; a visible one is an audit trail (AVID-57).
    if _catcher.exempt and reporter is not None:
        reporter.write_sep(
            "=", "P8 async-debug gate — real-hardware device init (exempt)", yellow=True
        )
        reporter.write_line(
            f"{len(_catcher.exempt)} slow-callback warning(s) at the one-time device "
            "open/close boundary — threaded (P8-clean), not gated. See tests/conftest.py."
        )
        for hit in _catcher.exempt:
            reporter.write_line(f"  - {hit}")

    # Grazes print BEFORE the verdict, and print whether or not the run fails (#328,
    # CLAUDE.md §7.1: every criterion reports before any verdict is decided, and a
    # check that has been narrowed says so out loud). A run that is green with three
    # grazes in it is a different fact from a run that is green with none, and the
    # difference is exactly where a real regression would first appear.
    grazed = _catcher.grazed
    if grazed and reporter is not None:
        reporter.write_sep(
            "=", "P8 async-debug gate — grazes (reported, not gated)", yellow=True
        )
        reporter.write_line(
            f"{len(grazed)} slow-callback warning(s) over "
            f"{_SLOW_CALLBACK_DURATION_S * 1000:.0f} ms but under "
            f"{_GROSS_MULTIPLE * _SLOW_CALLBACK_DURATION_S * 1000:.0f} ms, each seen "
            f"once. Runner jitter looks exactly like this; blocking I/O recurs (#328)."
        )
        for hit in grazed:
            reporter.write_line(f"  - {hit}")

    hits = _catcher.hits
    if not hits:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if reporter is not None:
        counts = _catcher._counts()
        reporter.write_sep("=", "P8 async-debug gate FAILED", red=True)
        reporter.write_line(
            f"{len(hits)} slow-callback warning(s) — blocking I/O on the loop. "
            f"Bar is {_SLOW_CALLBACK_DURATION_S * 1000:.0f} ms; a warning is gated "
            f"when it is gross or corroborated (see tests/conftest.py):"
        )
        for warning in _catcher.seen:
            reason = _catcher.verdict(warning, counts)
            if reason is not None:
                reporter.write_line(f"  - [{reason}] {warning.message}")
