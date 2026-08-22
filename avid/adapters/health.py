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
from dataclasses import asdict
from enum import Enum
from urllib.parse import parse_qs
from uuid import uuid4

from avid.adapters.event_tap import EventTap, render, render_drops
from avid.core.ports import BehaviorTools, FactRepository, MetricsSource, StateSource
from avid.domain.memory import Fact

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


# The SSE response head. `Cache-Control: no-cache` and the absence of Content-Length are what
# make a client stream rather than buffer; `Connection: close` because HTTP/1.1 keep-alive has no
# meaning for a response that never ends.
_SSE_HEAD = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Cache-Control: no-cache\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


def _include_superseded(query: str) -> bool | None:
    """Parse ``?include_superseded=0|1``. ``None`` means the caller asked for something else.

    ⚠️ A value that is neither 0 nor 1 is an error rather than a silent ``False``. On a privacy
    endpoint a typo that quietly *narrows* what you are shown is the wrong failure: the reader
    would see a shorter list and no reason to doubt it. Absent is fine and means live-only, which
    is the documented default and not a guess.
    """
    parsed = parse_qs(query, keep_blank_values=True)
    values = parsed.get("include_superseded")
    if not values:
        return False
    if len(values) != 1 or values[0] not in ("0", "1"):
        return None
    return values[0] == "1"


