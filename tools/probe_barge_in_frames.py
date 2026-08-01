#!/usr/bin/env python
"""Ask the Realtime API *which* barge-in client event it rejects, and print its exact words (#178).

Every barge-in on the Pi killed the session: 10 sessions in 80 s of bench, each announcing "one
sec, I lost my connection". #178's Stage 1 established that the outage was **ours** — we mapped an
``error`` frame to ``SessionClosed`` and tore down a working socket — but it did not establish
*why the API complained in the first place*, because nothing in the adapter logged an inbound
frame. This script closes that gap.

**It provokes each candidate deliberately and prints the raw error frame**, rather than waiting for
one to happen at the bench:

1. ``response.cancel`` with no response in flight — the leading hypothesis. Generation outruns
   playback, so a user interrupting a *buffered* reply cancels something already finished.
2. ``conversation.item.truncate`` on an item id the session has never seen.
3. ``conversation.item.truncate`` past the end of a real item's audio. ⚠️ Our ``audio_end_ms`` can
   only *undershoot* what the server sent (it is ms the device **accepted**, and since AVID-174 it
   excludes ms a ``stop()`` discarded), so this is the least likely — it is here to be **ruled
   out**, and to show what an out-of-range complaint looks like if one ever appears.
4. The real §6.2.4 sequence: let a response start, then ``truncate`` mid-audio and ``cancel``.

**Why this and not another bench day.** It needs no microphone, no speaker, no ``AudioService``,
no VAD and no human — it runs over SSH in seconds. The Pi is the scarce resource and the bench
protocol is expensive; learning one error string should not cost a session at the rig.

    uv run --frozen python tools/probe_barge_in_frames.py

Needs ``OPENAI_API_KEY`` in the environment and the ``openai`` extra (``websockets`` is not in the
default venv). Reads the key once, never logs, echoes or writes it (SECURITY.md). Costs a few
hundred tokens of the cheapest model: one short response, no audio in, nothing played.

⚠️ **Whatever this prints is the input to #178's Stage 2, and Stage 2 must not be written without
it.** The hypotheses above are ranked, not known.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
from typing import Any

_URL = "wss://api.openai.com/v1/realtime"
_DEFAULT_MODEL = "gpt-realtime-mini-2025-12-15"

# How long to wait for the API to answer a provocation before calling it silence. Errors come back
# fast; the timeout only bounds the "no complaint at all" case, which is itself a finding.
_REPLY_TIMEOUT_S = 4.0


def _session_update() -> dict[str, Any]:
    """The GA-shape session config, trimmed to what a text-only probe needs (§6.2.2).

    Deliberately **not** imported from ``OpenAIRealtimeClient._session_config``: this script must
    stay runnable against a hand-edited payload when the next API change lands, which is exactly
    when the adapter's own shape is the thing under suspicion."""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": "cedar",
                },
            },
            "instructions": "Reply with one short sentence.",
        },
    }


class _Probe:
    """One WebSocket session, with a reader task that prints every ``error`` frame it sees."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._seq = 0
        self.errors: list[dict[str, Any]] = []
        self.seen_types: list[str] = []
        self._item_id: str | None = None
        self._response_active = False

    async def send(self, payload: dict[str, Any]) -> str:
        """Send one client event, stamped so the API's error can name it (the #178 mechanism)."""
        self._seq += 1
        kind = str(payload["type"]).replace(".", "_")
        event_id = f"probe_{self._seq}_{kind}"
        await self._ws.send(json.dumps({"event_id": event_id, **payload}))
        return event_id

    async def drain(self, seconds: float = _REPLY_TIMEOUT_S) -> None:
        """Read frames for *seconds*, recording errors and tracking response/item state."""
        try:
            async with asyncio.timeout(seconds):
                async for raw in self._ws:
                    msg = json.loads(raw)
                    kind = str(msg.get("type", ""))
                    self.seen_types.append(kind)
                    if kind == "error":
                        self.errors.append(msg)
                        _print_error(msg)
                    elif kind == "response.created":
                        self._response_active = True
                    elif kind == "response.done":
                        self._response_active = False
                    elif kind == "response.output_audio.delta":
                        self._item_id = str(msg.get("item_id", "")) or self._item_id
        except TimeoutError:
            pass

    @property
    def item_id(self) -> str | None:
        return self._item_id

    @property
    def response_active(self) -> bool:
        return self._response_active


