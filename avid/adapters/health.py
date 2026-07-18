"""The local control API — minimal ``/health`` (AVID-40, SDS §9.5).

The inbound half of the hexagon: a driving adapter systemd and a human poll, not a
device the application drives. At M1 it exposes exactly one route — ``GET /health`` —
and nothing else; ``/metrics``, ``/state``, ``/facts``, ``/events/stream`` (SDS §9.5)
arrive with the services that have something to report. Building the whole table now
would be endpoints returning nothing.

**Liveness is proven by answering at all.** ``/health`` returns 200 whenever this
handler runs — and it runs on the event loop, so a 200 *is* the loop being live (SDS
§9.5). A wedged loop cannot accept the connection; the caller times out and reads that
as unhealthy. There is nothing to check beyond "did this coroutine get to run".

**Loopback binding is the authentication** (SDS §9.5, SECURITY.md): there is no auth,
no TLS. ``config.api`` already rejects a non-loopback ``bind`` at load; this adapter
asserts it again at construction — defence in depth, so the server cannot be stood up
on a routable address even if it were built past the config gate.

Stdlib only: a hand-rolled request line + one response over :func:`asyncio.start_server`
keeps the runtime dependency set at just ``pydantic`` (as ``FakeDisplay``'s PNG encoder
does for the screen). ``core`` stays lean; no web framework enters the tree for one route.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("avid.adapters.health")

# SDS §9.5: the control API binds loopback only; a routable bind is a security bug.
# Mirrors core.config._LOOPBACK — asserted here too so the socket cannot be opened
# on a public address even if construction bypassed the config validator.
_LOOPBACK: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_MAX_REQUEST_BYTES = 8192  # a health GET is tiny; cap so a bad client cannot grow us.

_OK = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 2\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"ok"
)
_NOT_FOUND = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 9\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"not found"
)


class HealthServer:
    """The ``GET /health`` endpoint of the local control API (SDS §9.5).

    Constructed only by the composition root (P3). ``start`` binds the socket and
    begins serving; ``stop`` closes it and drains, well inside systemd's 5 s stop
    budget (SDS §9.2). Pass ``port=0`` to bind an ephemeral port and read it back via
    :attr:`bound_port` — how the adapter test avoids a fixed-port clash.
    """

    def __init__(self, *, bind: str, port: int) -> None:
        if bind not in _LOOPBACK:
            raise ValueError(
                f"HealthServer refuses to bind {bind!r}: the control API is loopback "
                f"only (one of {sorted(_LOOPBACK)}). A routable bind is an "
                f"unauthenticated socket — a security bug (SDS §9.5)."
            )
        self._bind = bind
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int:
        """The actually-bound TCP port (meaningful after :meth:`start`; resolves
        ``port=0`` to the ephemeral port the OS chose)."""
        if self._server is None:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Bind ``bind:port`` and begin serving ``/health``."""
        self._server = await asyncio.start_server(
            self._handle, host=self._bind, port=self._port
        )
        _log.info("control API listening on %s:%d", self._bind, self.bound_port)

    async def stop(self) -> None:
        """Stop serving and release the socket. Idempotent."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Answer one request, then close. ``GET /health`` -> 200, anything else 404.

        Best-effort and self-contained: a malformed request is logged and dropped, never
        allowed to bubble into the loop (a crashing control endpoint must not take the
        robot down — SDS §3.12.3).
        """
        try:
            request_line = await reader.readline()
            method, _, rest = request_line.decode("latin-1").partition(" ")
            path = rest.partition(" ")[0]
            # Drain the request headers (bounded) so the client's write completes before
            # we reply and close; we need none of them for a health GET.
            await self._drain_headers(reader)
            live = method == "GET" and path == "/health"
            writer.write(_OK if live else _NOT_FOUND)
            await writer.drain()
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            _log.warning("control API request dropped: %s", exc)
        finally:
            writer.close()

    @staticmethod
    async def _drain_headers(reader: asyncio.StreamReader) -> None:
        """Read up to the blank line that ends the request head, capped at
        ``_MAX_REQUEST_BYTES`` so an endless header stream cannot grow us unbounded."""
        read = 0
        while read < _MAX_REQUEST_BYTES:
            line = await reader.readline()
            read += len(line)
            if line in (b"\r\n", b"\n", b""):
                return
