"""Capture the response *lifecycle* frames, to settle #182 from evidence rather than a hunch.

#178's guard tracks one response at a time (`_active_response`, a single slot). The bench shows
that is wrong: `conversation_already_has_active_response` proves two responses can be in play,
and `response_cancel_not_active` proves the slot disagrees with the server about whether one is
live. Widening the slot to a set is the obvious guess — and #178 demonstrated that ten minutes
with a probe beats guessing, so this answers the three questions first:

1. **Does `response.done` carry the response id?** If it does, a keyed set is trivially correct.
   If it does not, the client cannot tell *which* response ended and the design is different.
2. **Is a second `response.create` ever accepted, or always rejected?** That decides whether
   overlap is a state we must model or an error we must avoid provoking.
3. **What is in flight when a cancel is rejected?** i.e. does the server consider a response
   over at `response.done`, or at some earlier frame we are not watching.

Prints one line per response-lifecycle frame with its id, so the sequence can simply be read.

    uv run --frozen python tools/probe_overlap.py

Needs OPENAI_API_KEY and the `openai` extra. No mic, no speaker, no Pi hardware. Reads the key
once and never logs, echoes or writes it (SECURITY.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from typing import Any

_URL = "wss://api.openai.com/v1/realtime"
_MODEL = "gpt-realtime-mini-2025-12-15"

# Every frame that could plausibly mark a response starting or ending. Deliberately wider than
# {created, done}: question 3 is whether the server considers a response finished at some frame
# we are not currently watching, and that cannot be answered by only watching the two we assumed.
_LIFECYCLE = (
    "response.created",
    "response.done",
    "response.cancelled",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_audio.done",
    "response.output_audio_transcript.done",
)


def _rid(msg: dict[str, Any]) -> str:
    """The response id a frame carries, from wherever this frame type happens to put it."""
    for path in (("response", "id"), ("response_id",)):
        node: Any = msg
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if node:
            return str(node)
    return "—"


async def _run() -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("FAIL: OPENAI_API_KEY is not set")
        return 1
    try:
        import websockets
    except ModuleNotFoundError:
        print("FAIL: websockets missing — `uv sync --frozen --extra openai`")
        return 1

    seq = 0

    async with await websockets.connect(
        f"{_URL}?model={_MODEL}",
        additional_headers={"Authorization": f"Bearer {key}"},
        ssl=ssl.create_default_context(),
    ) as ws:

        async def send(payload: dict[str, Any]) -> str:
            nonlocal seq
            seq += 1
            event_id = f"probe_{seq}_" + str(payload["type"]).replace(".", "_")
            await ws.send(json.dumps({"event_id": event_id, **payload}))
            return event_id

        async def watch(seconds: float, label: str) -> None:
            print(f"\n--- {label} (watching {seconds:.0f}s) ---")
            try:
                async with asyncio.timeout(seconds):
                    async for raw in ws:
                        msg = json.loads(raw)
                        kind = str(msg.get("type", ""))
                        if kind in _LIFECYCLE:
                            print(f"    {kind:42} id={_rid(msg)}")
                        elif kind == "error":
                            err = msg.get("error") or {}
                            print(
                                f"    !! error  code={err.get('code')!r} "
                                f"event_id={err.get('event_id')!r}"
                            )
                            print(f"       {err.get('message')!r}")
            except TimeoutError:
                pass

        await send(
            {
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
                    "instructions": "Answer in one short sentence.",
                },
            }
        )
        await watch(1.5, "session.update")

        # Q1 + Q3: one ordinary response, every lifecycle frame printed with its id.
        await send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Say hello."}],
                },
            }
        )
        await send({"type": "response.create"})
        await watch(6.0, "Q1/Q3: a single response, start to finish")

        # Q2: ask for two responses back to back, without waiting.
        await send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Count slowly from one to thirty.",
                        }
                    ],
                },
            }
        )
        await send({"type": "response.create"})
        await asyncio.sleep(0.8)  # let the first get going
        second = await send({"type": "response.create"})
        print(f"\n    [second response.create sent as {second}]")
        await watch(8.0, "Q2: a second response.create while the first is generating")

        # Q3 continued: cancel after the response has finished, to see what "finished" means.
        await send({"type": "response.cancel"})
        await watch(3.0, "Q3: cancel after the response completed")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
