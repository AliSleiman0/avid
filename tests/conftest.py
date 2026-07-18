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
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

# P8's threshold, in seconds. Below asyncio's 100 ms default so the loop reports
# anything slower than the SDS §725 bar.
_SLOW_CALLBACK_DURATION_S = 0.05

# Arm only when asyncio debug is requested — mirrors how CI invokes the gate
# (`PYTHONASYNCIODEBUG=1 pytest`) and keeps ordinary local runs silent.
_ARMED = bool(os.environ.get("PYTHONASYNCIODEBUG"))


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
    """Records asyncio's slow-callback warnings without swallowing them."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hits: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        # asyncio logs "Executing <...> took N.NNN seconds" / "Handle ... took ..."
        # for slow callbacks and slow selector polls; both mean the loop stalled.
        if "took" in message and "seconds" in message:
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
    if not _catcher.hits:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "P8 async-debug gate FAILED", red=True)
        reporter.write_line(
            f"{len(_catcher.hits)} slow-callback warning(s) > "
            f"{_SLOW_CALLBACK_DURATION_S * 1000:.0f} ms — blocking I/O on the loop:"
        )
        for hit in _catcher.hits:
            reporter.write_line(f"  - {hit}")