def _print_error(msg: dict[str, Any]) -> None:
    """Print the whole error object — this is a diagnostic, and the point is the vendor's words.

    Unlike the adapter's log line (an allow-list, SECURITY.md), the probe sends no user content
    and no memory, so there is nothing here to leak: everything it can echo, this script wrote."""
    error = msg.get("error") or {}
    print("    ┌─ error frame")
    for key in ("type", "code", "event_id", "param", "message"):
        if key in error:
            print(f"    │  {key:9} {error[key]!r}")
    extra = {
        k: v
        for k, v in error.items()
        if k not in {"type", "code", "event_id", "param", "message"}
    }
    if extra:
        print(f"    │  (other)  {extra!r}")
    print("    └─")


async def _case(probe: _Probe, name: str, payloads: list[dict[str, Any]]) -> None:
    before = len(probe.errors)
    print(f"\n>>> {name}")
    for payload in payloads:
        event_id = await probe.send(payload)
        print(f"    sent {payload['type']}  (event_id={event_id})")
    await probe.drain()
    if len(probe.errors) == before:
        print("    no error — the API accepted it")


async def _run(*, model: str) -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("FAIL: OPENAI_API_KEY is not set (needed to open a session)")
        return 1
    try:
        import websockets
    except ModuleNotFoundError:
        print("FAIL: websockets missing — `uv sync --frozen --extra openai`")
        return 1

    ctx = ssl.create_default_context()
    async with await websockets.connect(
        f"{_URL}?model={model}",
        additional_headers={"Authorization": f"Bearer {key}"},
        ssl=ctx,
    ) as ws:
        probe = _Probe(ws)
        await probe.send(_session_update())
        await probe.drain(1.5)

        await _case(
            probe,
            "1. response.cancel with NO response in flight  (leading hypothesis)",
            [{"type": "response.cancel"}],
        )

        await _case(
            probe,
            "2. conversation.item.truncate on an UNKNOWN item",
            [
                {
                    "type": "conversation.item.truncate",
                    "item_id": "item_does_not_exist",
                    "content_index": 0,
                    "audio_end_ms": 100,
                }
            ],
        )

        # Provoke a real response so cases 3 and 4 have a live item to act on.
        print("\n>>> asking for a short reply, to get a real item to truncate")
        await probe.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Count slowly from one to twenty.",
                        }
                    ],
                },
            }
        )
        await probe.send({"type": "response.create"})
        await probe.drain(3.0)
        item = probe.item_id
        print(f"    item_id={item!r}  response_active={probe.response_active}")

        if item is None:
            print(
                "    ⚠️  no audio item arrived — cases 3 and 4 cannot run; report this"
            )
        else:
            await _case(
                probe,
                "3. truncate PAST the end of the item's audio  (expected to be ruled out)",
                [
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item,
                        "content_index": 0,
                        "audio_end_ms": 10_000_000,
                    }
                ],
            )
            await _case(
                probe,
                "4. the real §6.2.4 sequence: truncate mid-audio, then cancel",
                [
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item,
                        "content_index": 0,
                        "audio_end_ms": 200,
                    },
                    {"type": "response.cancel"},
                ],
            )

        print(f"\n=== {len(probe.errors)} error frame(s) in this session ===")
        if not probe.errors:
            print(
                "None. The rejection is NOT reproducible from these four provocations —"
            )
            print(
                "report that on #178; it means the cause is elsewhere and the bench run stands."
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_barge_in_frames",
        description="Provoke each barge-in client event and print the API's error frame (#178).",
    )
    parser.add_argument(
        "--model", default=_DEFAULT_MODEL, help=f"default: {_DEFAULT_MODEL}"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(model=args.model))


if __name__ == "__main__":
    sys.exit(main())
