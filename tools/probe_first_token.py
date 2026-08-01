"""Measure model time-to-first-token across the Realtime family, to settle #106 AC-4.

O1 (SDS §2.8.1) budgets **P50 ≤ 800 ms** speech-end → first audio. Two bench runs missed it at
1773 / 1943 ms, and #191's on-wire instrumentation found where it goes: `response.created` lands
6–18 ms after the server's own speech-stop, and then **612–812 ms** passes before the first byte
of reply audio. That gap is the model generating. None of it is ours, and no knob in
`config/pi.toml` touches it.

Before amending a normative budget, the obvious question is *"did you try the other model?"* —
and right now the answer is no. This answers it.

**Why a probe rather than another bench run.** The number that decides AC-4 is
`response.created → first output_audio.delta`, which needs no microphone, no speaker, no VAD and
no human. So instead of asking an operator to hold four more conversations, this drives the same
generation path over text and reports a distribution per model. Repeatable, comparable, and
finished in a couple of minutes. It is the same discipline #178's frame probe paid for: ten
minutes with a probe beats an afternoon of guessing.

⚠️ **What this does and does not measure.** It measures generation start latency. It does *not*
include the server's `silence_duration_ms` commit delay (a configured constant, identical for
every model) or the network leg carrying our mic audio up. So the figures here are the
**model-attributable** part of O1 and are directly comparable *between* models — which is the
question — but they are a floor, not a prediction of O1 itself. An audio-committed turn also runs
input transcription, which is asynchronous and does not gate the response.

    uv run --frozen python tools/probe_first_token.py                    # the default four
    uv run --frozen python tools/probe_first_token.py --trials 8
    uv run --frozen python tools/probe_first_token.py --models gpt-realtime-2.1

Needs `OPENAI_API_KEY` and the `openai` extra. The key is read once from the environment and
never logged, echoed or written (SECURITY.md). `max_output_tokens` is pinned low: TTFT is
unaffected by the cap, and there is no reason to pay for replies nobody hears.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import statistics
import sys
import time
from typing import Any

_URL = "wss://api.openai.com/v1/realtime"
_NS_PER_MS = 1_000_000

# The models worth comparing. The pinned production mini first, so every other row reads as a
# delta from the status quo; then the dated flagship snapshot (the "did you try the big one?"
# answer), then the newest of each family, because a newer mini beating an older flagship is the
# outcome that would actually change the decision.
_DEFAULT_MODELS = (
    "gpt-realtime-mini-2025-12-15",
    "gpt-realtime-2025-08-28",
    "gpt-realtime-2.1",
    "gpt-realtime-2.1-mini",
)

# Short, varied prompts. Varied because a repeated identical prompt invites caching to flatter the
# second trial onward, and TTFT flattered by a cache is not the number a live conversation sees.
_PROMPTS = (
    "Say hello.",
    "What is two plus two?",
    "Name a colour.",
    "Count to three.",
    "Say goodbye.",
    "What day comes after Monday?",
    "Name a fruit.",
    "Say the word yes.",
)

_MAX_OUTPUT_TOKENS = 64


async def _measure_model(model: str, *, trials: int, key: str) -> list[float]:
    """Open one session against *model* and return its ``created → first delta`` samples, in ms.

    One session for all trials, deliberately: that is how the robot runs (§6.2.3 keeps the session
    open between turns), so a per-trial reconnect would measure connect cost we already know about
    from #157 and which AC-4 pays once per conversation, not once per turn.
    """
    import websockets

    samples: list[float] = []
    async with await websockets.connect(
        f"{_URL}?model={model}",
        additional_headers={"Authorization": f"Bearer {key}"},
        ssl=ssl.create_default_context(),
        open_timeout=30,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["audio"],
                        "audio": {
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "voice": "cedar",
                            }
                        },
                        "instructions": "Answer in one short sentence.",
                        "max_output_tokens": _MAX_OUTPUT_TOKENS,
                    },
                }
            )
        )

        for trial in range(trials):
            prompt = _PROMPTS[trial % len(_PROMPTS)]
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prompt}],
                        },
                    }
                )
            )
            await ws.send(json.dumps({"type": "response.create"}))

            created_ns: int | None = None
            async for raw in ws:
                msg: dict[str, Any] = json.loads(raw)
                kind = str(msg.get("type", ""))
                if kind == "response.created":
                    created_ns = time.monotonic_ns()
                elif kind == "response.output_audio.delta" and created_ns is not None:
                    samples.append((time.monotonic_ns() - created_ns) / _NS_PER_MS)
                    break
                elif kind == "error":
                    err = msg.get("error") or {}
                    print(f"    !! {err.get('code')!r}: {err.get('message')!r}")
                    break
                elif kind == "response.done" and created_ns is not None:
                    # Generation finished without ever producing audio — a text-only reply or a
                    # refusal. Recording it as a TTFT sample would be a lie about a different
                    # quantity, so the trial is dropped and said out loud.
                    print("    (no audio in this reply — trial dropped)")
                    break
            # Drain to response.done before the next trial: a second response.create while one is
            # still generating is rejected outright (proven by probe_overlap.py), which would cost
            # a trial and leave the session confused about what is in flight.
            async for raw in ws:
                if str(json.loads(raw).get("type", "")) == "response.done":
                    break
    return samples


async def _run(models: tuple[str, ...], trials: int) -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("FAIL: OPENAI_API_KEY is not set")
        return 1
    try:
        import websockets  # noqa: F401 - probing availability before the first connect
    except ModuleNotFoundError:
        print("FAIL: websockets missing — `uv sync --frozen --extra openai`")
        return 1

    print(
        f"model time-to-first-token: response.created -> first audio delta, {trials} trials"
    )
    print(
        "(the model-attributable part of O1; excludes the commit delay and the mic uplink)\n"
    )
    print(f"{'model':<32} {'min':>8} {'median':>8} {'max':>8}   n")
    print("-" * 70)

    results: dict[str, list[float]] = {}
    for model in models:
        try:
            samples = await _measure_model(model, trials=trials, key=key)
        except Exception as exc:  # noqa: BLE001 - one bad model must not lose the other rows
            print(f"{model:<32}  unavailable: {type(exc).__name__}: {exc}")
            continue
        results[model] = samples
        if not samples:
            print(f"{model:<32}  no usable samples")
            continue
        print(
            f"{model:<32} {min(samples):>7.0f}ms {statistics.median(samples):>7.0f}ms "
            f"{max(samples):>7.0f}ms  {len(samples):>2}"
        )

    print("\nAgainst O1's 800 ms P50 budget: this is what remains AFTER the configured")
    print("[ai.turn_detection] silence_duration_ms (500 ms today) has already elapsed.")
    usable = {m: s for m, s in results.items() if s}
    if usable:
        best = min(usable, key=lambda m: statistics.median(usable[m]))
        print(f"Lowest median: {best} at {statistics.median(usable[best]):.0f} ms.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=list(_DEFAULT_MODELS))
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()
    return asyncio.run(_run(tuple(args.models), args.trials))


if __name__ == "__main__":
    sys.exit(main())
