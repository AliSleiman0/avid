"""SPK-1 Phase 1 — connectivity + usage-shape probe (cheap, text-only).

Purpose: de-risk the API before writing the full audio measurement. It:
  1. checks whether the configured model resolves (SDS §6.10 volatility) — this
     is how we found the old pin ``gpt-realtime-2.1-mini-2026-07-06`` did NOT
     resolve (WS 4004); config now pins a real snapshot;
  2. reveals the *actual* openai-python realtime connect surface on this box;
  3. dumps the raw ``response.done.usage`` JSON so Phase 2 (measure.py) parses
     real field names, not guessed ones.

Run:  uv run --with "openai[realtime]>=2" python spikes/spk1_realtime_cost/probe.py

Cost: one short text-only turn — fractions of a cent. No audio yet.
"""

from __future__ import annotations

import asyncio
import json
import sys

from _env import load_api_key
from prices import MODEL


async def main() -> int:
    import openai

    print(f"openai-python version: {openai.__version__}")
    client = openai.AsyncOpenAI(api_key=load_api_key())

    # The realtime connect helper moved from client.beta.realtime to
    # client.realtime as the API graduated; try GA first, then beta.
    connect = None
    for path in ("realtime", "beta.realtime"):
        obj = client
        for attr in path.split("."):
            obj = getattr(obj, attr, None)
            if obj is None:
                break
        if obj is not None and hasattr(obj, "connect"):
            connect = obj.connect
            print(f"using client.{path}.connect")
            break
    if connect is None:
        print("ERROR: no realtime.connect helper on this SDK. Attrs:", dir(client))
        return 2

    seen_event_types: list[str] = []
    try:
        async with connect(model=MODEL) as conn:
            await conn.session.update(
                session={"type": "realtime", "output_modalities": ["text"]}
            )
            await conn.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Say hello in five words."}
                    ],
                }
            )
            await conn.response.create()
            async for event in conn:
                seen_event_types.append(event.type)
                if event.type == "response.done":
                    dump = event.model_dump() if hasattr(event, "model_dump") else event
                    print("\n=== response.done (raw) ===")
                    print(json.dumps(dump, indent=2, default=str))
                    break
                if event.type == "error":
                    print("\n=== error event ===")
                    print(
                        json.dumps(
                            getattr(event, "model_dump", lambda: event)(),
                            indent=2,
                            default=str,
                        )
                    )
                    return 3
    except Exception as exc:  # noqa: BLE001 — spike: surface whatever the API/SDK raises
        print(f"\nCONNECT/RUN FAILED: {type(exc).__name__}: {exc}")
        return 1

    print("\nevent types seen:", " -> ".join(seen_event_types))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