def _render_fact(fact: Fact) -> dict[str, object]:
    """One fact as JSON. Enum by name, tuple to list, UUID to str.

    The embedding is not here because it is not on :class:`~avid.domain.memory.Fact` at all
    (§8.5 keeps the vector on the index side) — so the field that would turn an audit into a
    1,536-byte-per-row dump nobody reads is excluded by the domain's own shape rather than by a
    filter someone has to remember.
    """
    rendered: dict[str, object] = asdict(fact)
    rendered["kind"] = fact.kind.name if isinstance(fact.kind, Enum) else fact.kind
    rendered["derived_from"] = list(fact.derived_from)
    rendered["source_correlation_id"] = (
        str(fact.source_correlation_id) if fact.source_correlation_id else None
    )
    return rendered


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
        self,
        *,
        bind: str,
        port: int,
        behavior: BehaviorTools | None = None,
        metrics: MetricsSource | None = None,
        state: StateSource | None = None,
        tap: EventTap | None = None,
        facts: FactRepository | None = None,
        keepalive_s: float = 15.0,
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
        # §9.5's GET /metrics (#380), behind a Protocol like `behavior` above. Optional for the
        # same reason: the M0 health-only wiring still constructs, and a `None` answers 503 rather
        # than pretending an unwired registry is an empty one — which would be this endpoint's own
        # "absent is not zero" rule, broken at the door.
        self._metrics = metrics
        # §9.5's GET /state (#385) and GET /events/stream. Optional for the same reason as the two
        # above, and answering 503 rather than inventing a reading: a robot whose state source was
        # never wired is not a robot in an unknown state, it is an unwired server, and those are
        # different sentences.
        self._state = state
        self._tap = tap
        # §7.10's audit, over the same port MemoryService writes through (#386). Optional like the
        # rest, and 503 when absent — this route answers "what do you know about me?", and an empty
        # list from an unwired store is the one wrong answer it must never give.
        self._facts = facts
        # How long a quiet stream waits before writing an SSE keepalive comment. Injected rather
        # than a constant so a test can use a small one — and it is the mechanism AC-4 rests on:
        # on an idle robot, the write to a client that walked away is what finally raises.
        self._keepalive_s = keepalive_s
        self._server: asyncio.Server | None = None
        # Every in-flight stream. `stop()` cancels them, because `server.wait_closed()` waits for
        # open connections and one held-open `curl -N` would otherwise hold shutdown past
        # systemd's 5 s stop budget (SDS §9.2) — a debugging endpoint taking the robot's shutdown
        # down with it.
        self._streams: set[asyncio.Task[None]] = set()

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
        """Stop serving, end every open stream, and release the socket. Idempotent.

        ⚠️ The streams are cancelled **before** ``wait_closed()``, not after: an SSE client holds
        its connection open indefinitely by design, and ``wait_closed()`` waits for open
        connections. Closing the listener first and hoping would hang shutdown for as long as a
        `curl -N` was left running.
        """
        for task in tuple(self._streams):
            task.cancel()
        if self._streams:
            await asyncio.gather(*self._streams, return_exceptions=True)
            self._streams.clear()
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
            target = rest.partition(" ")[0]
            # ⚠️ Split once, here. Every route compares `path` to a literal, so before #386 a
            # request for `/health?x=1` was a 404 — harmless until a route needed a parameter, and
            # then it is the whole feature. `query` is passed down rather than re-parsed per route.
            path, _, query = target.partition("?")
            # The headers are read rather than discarded now: POST needs Content-Length to know
            # how much body to expect, and reading to the blank line is what lets the client's
            # write complete before we reply and close.
            length = await self._read_headers(reader)
            # ⚠️ One route does not answer-and-close. Everything else in this server writes a
            # complete response and hangs up; `/events/stream` holds the socket open for hours by
            # design, so it takes the writer instead of returning bytes — and it must be
            # dispatched here rather than inside `_route`, whose contract is "return a response".
            if method == "GET" and path == "/events/stream":
                await self._serve_stream(writer)
                return
            writer.write(await self._route(method, path, query, reader, length))
            await writer.drain()
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            _log.warning("control API request dropped: %s", exc)
        finally:
            writer.close()

    async def _route(
        self,
        method: str,
        path: str,
        query: str,
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
        if path == "/metrics":
            if method != "GET":
                return _METHOD_NOT_ALLOWED
            return self._metrics_response()
        if path == "/state":
            if method != "GET":
                return _METHOD_NOT_ALLOWED
            return self._state_response()
        if path == "/facts":
            if method != "GET":
                return _METHOD_NOT_ALLOWED
            return await self._facts_response(query)
        if path == "/quiet":
            if method != "POST":
                return _METHOD_NOT_ALLOWED
            return await self._quiet(reader, length)
        if path == "/events/stream":
            # A GET reaches `_serve_stream` before this and never arrives here; anything else is
            # the wrong method rather than an unknown path, the same distinction `/quiet` makes.
            return _METHOD_NOT_ALLOWED
        return _NOT_FOUND

    def _metrics_response(self) -> bytes:
        """``GET /metrics`` — §3.12.2's registry as JSON (#380).

        Synchronous: every provider is a cheap in-memory read, so there is nothing to await and
        adding an ``await`` would only invite someone to put a query behind one (P8).

        Never raises. ``MetricsRegistry.snapshot`` already catches a failing provider and names it
        in ``absent``; this catches serialisation as well, because a metrics endpoint that took the
        robot down would be a reliability defect living inside a reliability feature (§3.12.3).
        """
        if self._metrics is None:
            return _response("503 Service Unavailable", b"no metrics source")
        try:
            body = json.dumps(self._metrics.snapshot()).encode("utf-8")
        except (TypeError, ValueError) as exc:
            _log.warning("metrics snapshot could not be serialised: %s", exc)
            return _response("500 Internal Server Error", b"metrics unavailable")
        return _response("200 OK", body, content_type="application/json")

    def _state_response(self) -> bytes:
        """``GET /state`` — §9.5's row: the operational state, the affect, and the session (#385).

        Synchronous for the same reason ``_metrics_response`` is: every value behind
        :class:`~avid.core.ports.StateSource` is an attribute read, and adding an ``await`` would
        only invite someone to put a query behind one (P8).

        ⚠️ ``RobotState`` and ``Affect`` arrive as two independent readings and are serialised as
        two independent fields. SDS §3.10 makes them orthogonal, and a route that inferred one
        from the other would publish that error as fact — see ``core/state_report.py``.
        """
        if self._state is None:
            return _response("503 Service Unavailable", b"no state source")
        try:
            body = json.dumps(self._state.snapshot()).encode("utf-8")
        except (TypeError, ValueError) as exc:
            _log.warning("state snapshot could not be serialised: %s", exc)
            return _response("500 Internal Server Error", b"state unavailable")
        return _response("200 OK", body, content_type="application/json")

    async def _serve_stream(self, writer: asyncio.StreamWriter) -> None:
        """``GET /events/stream`` — the SSE tap (#385, SDS §9.5, §3.5.1).

        Run as a tracked task so :meth:`stop` can cancel it; the caller awaits that task, so a
        client disconnecting still unwinds through the ``finally`` here.
        """
        if self._tap is None:
            writer.write(_response("503 Service Unavailable", b"no event tap"))
            await writer.drain()
            return
        task = asyncio.current_task()
        if task is not None:
            self._streams.add(task)
        try:
            await self._pump_stream(writer, self._tap)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            # The ordinary end of a stream: the client pressed Ctrl-C. Not a warning.
            _log.debug("event stream closed by the client: %s", exc)
        except asyncio.CancelledError:
            _log.debug("event stream cancelled by shutdown")
        finally:
            if task is not None:
                self._streams.discard(task)

    async def _pump_stream(self, writer: asyncio.StreamWriter, tap: EventTap) -> None:
        """Write SSE frames until the client goes away or the server stops.

        ⚠️ The keepalive is not cosmetic. On an idle robot nothing is published for minutes, and a
        socket is only discovered to be dead when something is written to it — so without a
        periodic comment a vanished ``curl`` would sit in the tap's client set indefinitely,
        which is AC-4's leak.
        """
        # ⚠️ Attach BEFORE writing the head, not after. Between the two there is an `await`, and
        # anything published inside it would be missed by a client that has already been told the
        # stream is open — a tap with a blind spot at exactly the moment you started watching. It
        # also makes attachment observable the instant the client can read the head, which is what
        # lets the tests assert rather than poll.
        client = tap.attach()
        try:
            writer.write(_SSE_HEAD)
            await writer.drain()
            while True:
                try:
                    event = await asyncio.wait_for(
                        client.queue.get(), timeout=self._keepalive_s
                    )
                except TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                if client.dropped:
                    # Told in band, before the next event, so the gap is visible exactly where it
                    # happened. §3.5.5: silent drops are a debugging catastrophe — and this is the
                    # debugger.
                    writer.write(render_drops(client.dropped))
                    client.dropped = 0
                writer.write(render(event))
                await writer.drain()
        finally:
            tap.detach(client)

    async def _facts_response(self, query: str) -> bytes:
        """``GET /facts`` — §7.10's privacy audit, *"what do you know about me?"* (#386).

        §7.10's guarantee has two halves: ``forget`` is a hard cascading DELETE, and the user can
        **see** what is held about them. The delete half shipped at M7; this is the other one, and
        until it existed the answer required opening SQLite by hand — which is not an answer a user
        has.

        ⚠️ **Awaits, where its two neighbours are synchronous.** ``/metrics`` and ``/state`` read
        attributes; this reads a database, so it goes through the port's executor and never touches
        the loop with SQLite (P8, AC-4). A store with thousands of facts must not stall the robot
        to answer a curl.

        ⚠️ **The scope is echoed back.** *"What do you know about me"* answered with a bare list is
        an answer the reader cannot check — they cannot tell a short history from a filtered one.
        """
        if self._facts is None:
            return _response("503 Service Unavailable", b"no fact store")
        include = _include_superseded(query)
        if include is None:
            return _response(
                "400 Bad Request",
                b"include_superseded must be 0 or 1",
            )
        facts = await (self._facts.fetch_all() if include else self._facts.fetch_live())
        body = json.dumps(
            {
                "facts": [_render_fact(fact) for fact in facts],
                "count": len(facts),
                "include_superseded": include,
            }
        ).encode("utf-8")
        return _response("200 OK", body, content_type="application/json")

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
