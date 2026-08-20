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
import json
import os
import socket
import ssl
import statistics
import sys
import time
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOST = "api.openai.com"
_PORT = 443
_NS_PER_MS = 1_000_000

# Matches avid/adapters/realtime.py's endpoint, so the upgrade this probe times is the same one
# the adapter performs.
_REALTIME_URL = "wss://api.openai.com/v1/realtime"
# ⚠️ The model used to be a hand-synced literal here (`gpt-realtime-mini-2025-12-15`, "a cheap
# model"), which is the drift CLAUDE.md §7.1 exists to stop: `config/pi.toml` has pinned the
# FLAGSHIP since d1302fc (2026-08-01), so from that date every run of this probe silently measured
# a model the robot does not use. It now reads `[ai] model` from the same config the M6-prefix arm
# already loads, `--model` overrides it for a single-variable arm, and the value is printed in the
# header — because a diagnostic that cannot say what it measured is the instrument bug this
# project keeps paying for (see #373).
#
# ⚠️ Numbers recorded on #106 predate this change and were taken on the MINI. They are not
# comparable to a default run today without saying so.


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


async def _time_upgrade(
    api_key: str, context: ssl.SSLContext, *, model: str, instructions: str = ""
) -> dict[str, int]:
    """One authenticated pass, split four ways (AVID-157's spike).

    This is the leg the adapter cannot avoid and the key-free phases cannot see. Subtracting the
    transport numbers above from it leaves the API's own handshake cost.

    ⚠️ **``send`` was never the interesting number and used to be the only one here.** It times a
    local socket write and is always ~0 — it says nothing about whether the API is slow. The two
    phases that answer #157's actual question are the ones that *wait for the far end*:

    * ``created`` — upgrade complete → the server's own ``session.created`` frame. This is the
      **server's session bootstrap**, and #157 named it as one of the two candidates for the
      ~1.3–1.8 s that transport does not explain.
    * ``updated`` — our ``session.update`` sent → the server's ``session.updated`` acknowledgement.
      This is where payload size, if it matters at all, would show up.

    ``instructions`` exists to test #157's own hypothesis — *"whether ``session.update``'s size
    matters (instructions + tools + memory block)"* — which M6 makes urgent rather than academic,
    because layer 2 adds ~250 tokens to every session's prefix.
    """
    import websockets  # optional `openai` extra, exactly like the adapter's lazy import

    phases: dict[str, int] = {}
    headers = {"Authorization": f"Bearer {api_key}"}

    started = time.monotonic_ns()
    connection = await websockets.connect(
        f"{_REALTIME_URL}?model={model}", additional_headers=headers, ssl=context
    )
    phases["upgrade"] = time.monotonic_ns() - started

    started = time.monotonic_ns()
    await _recv_until(connection, "session.created")
    phases["created"] = time.monotonic_ns() - started

    payload = json.dumps(
        {
            "type": "session.update",
            "session": {"type": "realtime", "instructions": instructions},
        }
    )
    started = time.monotonic_ns()
    await connection.send(payload)
    phases["send"] = time.monotonic_ns() - started

    started = time.monotonic_ns()
    await _recv_until(connection, "session.updated")
    phases["updated"] = time.monotonic_ns() - started

    await connection.close()
    return phases


async def _recv_until(connection: object, kind: str) -> None:
    """Read frames until one of type *kind* arrives, or the socket ends.

    Bounded by the socket rather than a timer on purpose: a probe that gave up early would report
    a fast bootstrap for a session that never started, which is the direction a latency
    measurement must not fail in."""
    while True:
        raw = await connection.recv()  # type: ignore[attr-defined]
        if json.loads(raw).get("type") == kind:
            return


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
    # The cp1252 console mangles this script's em dashes into "?" today and would CRASH on a "⚠️".
    # Same fix as tools/probe_tool_call_rate.py; see docs/handoff.md's standing gotcha.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument(
        "--config",
        default="config/pi.toml",
        help="profile supplying the identity layer for the M6-prefix arm",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also time the WebSocket upgrade (needs OPENAI_API_KEY + the openai extra)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model to open against (default: the --config file's [ai] model)",
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

    # Two arms, so #157's "does session.update's size matter" stops being a hypothesis. The M6 arm
    # is the REAL shipped prefix — layers 1-3 through the shipped composer — because a made-up
    # string of roughly the right length would measure the wrong thing if the API charges for
    # anything other than raw bytes.
    from avid.core.config import PersonalityConfig, load_config
    from avid.core.personality import compose, compose_instructions
    from avid.services.tools import CAPABILITY_INSTRUCTIONS

    config = load_config(args.config)
    model = args.model or config.ai.model
    print(
        f"model {model} (from {args.config})"
        if not args.model
        else f"model {model} (--model override; {args.config} says {config.ai.model})"
    )
    personality_path = _REPO_ROOT / "config" / "personality" / "default.toml"
    with personality_path.open("rb") as handle:
        personality = PersonalityConfig.model_validate(tomllib.load(handle))
    m6_prefix = compose_instructions(
        identity=config.ai.instructions,
        personality=compose(personality),
        capabilities=CAPABILITY_INSTRUCTIONS,
    )

    for label, instructions in (
        ("empty instructions", ""),
        (f"M6 prefix ({len(m6_prefix)} chars)", m6_prefix),
    ):
        upgrades = []
        for _ in range(args.iterations):
            try:
                upgrades.append(
                    await _time_upgrade(
                        api_key, context, model=model, instructions=instructions
                    )
                )
            except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
                print(f"\nupgrade failed: {type(exc).__name__}: {exc}")
                return 1
        _report(f"websocket upgrade — {label}", upgrades)

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
