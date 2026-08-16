"""The local control API adapter — GET /health (AVID-40, SDS §9.5).

Exercised against a real socket on an ephemeral port (``port=0``), so it proves the
actual bind/serve/stop path rather than a mock of it. A raw asyncio client speaks just
enough HTTP to read the status line and body — no third-party client, matching the
adapter's own stdlib-only design.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

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


# --- POST /quiet (#244, SDS §9.5, §10.4) ----------------------------------------------------


class _RecordingBehavior:
    """A ``BehaviorTools`` double that records the durations it was asked for (SDS §14.3)."""

    def __init__(self, *, until: int = 1_800_003_600, boom: bool = False) -> None:
        self.durations: list[int] = []
        self._until = until
        self._boom = boom

    async def set_quiet(self, duration_s: int, *, correlation_id: UUID) -> int:
        if self._boom:
            raise ValueError("quiet duration must be positive")
        self.durations.append(duration_s)
        return self._until


async def _speak(server: HealthServer, raw: bytes) -> bytes:
    """Speak raw HTTP to the bound port and read the whole response.

    Raw bytes rather than an HTTP client, matching the rest of this file: the route's contract is
    what goes over the wire, and a client library would paper over exactly the header handling this
    PR changed.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    try:
        writer.write(raw)
        await writer.drain()
        return await reader.read()
    finally:
        writer.close()


def _post(body: str, *, headers: str = "") -> bytes:
    return (
        f"POST /quiet HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(body)}\r\n{headers}\r\n{body}"
    ).encode("latin-1")


async def test_post_quiet_sets_the_same_state_the_tool_sets() -> None:
    """AC-2, and the assertion that matters is *which* state: §9.5 describes this route as "also
    reachable via set_quiet tool", and the only way to make that true rather than approximately
    true is for both doors to call one method on one port."""
    behavior = _RecordingBehavior(until=1_800_003_600)
    server = HealthServer(bind="127.0.0.1", port=0, behavior=behavior)  # type: ignore[arg-type]
    await server.start()
    try:
        response = await _speak(server, _post('{"duration_s": 3600}'))
    finally:
        await server.stop()

    assert b"200 OK" in response
    assert b'"until": 1800003600' in response
    assert behavior.durations == [3600]


@pytest.mark.parametrize(
    "body",
    ["not json", "{}", '{"duration_s": "an hour"}', '{"duration_s": null}', "[]"],
)
async def test_a_malformed_body_is_a_400_and_never_a_500(body: str) -> None:
    """§3.12.3: a crashing control endpoint must not take the robot down. Every one of these is a
    client error, so every one is a 4xx — and none of them reaches the event loop as an exception."""
    behavior = _RecordingBehavior()
    server = HealthServer(bind="127.0.0.1", port=0, behavior=behavior)  # type: ignore[arg-type]
    await server.start()
    try:
        response = await _speak(server, _post(body))
    finally:
        await server.stop()

    assert b"400 Bad Request" in response
    assert behavior.durations == []


async def test_a_rejected_duration_comes_back_as_the_ports_own_message() -> None:
    """A zero or negative duration is rejected by ``BehaviorService``, not re-validated here — one
    rule, one place. The message is passed through so the caller learns *why*."""
    server = HealthServer(
        bind="127.0.0.1",
        port=0,
        behavior=_RecordingBehavior(boom=True),  # type: ignore[arg-type]
    )
    await server.start()
    try:
        response = await _speak(server, _post('{"duration_s": 0}'))
    finally:
        await server.stop()

    assert b"400 Bad Request" in response
    assert b"positive" in response


async def test_a_body_with_no_length_is_refused() -> None:
    """Without ``Content-Length`` there is no way to know when the body ends, and reading until EOF
    on a keep-alive connection would hang the handler. 411 says so honestly."""
    server = HealthServer(
        bind="127.0.0.1",
        port=0,
        behavior=_RecordingBehavior(),  # type: ignore[arg-type]
    )
    await server.start()
    try:
        response = await _speak(
            server, b"POST /quiet HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
    finally:
        await server.stop()

    assert b"411 Length Required" in response


async def test_the_wrong_method_is_405_rather_than_404() -> None:
    """The distinction the single boolean this replaced could not make: a ``GET /quiet`` used to
    look exactly like a typo'd path, which is the least useful thing a control API can say."""
    server = HealthServer(
        bind="127.0.0.1",
        port=0,
        behavior=_RecordingBehavior(),  # type: ignore[arg-type]
    )
    await server.start()
    try:
        wrong_method = await _speak(
            server, b"GET /quiet HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        unknown_path = await _speak(
            server, b"GET /nope HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
    finally:
        await server.stop()

    assert b"405 Method Not Allowed" in wrong_method
    assert b"404 Not Found" in unknown_path


async def test_quiet_without_a_behaviour_engine_is_503_not_a_silent_success() -> None:
    """The M0 health-only wiring has no behaviour engine. Answering 200 would tell a caller their
    quiet request landed when nothing recorded it — the worst available answer."""
    server = HealthServer(bind="127.0.0.1", port=0)
    await server.start()
    try:
        response = await _speak(server, _post('{"duration_s": 3600}'))
    finally:
        await server.stop()

    assert b"503 Service Unavailable" in response
