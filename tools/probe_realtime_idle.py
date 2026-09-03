#!/usr/bin/env python
"""What an open, silent Realtime session costs, and how long the vendor keeps it (AVID-157).

`tools/probe_realtime_open.py` settled *where* the ~1.08 s session open goes: ~660-700 ms is
OpenAI's WebSocket upgrade, on a path whose full TLS setup is under 200 ms. That leaves exactly
one lever, and SDS §11.4 names it: **a pre-warmed socket** — which reopens ADR-007, because the
gate exists to avoid holding a socket while nobody is speaking. ADR-014 proposes opening it while
a *person is present* and streaming nothing until the VAD fires. Whether that is affordable turns
on two numbers this repo has never measured, and this script measures both:

1. **Does a silent session cost anything?** A session with no audio and no response produces no
   ``response.done`` and therefore no ``usage`` — that is the hypothesis. The probe holds a
   session open, sends nothing, and records every server frame with its elapsed time. A usage
   frame, an error, or a ``rate_limits.updated`` that moves would each be a cost signal.
2. **How long does the vendor keep an idle session?** Realtime sessions are bounded server-side;
   the bound is what decides the re-open policy (and whether a warm socket is ever there when the
   person finally speaks). The probe holds until the server closes the socket or ``--max-minutes``
   elapses, and reports which one happened and the close code.

An optional second arm (``--speak-after N``) proves a *held* session still answers: after N
minutes of silence it sends one text turn and times ``response.created`` to the first audio delta.
A warm socket that has quietly gone stale would fail here rather than in a conversation.

    uv run --frozen python tools/probe_realtime_idle.py                     # hold until closed
    uv run --frozen python tools/probe_realtime_idle.py --max-minutes 5     # short sanity run
    uv run --frozen python tools/probe_realtime_idle.py --speak-after 3     # hold 3 min, then speak

Needs ``OPENAI_API_KEY`` in the environment and the ``openai`` extra (``websockets``). It reads
``[ai] model`` from ``--config`` so it measures the model the robot actually uses (see #373 for why
a literal here would be drift), never logs the key, and reports the quantities it measured rather
than a verdict — the ADR reads the table, not this script (CLAUDE.md §7.1).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from typing import Any

_REALTIME_URL = "wss://api.openai.com/v1/realtime"
_NS_PER_MS = 1_000_000
_WIRE_RATE = 24000

# Frame types that would mean a silent session is NOT free. Anything under `response.` implies
# the server generated something; `error` is self-explanatory; `rate_limits.updated` is reported
# so a reader can see whether holding a socket consumes a limit.
_COST_SIGNALS = ("response.", "error", "rate_limits.updated")


def _ms(ns: int) -> float:
    return ns / _NS_PER_MS


def _session_config(instructions: str) -> dict[str, Any]:
    """The GA session shape the adapter sends (`OpenAIRealtimeClient._session_config`), minus tools.

    `turn_detection: None` mirrors the shipped `[ai.turn_detection] type = "none"` — the server
    must not be the one deciding a turn happened during a silence test."""
    return {
        "type": "realtime",
        "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": _WIRE_RATE},
                "turn_detection": None,
            },
            "output": {"format": {"type": "audio/pcm", "rate": _WIRE_RATE}},
        },
        "max_output_tokens": 200,
    }


async def _hold(
    connection: Any,
    *,
    max_s: float,
    speak_after_s: float | None,
    started_ns: int,
    log: list[tuple[float, str, dict[str, Any]]],
) -> dict[str, Any]:
    """Read frames until the server closes, the cap elapses, or the speak arm finishes."""
    import websockets

    result: dict[str, Any] = {
        "closed_by_vendor": False,
        "close_code": None,
        "close_reason": "",
        "held_s": 0.0,
        "spoke": False,
        "response_created_ms": None,
        "first_audio_ms": None,
        "usage": None,
    }
    spoke = False
    speak_sent_ns: int | None = None
    created_ns: int | None = None
    while True:
        elapsed_s = (time.monotonic_ns() - started_ns) / 1e9
        if elapsed_s >= max_s:
            break
        if speak_after_s is not None and not spoke and elapsed_s >= speak_after_s:
            # One text turn — no audio file needed, and the reply is still spoken audio, so the
            # first-delta split is the same quantity `probe_first_token.py` measures.
            await connection.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Say the word ready."}
                            ],
                        },
                    }
                )
            )
            await connection.send(json.dumps({"type": "response.create"}))
            speak_sent_ns = time.monotonic_ns()
            spoke = True
            result["spoke"] = True
        timeout = min(30.0, max_s - elapsed_s)
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed as exc:
            result["closed_by_vendor"] = True
            result["close_code"] = exc.rcvd.code if exc.rcvd else None
            result["close_reason"] = exc.rcvd.reason if exc.rcvd else ""
            break
        frame = json.loads(raw)
        kind = str(frame.get("type", "?"))
        now_ns = time.monotonic_ns()
        log.append(((now_ns - started_ns) / 1e9, kind, frame))
        if kind == "response.created" and speak_sent_ns is not None:
            created_ns = now_ns
            result["response_created_ms"] = _ms(now_ns - speak_sent_ns)
        elif kind == "response.output_audio.delta" and created_ns is not None:
            if result["first_audio_ms"] is None:
                result["first_audio_ms"] = _ms(now_ns - created_ns)
        elif kind == "response.done":
            result["usage"] = frame.get("response", {}).get("usage")
            if spoke:
                break
    result["held_s"] = (time.monotonic_ns() - started_ns) / 1e9
    return result


async def _main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/pi.toml")
    parser.add_argument(
        "--model", default=None, help="override the --config file's [ai] model"
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=70.0,
        help="give up holding after this long (past the vendor's documented bound on purpose)",
    )
    parser.add_argument(
        "--speak-after",
        type=float,
        default=None,
        help="minutes of silence before sending one text turn (default: never speak)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("needs OPENAI_API_KEY in the environment")
        return 2

    import websockets

    from avid.core.config import load_config

    config = load_config(args.config)
    model = args.model or config.ai.model
    print(f"model {model} ({'--model override' if args.model else args.config})")
    print(f"holding for up to {args.max_minutes:.1f} min, silent", end="")
    print(f", speaking after {args.speak_after:.1f} min" if args.speak_after else "")

    context = ssl.create_default_context()
    started_ns = time.monotonic_ns()
    connection = await websockets.connect(
        f"{_REALTIME_URL}?model={model}",
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ssl=context,
    )
    print(f"upgrade      {_ms(time.monotonic_ns() - started_ns):8.1f} ms")
    await connection.send(
        json.dumps(
            {
                "type": "session.update",
                "session": _session_config(config.ai.instructions),
            }
        )
    )
    log: list[tuple[float, str, dict[str, Any]]] = []
    speak_after_s = args.speak_after * 60.0 if args.speak_after is not None else None
    try:
        result = await _hold(
            connection,
            max_s=args.max_minutes * 60.0,
            speak_after_s=speak_after_s,
            started_ns=started_ns,
            log=log,
        )
    finally:
        try:
            await connection.close()
        except Exception:  # noqa: BLE001 - closing a possibly-closed socket in a diagnostic
            pass

    silent_frames = [
        entry for entry in log if entry[0] < (speak_after_s or float("inf"))
    ]
    cost_signals = [
        (t, kind) for t, kind, _ in silent_frames if kind.startswith(_COST_SIGNALS)
    ]
    limits = [
        frame for _, kind, frame in silent_frames if kind == "rate_limits.updated"
    ]

    print("\n--- silent hold ---")
    print(f"held_s                 {result['held_s']:8.1f}")
    print(
        "closed_by_vendor       "
        + (
            f"yes after {result['held_s']:.1f} s, code {result['close_code']} "
            f"{result['close_reason']!r}"
            if result["closed_by_vendor"]
            else f"no (cap {args.max_minutes:.1f} min reached or speak arm ended it)"
        )
    )
    print(f"frames_while_silent    {len(silent_frames):8d}")
    kinds: dict[str, int] = {}
    for _, kind, _ in silent_frames:
        kinds[kind] = kinds.get(kind, 0) + 1
    for kind, count in sorted(kinds.items()):
        print(f"    {kind:<48} x{count}")
    print(
        f"cost_signals           {len(cost_signals):8d}  (response.*, error, rate_limits.updated)"
    )
    for t, kind in cost_signals[:10]:
        print(f"    {t:8.1f} s  {kind}")
    if limits:
        first, last = limits[0], limits[-1]
        print("rate_limits first      " + json.dumps(first.get("rate_limits")))
        print("rate_limits last       " + json.dumps(last.get("rate_limits")))
    print(
        "usage_while_silent     "
        + (
            "none - no response.done arrived"
            if result["usage"] is None or result["spoke"]
            else json.dumps(result["usage"])
        )
    )

    if result["spoke"]:
        print("\n--- warm speak ---")
        print(
            f"response.created       {result['response_created_ms']:8.1f} ms after response.create"
            if result["response_created_ms"] is not None
            else "response.created       never arrived"
        )
        print(
            f"first audio delta      {result['first_audio_ms']:8.1f} ms after response.created"
            if result["first_audio_ms"] is not None
            else "first audio delta      never arrived"
        )
        print("usage                  " + json.dumps(result["usage"]))

    print(
        "\nRead it against ADR-014:\n"
        "  * cost_signals == 0 and no usage while silent -> an open socket is free to hold\n"
        "  * closed_by_vendor after T s                  -> the warm window; re-open policy needs T\n"
        "  * warm speak answers                          -> a held session is a usable session"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
