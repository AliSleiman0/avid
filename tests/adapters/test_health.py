"""The local control API adapter — GET /health (AVID-40, SDS §9.5).

Exercised against a real socket on an ephemeral port (``port=0``), so it proves the
actual bind/serve/stop path rather than a mock of it. A raw asyncio client speaks just
enough HTTP to read the status line and body — no third-party client, matching the
adapter's own stdlib-only design.
"""

from __future__ import annotations

import asyncio

import pytest

from avid.adapters import HealthServer


async def _request(port: int, path: str) -> tuple[int, bytes]:
    """Send ``GET <path>`` to loopback:*port*; return (status_code, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    status_line = await reader.readline()
    rest = await reader.read()  # drain to EOF (Connection: close)
    writer.close()
    await writer.wait_closed()
    status = int(status_line.split()[1])
    body = rest.rpartition(b"\r\n\r\n")[2]
    return status, body


async def test_health_returns_200_ok_when_serving() -> None:
    server = HealthServer(bind="127.0.0.1", port=0)
    await server.start()
    try:
        status, body = await _request(server.bound_port, "/health")
        assert status == 200
        assert body == b"ok"
    finally:
        await server.stop()


async def test_unknown_path_returns_404() -> None:
    server = HealthServer(bind="127.0.0.1", port=0)
    await server.start()
    try:
        status, body = await _request(server.bound_port, "/does-not-exist")
        assert status == 404
        assert body == b"not found"
    finally:
        await server.stop()


def test_refuses_a_non_loopback_bind() -> None:
    # SDS §9.5: a routable bind is an unauthenticated socket — a security bug. The
    # adapter asserts it at construction, defence-in-depth behind the config validator.
    with pytest.raises(ValueError, match="loopback"):
        HealthServer(bind="0.0.0.0", port=8787)


async def test_stop_is_idempotent() -> None:
    server = HealthServer(bind="127.0.0.1", port=0)
    await server.start()
    await server.stop()
    await server.stop()  # second stop is a no-op, not an error


def test_bound_port_before_start_is_the_requested_port() -> None:
    server = HealthServer(bind="127.0.0.1", port=8787)
    assert server.bound_port == 8787
