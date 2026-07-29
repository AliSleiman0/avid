#!/usr/bin/env python
"""Time the phases of a Realtime session open, host by host (AVID-157).

The #106 bench measured ``OpenAIRealtimeClient.open()`` at **1.5-6.7 s** against SDS §6.3's
~200 ms budget, with ~1.3-1.8 s unexplained by transport. This script exists to say *where* that
time goes, and specifically whether it is **ours or the API's** — which is what decides whether
§6.3's budget is wrong or there is a defect to fix.

**Most of it needs no API key.** DNS, TCP, the TLS handshake and building an ``SSLContext`` all
work against ``api.openai.com:443`` unauthenticated; only the WebSocket *upgrade* needs a key. So
the default mode is free, runs anywhere, and — run on the laptop and on the Pi — separates
Pi-specific cost from API cost, which is the one thing the on-Pi numbers alone cannot do.

    uv run --frozen python tools/probe_realtime_open.py                  # free, no key
    uv run --frozen python tools/probe_realtime_open.py --live           # + the WS upgrade

``--live`` needs ``OPENAI_API_KEY`` in the environment and the ``openai`` extra installed
(``uv sync --frozen --extra openai`` — ``websockets`` is not in the default venv). It opens a
session, sends nothing, and closes: no audio, no response, so the cost is a rounding error.

**Cold and warm are reported separately, and that is the point.** The first iteration in a process
pays one-time costs — the CA bundle parse, DNS and TLS caches, the lazy import — and the bench's
own numbers (1494/1922/832 ms across three opens; 6652 ms on a first one) look exactly like that
pattern. "Once per process" and "once per conversation" imply very different things about §6.3.

Stdlib only in the default mode, so it runs on a bare Pi with no extras installed. It reads
``OPENAI_API_KEY`` from the environment for ``--live`` and never logs, echoes or writes it
(SECURITY.md).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import ssl
import statistics
import time

_HOST = "api.openai.com"
_PORT = 443
_NS_PER_MS = 1_000_000

# Matches avid/adapters/realtime.py's endpoint and a cheap model, so the upgrade this probe times
# is the same one the adapter performs. Kept in sync by hand — this is a diagnostic, not the app.
_REALTIME_URL = "wss://api.openai.com/v1/realtime"
_MODEL = "gpt-realtime-mini-2025-12-15"


def _ms(ns: int) -> float:
    return ns / _NS_PER_MS


def _time_transport() -> dict[str, int]:
    """One unauthenticated pass: resolve, build a context, connect, handshake.

    Deliberately separate calls rather than ``ssl.create_default_context().wrap_socket(...)`` in
    one breath, because the question is which of them is slow — on a Pi the CA-bundle parse and
    the handshake are very different costs with very different fixes.
    """
    phases: dict[str, int] = {}

    started = time.monotonic_ns()
    addr_info = socket.getaddrinfo(_HOST, _PORT, socket.AF_UNSPEC, socket.SOCK_STREAM)
    phases["dns"] = time.monotonic_ns() - started

    started = time.monotonic_ns()
    context = ssl.create_default_context()
    phases["ssl_ctx"] = time.monotonic_ns() - started

    family, socktype, proto, _canon, sockaddr = addr_info[0]
    raw = socket.socket(family, socktype, proto)
    raw.settimeout(30.0)
    try:
        started = time.monotonic_ns()
        raw.connect(sockaddr)
        phases["tcp"] = time.monotonic_ns() - started

        started = time.monotonic_ns()
        wrapped = context.wrap_socket(raw, server_hostname=_HOST)
        phases["tls"] = time.monotonic_ns() - started
        wrapped.close()
    finally:
        raw.close()
    return phases


async def _time_upgrade(api_key: str, context: ssl.SSLContext) -> dict[str, int]:
    """One authenticated pass: the WebSocket upgrade, plus a ``session.update`` and close.

    This is the leg the adapter cannot avoid and the key-free phases cannot see. Subtracting the
    transport numbers above from it leaves the API's own handshake cost.
    """
    import websockets  # optional `openai` extra, exactly like the adapter's lazy import

    phases: dict[str, int] = {}
    headers = {"Authorization": f"Bearer {api_key}"}

    started = time.monotonic_ns()
    connection = await websockets.connect(
        f"{_REALTIME_URL}?model={_MODEL}", additional_headers=headers, ssl=context
    )
    phases["upgrade"] = time.monotonic_ns() - started

    started = time.monotonic_ns()
    await connection.send('{"type":"session.update","session":{"type":"realtime"}}')
    phases["send"] = time.monotonic_ns() - started

    await connection.close()
    return phases


def _report(label: str, samples: list[dict[str, int]]) -> None:
    """Print cold (first) and warm (median of the rest) for every phase."""
    if not samples:
        return
    print(f"\n{label}  (n={len(samples)})")
    print(f"  {'phase':<10} {'cold':>10} {'warm median':>14} {'warm min':>10}")
    for phase in samples[0]:
        cold = _ms(samples[0][phase])
        rest = [_ms(s[phase]) for s in samples[1:]]
        warm_median = f"{statistics.median(rest):10.1f}" if rest else f"{'—':>10}"
        warm_min = f"{min(rest):8.1f}" if rest else f"{'—':>8}"
        print(f"  {phase:<10} {cold:9.1f}  {warm_median}    {warm_min}")
    totals = [sum(s.values()) for s in samples]
    print(f"  {'TOTAL':<10} {_ms(totals[0]):9.1f}", end="")
    if len(totals) > 1:
        print(f"  {statistics.median([_ms(t) for t in totals[1:]]):10.1f}", end="")
    print()


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also time the WebSocket upgrade (needs OPENAI_API_KEY + the openai extra)",
    )
    args = parser.parse_args()

    print(f"probing {_HOST}:{_PORT} — {args.iterations} iterations")

    transport = [_time_transport() for _ in range(args.iterations)]
    _report("transport (no key needed)", transport)

    if not args.live:
        print(
            "\nrun again with --live to add the WebSocket upgrade — that is the leg SDS §6.3's\n"
            "200 ms budget is really about, and the only one that needs a key."
        )
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("\n--live needs OPENAI_API_KEY in the environment")
        return 2

    context = ssl.create_default_context()  # built once, as the adapter now does
    upgrades = []
    for _ in range(args.iterations):
        try:
            upgrades.append(await _time_upgrade(api_key, context))
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports failures, never raises
            print(f"\nupgrade failed: {type(exc).__name__}: {exc}")
            return 1
    _report("websocket upgrade (authenticated)", upgrades)

    print(
        "\nRead it against SDS §6.3's ~200 ms:\n"
        "  * upgrade slow on BOTH hosts   -> the API's handshake; §6.3's budget is wrong\n"
        "  * upgrade slow on the Pi ONLY  -> Pi-specific, a defect worth chasing\n"
        "  * only the cold row is slow    -> once per PROCESS, not per conversation\n"
        "Record the numbers on #106 as the AC-4 settlement."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
