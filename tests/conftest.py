"""Test-suite–wide fixtures. Home of the P8 async-debug gate.

P8 (SDS §725, §14.9; CLAUDE.md): "no blocking I/O on the loop" — the CI
async-debug job runs the suite under ``PYTHONASYNCIODEBUG=1`` and a slow-callback
warning **> 50 ms** must fail the run.

The gate is CI-opt-in: it arms only when ``PYTHONASYNCIODEBUG`` is set, so
day-to-day ``pytest`` stays quiet. When armed, every test loop is created in debug
mode with ``slow_callback_duration`` lowered from asyncio's 100 ms default to
P8's **50 ms** — so any callback the loop reports already exceeds the bar and we
need not parse durations. A handler on the ``asyncio`` logger records those
warnings; ``pytest_sessionfinish`` fails the session if any were seen.

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
from pathlib import Path

import pytest

# P8's threshold, in seconds. Below asyncio's 100 ms default so the loop reports
# anything slower than the SDS §725 bar.
_SLOW_CALLBACK_DURATION_S = 0.05

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


class _SlowCallbackCatcher(logging.Handler):
    """Records asyncio's slow-callback warnings without swallowing them.

    Splits them into :attr:`hits` (gated — they fail the run) and :attr:`exempt`
    (reported only): on a real-hardware run, a warning attributed to pytest-asyncio's
    fixture setup/teardown is the one-time device open/close boundary, which is
    threaded and so P8-clean (see the module docstring). ``on_hardware`` defaults to
    the detected environment; it is a parameter so both branches are unit-testable.
    """

    def __init__(self, *, on_hardware: bool = _ON_HARDWARE) -> None:
        super().__init__(level=logging.WARNING)
        self._on_hardware = on_hardware
        self.hits: list[str] = []
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
            self.hits.append(message)


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

    if not _catcher.hits:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if reporter is not None:
        reporter.write_sep("=", "P8 async-debug gate FAILED", red=True)
        reporter.write_line(
            f"{len(_catcher.hits)} slow-callback warning(s) > "
            f"{_SLOW_CALLBACK_DURATION_S * 1000:.0f} ms — blocking I/O on the loop:"
        )
        for hit in _catcher.hits:
            reporter.write_line(f"  - {hit}")
