"""SPK-1 Phase 2 — MEASURED cost per conversation-minute (audio-in/audio-out).

Drives a representative multi-turn conversation against the Realtime API and
records the *billed* per-turn ``response.done.usage`` (real field shapes verified
by probe.py). Answers the decisive question the priced estimate could not:
does accumulated context audio re-bill each turn, and cached ($0.30/1M) or
uncached ($10/1M)?

User speech is synthesized once via TTS and cached as raw PCM16/24k under
audio_cache/ so re-runs are deterministic and cheap. Server VAD is off — turns
are committed manually for measurement determinism.

Run:  uv run --with "openai[realtime]>=2" python -u spikes/spk1_realtime_cost/measure.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

from _env import load_api_key
from prices import MODEL, O7_MONTHLY_TARGET_USD, TokenBreakdown, cost_usd

SAMPLE_RATE = 24_000
BYTES_PER_SAMPLE = 2  # PCM16 mono
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"
AUDIO_FMT = {"type": "audio/pcm", "rate": SAMPLE_RATE}
CACHE = Path(__file__).parent / "audio_cache"

# A representative short conversation — a handful of turns so context accumulates
# and any per-turn re-billing becomes visible.
TURNS = [
    "Hey Pico, good morning. How are you doing today?",
    "That's nice. Can you remind me what I said I'd do this afternoon?",
    "Right, the dentist. What time was the appointment again?",
    "Thanks. And could you tell me a quick joke before I head out?",
    "Ha, that's terrible. Okay, talk to you later, bye.",
]


def synth_pcm(client, text: str, idx: int) -> bytes:
    """Synthesize one user utterance to PCM16/24k mono, cached on disk."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"turn_{idx}.pcm"
    if path.exists():
        return path.read_bytes()
    resp = client.audio.speech.create(
        model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="pcm"
    )
    data = resp.read()
    path.write_bytes(data)
    return data


def pcm_seconds(data: bytes) -> float:
    return len(data) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def breakdown_from_usage(u: dict) -> TokenBreakdown:
    itd = u.get("input_token_details", {}) or {}
    ctd = itd.get("cached_tokens_details", {}) or {}
    otd = u.get("output_token_details", {}) or {}
    audio_in = itd.get("audio_tokens", 0) or 0
    audio_in_cached = ctd.get("audio_tokens", 0) or 0
    text_in = itd.get("text_tokens", 0) or 0
    text_in_cached = ctd.get("text_tokens", 0) or 0
    return TokenBreakdown(
        audio_input=audio_in - audio_in_cached,
        audio_input_cached=audio_in_cached,
        audio_output=otd.get("audio_tokens", 0) or 0,
        text_input=text_in - text_in_cached,
        text_input_cached=text_in_cached,
        text_output=otd.get("text_tokens", 0) or 0,
    )


async def run_turn(conn, pcm: bytes) -> dict:
    """Feed one user utterance, get a response, return its raw usage dict."""
    b64 = base64.b64encode(pcm).decode("ascii")
    for i in range(0, len(b64), 32_000):
        await conn.input_audio_buffer.append(audio=b64[i : i + 32_000])
    await conn.input_audio_buffer.commit()
    await conn.response.create()
    async for event in conn:
        if event.type == "response.done":
            return event.response.usage.model_dump()
        if event.type == "error":
            raise RuntimeError(json.dumps(event.model_dump(), default=str))
    raise RuntimeError("stream ended before response.done")


async def main() -> int:
    import openai

    key = load_api_key()
    sync_client = openai.OpenAI(api_key=key)
    print(f"synthesizing {len(TURNS)} user utterances (cached in audio_cache/)...")
    pcms = [synth_pcm(sync_client, t, i) for i, t in enumerate(TURNS)]
    user_secs = sum(pcm_seconds(p) for p in pcms)

    client = openai.AsyncOpenAI(api_key=key)
    session_total = TokenBreakdown()
    per_turn: list[tuple[TokenBreakdown, float]] = []
    resolved_model = MODEL
    t0 = time.monotonic()
    async with client.realtime.connect(model=MODEL) as conn:
        await conn.session.update(
            session={
                "type": "realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {"format": AUDIO_FMT, "turn_detection": None},
                    "output": {"format": AUDIO_FMT, "voice": "cedar"},
                },
            }
        )
        for idx, pcm in enumerate(pcms):
            usage = await run_turn(conn, pcm)
            b = breakdown_from_usage(usage)
            asst_secs = b.audio_output * 0.05  # ~1 token / 50 ms
            per_turn.append((b, asst_secs))
            session_total = session_total + b
            print(
                f"  turn {idx}: audio_in={b.audio_input} "
                f"cached={b.audio_input_cached} audio_out={b.audio_output} "
                f"text_in={b.text_input} text_out={b.text_output} "
                f"-> ${cost_usd(b):.5f}"
            )
    wall_s = time.monotonic() - t0

    asst_secs = sum(s for _, s in per_turn)
    convo_secs = user_secs + asst_secs
    total_cost = cost_usd(session_total)
    per_min = total_cost / (convo_secs / 60) if convo_secs else 0.0

    print("\n=== per-turn audio-input growth (context re-bill check) ===")
    prev = None
    for i, (b, _) in enumerate(per_turn):
        delta = "" if prev is None else f"  (+{b.audio_input - prev})"
        cached_note = " CACHED" if b.audio_input_cached else ""
        print(
            f"  turn {i}: uncached_audio_in={b.audio_input}{delta}"
            f"  cached_audio_in={b.audio_input_cached}{cached_note}"
        )
        prev = b.audio_input

    print("\n=== MEASURED ===")
    print(f"model:                 {resolved_model}")
    print(f"turns:                 {len(TURNS)}")
    print(f"user speech:           {user_secs:.1f}s")
    print(f"assistant speech:      {asst_secs:.1f}s")
    print(f"conversation audio:    {convo_secs:.1f}s")
    print(f"session wall time:     {wall_s:.1f}s")
    print(f"total billed cost:     ${total_cost:.5f}")
    print(f"$/conversation-minute: ${per_min:.5f}")
    print(f"\nprojection vs O7 (${O7_MONTHLY_TARGET_USD:.0f}/mo):")
    for mins_day in (10, 30, 60):
        monthly = per_min * mins_day * 30
        verdict = "PASS" if monthly <= O7_MONTHLY_TARGET_USD else "FAIL"
        print(f"  {mins_day} conv-min/day -> ${monthly:.2f}/mo  {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
