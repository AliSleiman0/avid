# SPK-1 Findings — Measured Realtime cost per conversation-minute

> Deliverable for issue #44 (PMP §6.4). **Status: MEASURED — complete.**

- **Date:** 2026-07-18
- **Model measured:** `gpt-realtime-2.1-mini` (alias; the pinned snapshot fails — see below)
- **openai-python:** 2.46.0 · connect via `client.realtime.connect` (needs the
  `openai[realtime]` extra — pulls `websockets`).
- **Price sheet:** developers.openai.com pricing, fetched 2026-07-18 (`prices.py`).

## TL;DR

- **Measured cost/conversation-minute: $0.0212** (5-turn audio-in/audio-out, `cedar` config).
- **O7 (≤$25/mo) holds VAD-gated to ~40 conv-min/day.** 10/day → $6.35; 30/day → $19.05; 60/day → $38.10.
- **Always-on, no VAD gate ≈ $670/mo — confirms RISK-02.** The ADR-007 gate is load-bearing.
- **Context audio re-bills at the CACHED tier ($0.30/1M), not uncached ($10):** the
  "exploding tokens" super-linear blow-up does NOT occur. The 33× cliff is avoided.
- **Dominant cost = assistant audio OUTPUT** ($20/1M). In the sample the model talked
  69s vs the user's 21s (77% of airtime). Terser responses (`max_output_tokens`,
  personality) are the primary cost lever — measured $0.0212 vs $0.0156 estimate is
  entirely the verbose default.

## Findings from the connectivity probe (settled, no quota needed)

1. **The pinned model does not exist.** `config/pi.toml [ai] model =
   "gpt-realtime-2.1-mini-2026-07-06"` returns WS 4004 `model_not_found`. That
   dated snapshot never shipped. → **config fix required** (see below).
2. **Resolvable models** for this key: alias `gpt-realtime-2.1-mini`, and real
   dated snapshots `gpt-realtime-mini-2025-12-15` (latest) / `-2025-10-06`.
   The spike measures against the alias `gpt-realtime-2.1-mini`.
3. **Blocker:** account returns `insufficient_quota` — the session opens but no
   billed turn can run until credit is added. The *measured* AC needs this.

### Config fix (follow-up, separate from #44)
Repoint `config/pi.toml` to a snapshot that exists and stays pinned (SDS §6.10):
`model = "gpt-realtime-mini-2025-12-15"`. File as a small `bug`/`chore`; do not
fold into this spike.

## Measured run (`measure.py`)

5-turn audio-in/audio-out conversation, server VAD off (manual commit), user
speech TTS-synthesized (`gpt-4o-mini-tts`, cached). Per-turn billed usage:

| Turn | uncached audio_in | cached audio_in | audio_out | text_in | text_out | turn $ |
|---|--:|--:|--:|--:|--:|--:|
| 0 | 32 | 0   | 238 | 126 | 85  | 0.00536 |
| 1 | 70 | 0   | 382 | 62  | 132 | 0.00870 |
| 2 | 42 | 64  | 298 | 84  | 110 | 0.00673 |
| 3 | 23 | 128 | 276 | 96  | 96  | 0.00609 |
| 4 | 80 | 128 | 179 | 103 | 76  | 0.00468 |

- user speech 20.8s · assistant speech 68.7s · conversation audio 89.5s
- total billed **$0.03156** → **$0.02117 / conversation-minute**

### Context re-bill: CACHED (the key result)
From turn 2 on, accumulated context appears as `cached audio_in` (64→128→128)
billed at **$0.30/1M**, while uncached audio_in stays ~flat (just the current
utterance). The super-linear "exploding tokens" failure mode **does not occur** —
cost grows ~linearly with turns. The 33× cliff is avoided.

### Cost is dominated by assistant audio OUTPUT
audio_out ($20/1M) is ~85–90% of each turn's cost. The model spoke 69s vs the
user's 21s. Terser responses (`[ai] max_output_tokens`, personality) scale cost
down almost linearly — the primary lever.

## Projection vs O7 (measured $0.0212/conv-min)

| Scenario | $/month | vs O7 ($25) |
|---|--:|:--:|
| VAD-gated, 10 conv-min/day | 6.35 | **PASS** |
| VAD-gated, 30 conv-min/day | 19.05 | **PASS** |
| VAD-gated, 60 conv-min/day | 38.10 | FAIL |
| Always-on, no gate (est.) | ~670 | **FAIL — confirms RISK-02** |

O7 holds VAD-gated to ~**40 conv-min/day** at the current verbose default; higher
with tightened output.

## Recommendation (for M5)

- **Ship ADR-007's VAD gate — not optional.** Always-on is ~27× over O7. The gate
  is what makes Pico affordable; RISK-02 confirmed.
- **No architectural rewrite needed.** Context re-billing is cached, so long
  sessions don't blow up. Fitting O7 is a matter of two knobs already in config:
  `[ai] max_output_tokens` (curb the dominant output-audio cost) and
  `[gate] session_idle_close_s` (bound always-open time). Design both into M5
  (SDS §6.2.3).
- **Cost meter (SDS §3.12.2):** read `response.done.usage`; split
  `input_token_details.cached_tokens_details.audio_tokens` (cheap) from uncached
  so the dashboard reflects real spend.

## Config fix (bundled in this PR)
The pinned snapshot `gpt-realtime-2.1-mini-2026-07-06` did not exist (4004). This
PR repoints it to a real dated snapshot **`gpt-realtime-mini-2025-12-15`** across
`avid/core/config.py`, `config/pi.toml`, `config/sim.toml`, and the docs. At M1
`realtime="replay"` so the string isn't dialed yet — M5 confirms the exact
snapshot when the real `RealtimeClient` adapter lands.
