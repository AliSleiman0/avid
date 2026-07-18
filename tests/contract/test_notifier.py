"""Contract suite for the ``ServiceNotifier`` port (AVID-38, SDS §3.11.3).

A port's contract test runs against *every* adapter, real and fake, so the fake can
never quietly drift from the real thing (P6, SDS §3.9.2). The shared tier below is
parametrized over both :class:`FakeServiceNotifier` and :class:`SystemdNotifier` and
asserts the one invariant that must hold for either: calling ``ready``/``watchdog``/
``stopping`` makes that notification observable, in order. The fake observes it in its
recorded list; the real one is observed by binding an actual ``AF_UNIX`` datagram
socket and reading the wire bytes — proving the sd_notify handshake for real.

The real adapter's socket is POSIX-only, so its parametrization is skipped on Windows;
the fake runs everywhere, and the no-socket no-op path is asserted cross-platform.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from avid.adapters import FakeServiceNotifier, SystemdNotifier
from avid.core.ports import ServiceNotifier

# A notifier plus a reader that returns the next notification as its bare token
# ("READY" / "WATCHDOG" / "STOPPING"), however that adapter surfaces it.
NotifierCase = tuple[ServiceNotifier, Callable[[], Awaitable[str]]]


@pytest.fixture(params=["FakeServiceNotifier", "SystemdNotifier"])
def notifier_case(
    request: pytest.FixtureRequest, tmp_path: object
) -> AsyncIterator[NotifierCase]:
    if request.param == "FakeServiceNotifier":
        fake = FakeServiceNotifier()

        async def read_fake() -> str:
            return fake.notifications[-1]

        yield fake, read_fake
        return

    if sys.platform == "win32":
        pytest.skip("AF_UNIX / sd_notify datagrams are a POSIX contract")

    # A real datagram socket standing in for systemd's $NOTIFY_SOCKET.
    sock_path = str(tmp_path / "notify.sock")  # type: ignore[attr-defined]
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(2.0)
    notifier = SystemdNotifier(address=sock_path)

    async def read_real() -> str:
        # The datagram was already sent synchronously before this call, so recvfrom
        # returns at once; split "READY=1" -> "READY" to compare tokens uniformly.
        data, _ = server.recvfrom(64)
        return data.decode().split("=", 1)[0]

    yield notifier, read_real
    notifier.close()
    server.close()


# --- shared contract: every ServiceNotifier adapter must satisfy it ----------


def test_adapter_satisfies_the_port(notifier_case: NotifierCase) -> None:
    notifier, _ = notifier_case
    assert isinstance(notifier, ServiceNotifier)


async def test_each_call_is_observable_in_order(notifier_case: NotifierCase) -> None:
    notifier, read_next = notifier_case

    await notifier.ready()
    assert await read_next() == "READY"

    await notifier.watchdog()
    assert await read_next() == "WATCHDOG"

    await notifier.stopping()
    assert await read_next() == "STOPPING"


# --- FakeServiceNotifier-specific: the recorded sequence ---------------------


async def test_fake_records_the_full_sequence() -> None:
    fake = FakeServiceNotifier()
    await fake.ready()
    await fake.watchdog()
    await fake.watchdog()
    await fake.stopping()
    assert fake.notifications == ["READY", "WATCHDOG", "WATCHDOG", "STOPPING"]


# --- SystemdNotifier-specific: off-systemd is a clean no-op ------------------


async def test_systemd_notifier_without_a_socket_is_a_noop() -> None:
    """No ``$NOTIFY_SOCKET`` (a laptop, not a ``Type=notify`` unit) -> the notifier
    holds no socket and every call returns without raising. Cross-platform: with no
    address it never touches ``AF_UNIX``."""
    notifier = SystemdNotifier(address=None)
    await notifier.ready()
    await notifier.watchdog()
    await notifier.stopping()
    notifier.close()
    notifier.close()  # idempotent — safe when nothing was ever opened


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="abstract-namespace unix sockets are Linux-specific",
)
async def test_systemd_notifier_handles_abstract_namespace(tmp_path: object) -> None:
    """A ``$NOTIFY_SOCKET`` beginning with ``@`` names the Linux abstract namespace,
    mapped to a leading NUL — systemd's own convention (unix(7))."""
    name = "\0avid-test-notify"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(name)
    server.settimeout(2.0)
    try:
        notifier = SystemdNotifier(address="@avid-test-notify")
        await notifier.ready()
        data, _ = server.recvfrom(64)
        assert data == b"READY=1"
        notifier.close()
    finally:
        server.close()
