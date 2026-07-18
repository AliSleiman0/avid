# SPK-1 — Measured Realtime cost per conversation-minute

Timeboxed spike (PMP §6.4, 1 IED) buying down **RISK-02 / O7 / M5**. Question:
what does an OpenAI Realtime session **actually cost per conversation-minute**,
measured — and does it fit **O7 (≤ $25/month)** under a plausible usage profile?

Throwaway measurement code. Lives *outside* `avid/` on purpose — it imports the
`openai` SDK, which the vendor-boundary rule forbids inside the app. The
deliverable is [`FINDINGS.md`](FINDINGS.md) + a summary posted to issue #44.

## Setup

1. `cp .env.example .env` at the repo root, paste your `OPENAI_API_KEY`.
   `.env` is gitignored; the key is never printed or committed.
2. Runs use ephemeral deps — nothing is added to `pyproject.toml`/`uv.lock`:

## Run

```bash
# Phase 1 — connectivity + usage-shape probe (fractions of a cent, text only)
uv run --with "openai>=1.40" python spikes/spk1_realtime_cost/probe.py

# Phase 2 — full audio-in/audio-out measurement (a few short conversations)
uv run --with "openai>=1.40" python spikes/spk1_realtime_cost/measure.py
```

## What it measures

- Per-**turn** `usage` from `response.done` (SDS §3.12.2), split into audio /
  text × input / cached-input / output. Per-turn (not just a session total)
  because Realtime can re-bill accumulated context audio each turn — if that's
  billed **uncached** ($10/1M) vs **cached** ($0.30/1M) the answer swings 33×.
- `$/conversation-minute` = session cost ÷ active wall-minutes.
- Monthly projection vs **O7**, at a stated usage profile, with sensitivity.

## Prices

`prices.py` — `gpt-realtime-2.1-mini` family, price sheet fetched 2026-07-18
(provenance in the module docstring). Tokens are ground truth
from the API; only the token→$ conversion depends on this sheet.
