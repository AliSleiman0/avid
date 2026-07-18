"""End-to-end boot & supervision proof (AVID-41, SDS §3.11.3, §9.5).

CI cannot run systemd, but it *can* prove the handshake the watchdog depends on: an
external ``python -m avid`` process, given a stand-in ``$NOTIFY_SOCKET``, must announce
``READY=1`` once it reaches IDLE, answer ``GET /health`` with 200 while it runs, and —
on SIGTERM — announce ``STOPPING=1`` and exit 0. That is the whole ``Type=notify``
contract minus systemd itself; the physical restart-on-kill is proven on the Pi
(AVID-42).

Skipped on Windows: SIGTERM, ``AF_UNIX``, and ``$NOTIFY_SOCKET`` are POSIX contracts
and the Pi/CI target is Linux (SDS §3.11.2). Runs on the 3.11 and 3.13 Linux CI legs,
under ``PYTHONASYNCIODEBUG=1`` (the child inherits the env, so it boots in debug mode
too).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM / AF_UNIX / NOTIFY_SOCKET are POSIX contracts; CI/Pi target is Linux",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PI_TOML = _REPO_ROOT / "config" / "pi.toml"
# pi.toml pins the control API here (SDS §9.5). CI runs tests serially, so the fixed
# port does not clash with tests/e2e/test_boot.py's own short-lived server.
_HEALTH_URL = "http://127.0.0.1:8787/health"

_BOOT_TIMEOUT_S = 15.0
_STOP_TIMEOUT_S = 10.0


def _recv_until(sock: socket.socket, expected: bytes, timeout: float) -> bool:
    """Read datagrams until one equals *expected* or *timeout* elapses.

    Skips any interleaved ``WATCHDOG=1`` so the assertion is order-robust even if the
    ping interval were crossed mid-test — we care that READY/STOPPING each *arrive*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            data, _ = sock.recvfrom(64)
        except TimeoutError:
            return False
        if data == expected:
            return True
    return False


def _poll_health(timeout: float) -> int | None:
    """Poll ``/health`` until it answers or *timeout* elapses; return the status code."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=1.0) as resp:
                return int(resp.status)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.05)
    return None


def test_boot_reaches_idle_notifies_serves_health_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    # A real datagram socket stands in for systemd's $NOTIFY_SOCKET. Bound before the
    # child launches, so a READY sent the instant it reaches IDLE is buffered for us.
    sock_path = tmp_path / "notify.sock"
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind(str(sock_path))

    # Inherit the env (so PYTHONASYNCIODEBUG reaches the child on CI) plus the socket.
    env = {**os.environ, "NOTIFY_SOCKET": str(sock_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "avid", "--config", str(_PI_TOML)],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 1. It reaches IDLE and announces readiness to its supervisor.
        assert _recv_until(notify, b"READY=1", _BOOT_TIMEOUT_S), (
            "child never sent READY=1"
        )
        # 2. /health answers 200 while the loop is live.
        assert _poll_health(_BOOT_TIMEOUT_S) == 200
        assert proc.poll() is None, "child exited before shutdown"

        # 3. SIGTERM -> announces a deliberate stop, then exits cleanly.
        proc.send_signal(signal.SIGTERM)
        assert _recv_until(notify, b"STOPPING=1", _STOP_TIMEOUT_S), (
            "child never sent STOPPING=1"
        )
        assert proc.wait(timeout=_STOP_TIMEOUT_S) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_STOP_TIMEOUT_S)
        notify.close()
