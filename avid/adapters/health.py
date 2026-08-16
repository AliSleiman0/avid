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
import json
import logging
from uuid import uuid4

from avid.core.ports import BehaviorTools

_log = logging.getLogger("avid.adapters.health")

# SDS §9.5: the control API binds loopback only; a routable bind is a security bug.
# Mirrors core.config._LOOPBACK — asserted here too so the socket cannot be opened
# on a public address even if construction bypassed the config validator.
_LOOPBACK: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_MAX_REQUEST_BYTES = 8192  # a health GET is tiny; cap so a bad client cannot grow us.
# Also the ceiling on a POST body: `{"duration_s": 3600}` is 22 bytes, so anything near this
# is not a client we want to keep reading from.

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


def _response(status: str, body: bytes, *, content_type: str = "text/plain") -> bytes:
    """Build one HTTP/1.1 response. Replaces the canned constants for the dynamic routes.

    ``GET /health`` keeps its literal byte string on purpose: it is the systemd watchdog's probe,
    the one route whose exact bytes a regression test pins, and it should not start depending on a
    formatter that could change under it.
    """
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1") + body


_METHOD_NOT_ALLOWED = (
    b"HTTP/1.1 405 Method Not Allowed\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 18\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"method not allowed"
)


class HealthServer:
    """The ``GET /health`` endpoint of the local control API (SDS §9.5).

    Constructed only by the composition root (P3). ``start`` binds the socket and
    begins serving; ``stop`` closes it and drains, well inside systemd's 5 s stop
    budget (SDS §9.2). Pass ``port=0`` to bind an ephemeral port and read it back via
    :attr:`bound_port` — how the adapter test avoids a fixed-port clash.
    """

    def __init__(
        self, *, bind: str, port: int, behavior: BehaviorTools | None = None
    ) -> None:
        if bind not in _LOOPBACK:
            raise ValueError(
                f"HealthServer refuses to bind {bind!r}: the control API is loopback "
                f"only (one of {sorted(_LOOPBACK)}). A routable bind is an "
                f"unauthenticated socket — a security bug (SDS §9.5)."
            )
        self._bind = bind
        self._port = port
        # §9.5's POST /quiet, behind the same Protocol the `set_quiet` tool dispatches
        # against (#243). Optional so the M0 health-only wiring still constructs; a `None`
        # here answers 503 rather than pretending the route worked.
        self._behavior = behavior
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
            # The headers are read rather than discarded now: POST needs Content-Length to know
            # how much body to expect, and reading to the blank line is what lets the client's
            # write complete before we reply and close.
            length = await self._read_headers(reader)
            writer.write(await self._route(method, path, reader, length))
            await writer.drain()
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            _log.warning("control API request dropped: %s", exc)
        finally:
            writer.close()

    async def _route(
        self,
        method: str,
        path: str,
        reader: asyncio.StreamReader,
        length: int | None,
    ) -> bytes:
        """Dispatch one request to a response. Never raises — the caller logs and closes.

        Unknown paths stay 404 and a known path with the wrong method is 405, which is the
        distinction the single boolean this replaced could not make: a `GET /quiet` used to look
        exactly like a typo.
        """
        if path == "/health":
            return _OK if method == "GET" else _METHOD_NOT_ALLOWED
        if path == "/quiet":
            if method != "POST":
                return _METHOD_NOT_ALLOWED
            return await self._quiet(reader, length)
        return _NOT_FOUND

    async def _quiet(self, reader: asyncio.StreamReader, length: int | None) -> bytes:
        """``POST /quiet {"duration_s": N}`` — §9.5's row, §10.4's manual override.

        The *same* state the ``set_quiet`` tool sets (#243), through the same Protocol. Two doors,
        one room: §9.5 describes this route as "also reachable via set_quiet tool", and the only way
        to make that true rather than approximately true is for both to call one method.

        Every failure here is a 4xx, never a 500 and never an exception reaching the loop — §3.12.3's
        rule that a crashing control endpoint must not take the robot down. Loopback binding is still
        the whole of the authentication (§9.5); this route mutates behaviour, which is precisely why
        the bind assertion in ``__init__`` is defence in depth rather than decoration.
        """
        if self._behavior is None:
            return _response("503 Service Unavailable", b"no behaviour engine")
        if length is None or length <= 0 or length > _MAX_REQUEST_BYTES:
            return _response("411 Length Required", b"length required")
        try:
            raw = await reader.readexactly(length)
            payload = json.loads(raw)
            duration = int(payload["duration_s"])
        except (
            asyncio.IncompleteReadError,
            ValueError,
            TypeError,
            KeyError,
        ):
            return _response("400 Bad Request", b'expected {"duration_s": <seconds>}')
        try:
            until = await self._behavior.set_quiet(duration, correlation_id=uuid4())
        except ValueError as exc:
            return _response("400 Bad Request", str(exc).encode("utf-8"))
        _log.info("quiet requested over HTTP: %ds, until %d", duration, until)
        return _response(
            "200 OK",
            json.dumps({"ok": True, "until": until}).encode("utf-8"),
            content_type="application/json",
        )

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> int | None:
        """Read to the blank line ending the request head; return ``Content-Length`` if present.

        Capped at ``_MAX_REQUEST_BYTES`` so an endless header stream cannot grow us unbounded —
        the same bound the previous drain used, kept rather than re-derived.
        """
        length: int | None = None
        read = 0
        while read < _MAX_REQUEST_BYTES:
            line = await reader.readline()
            read += len(line)
            if line in (b"\r\n", b"\n", b""):
                return length
            name, sep, value = line.decode("latin-1").partition(":")
            if sep and name.strip().lower() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = None
        return length
