"""ServiceNotifier adapters — the systemd one and its fake (AVID-38, SDS §3.11.3).

Two implementations of the :class:`~avid.core.ports.ServiceNotifier` port:

* :class:`SystemdNotifier` — the real one. Speaks the ``sd_notify`` protocol: a
  datagram of ``READY=1`` / ``WATCHDOG=1`` / ``STOPPING=1`` to the ``AF_UNIX`` socket
  systemd hands us in ``$NOTIFY_SOCKET`` (injected via config, per P7). Given no
  address — running off systemd, e.g. a laptop — every call is a no-op, which is the
  standard sd_notify behaviour and why the same binary runs supervised or not.
* :class:`FakeServiceNotifier` — records the calls in order and *is* the simulator
  (P6, SDS §3.9.2), so the fake can never drift from the real contract: the contract
  suite (``tests/contract/test_notifier.py``) runs against both.

Both are constructed only by the composition root or a test fixture (P3). Sends are
best-effort and never raise into the loop: a supervisor notification is a
notification, not an obligation (SDS §3.12.3 — nothing but a bad key at boot stops
the robot), and the datagram socket is non-blocking so the send never touches the
loop's time budget (P8).
"""

from __future__ import annotations

import logging
import socket

_log = logging.getLogger("avid.adapters.notifier")

# The three sd_notify state datagrams this port needs (see sd_notify(3)).
_READY = b"READY=1"
_WATCHDOG = b"WATCHDOG=1"
_STOPPING = b"STOPPING=1"


class FakeServiceNotifier:
    """The :class:`~avid.core.ports.ServiceNotifier` fake (P6): every call is recorded.

    ``notifications`` is the ordered, assertable record of what the process told its
    supervisor — the same role ``FakeDisplay.frames`` plays for the screen. This is
    the notifier wired by the all-fake laptop profile, so a laptop run exercises the
    exact ready/watchdog/stopping sequence the Pi does, minus the socket.
    """

    def __init__(self) -> None:
        # In call order: "READY", "WATCHDOG" (one per ping), "STOPPING".
        self.notifications: list[str] = []

    async def ready(self) -> None:
        self.notifications.append("READY")

    async def watchdog(self) -> None:
        self.notifications.append("WATCHDOG")

    async def stopping(self) -> None:
        self.notifications.append("STOPPING")


class SystemdNotifier:
    """The real :class:`~avid.core.ports.ServiceNotifier`: sd_notify over ``AF_UNIX``.

    *address* is the value of ``$NOTIFY_SOCKET`` (injected as ``config.notify_socket``,
    read in ``core/config.py`` — the sole P7-sanctioned env site). A leading ``@`` marks
    the Linux abstract namespace, mapped to a leading NUL per the kernel convention. A
    ``None`` or empty address means we are not running under a ``Type=notify`` unit, so
    the notifier holds no socket and every method returns immediately.
    """

    def __init__(self, address: str | None) -> None:
        self._sock: socket.socket | None = None
        self._target: str | bytes | None = None
        if not address:
            return
        # Abstract-namespace sockets start with '@', which stands in for a NUL byte;
        # the address is then bytes, not a filesystem path (see unix(7)).
        self._target = b"\0" + address[1:].encode() if address[0] == "@" else address
        # SOCK_DGRAM + non-blocking: sd_notify is connectionless and must not stall the
        # loop (P8). AF_UNIX is Linux/POSIX; this adapter is only built there (the Pi),
        # never in the Windows sim, so referencing it lazily here is safe. The ignore is
        # because mypy checks against win32, where the stdlib stub omits AF_UNIX.
        self._sock = socket.socket(
            socket.AF_UNIX,  # type: ignore[attr-defined]  # POSIX-only; Pi-only adapter
            socket.SOCK_DGRAM,
        )
        self._sock.setblocking(False)

    async def ready(self) -> None:
        self._send(_READY)

    async def watchdog(self) -> None:
        self._send(_WATCHDOG)

    async def stopping(self) -> None:
        self._send(_STOPPING)

    def close(self) -> None:
        """Release the socket. Idempotent; safe when no socket was ever opened."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _send(self, message: bytes) -> None:
        """Fire one datagram, best-effort. A failed notify is logged, never raised:
        the supervisor may restart us, but the robot does not crash over a lost ping."""
        if self._sock is None or self._target is None:
            return
        try:
            self._sock.sendto(message, self._target)
        except OSError as exc:  # pragma: no cover - defensive; the socket is local
            _log.warning("sd_notify %s failed: %s", message.decode(), exc)
