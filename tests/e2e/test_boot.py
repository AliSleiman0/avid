"""End-to-end boot: `avid --config config/sim.toml` reaches IDLE, SIGTERM exits 0.

This is AVID-14's headline acceptance criterion, exercised against the real console
entry point in a subprocess. Skipped on Windows: SIGTERM / the loop's signal handling
is a POSIX contract, and the Pi/CI target is Linux (SDS §3.11.2).
"""

from __future__ import annotations

import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM/add_signal_handler is a POSIX contract; CI/Pi target is Linux",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIM_TOML = _REPO_ROOT / "config" / "sim.toml"

_BOOT_TIMEOUT_S = 15.0
_STOP_TIMEOUT_S = 10.0


def _wait_for_line(proc: subprocess.Popen[str], needle: str, timeout: float) -> bool:
    """Read *proc*'s merged output until a line contains *needle* or *timeout* elapses."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:  # EOF: the process exited early
            return False
        if needle in line:
            return True
    return False


def test_boot_reaches_idle_and_sigterm_exits_zero() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "avid", "--config", str(_SIM_TOML)],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # logging goes to stderr; merge so we can read it
        text=True,
        bufsize=1,
    )
    try:
        assert _wait_for_line(proc, "reached IDLE", _BOOT_TIMEOUT_S), (
            "robot did not reach IDLE within the timeout"
        )
        assert proc.poll() is None, "process exited before SIGTERM"

        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=_STOP_TIMEOUT_S)
        assert returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_STOP_TIMEOUT_S)
