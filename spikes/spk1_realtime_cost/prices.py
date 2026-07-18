"""Price sheet for the pinned Realtime snapshot, and a token->USD cost function.

MEASUREMENT NOTE: the *tokens* are ground truth, read from the API's own
``response.done.usage`` payload (SDS §3.12.2). Only the token->dollar
conversion below is a published-price lookup, so the spike's finding is only as
current as this table. Provenance is recorded so it can be re-checked when the
Realtime family churns (SDS §6.10 volatility warning).

Source: OpenAI API pricing page (developers.openai.com/api/docs/pricing),
fetched 2026-07-18, "Realtime and audio generation models" table.
"""

from __future__ import annotations

from dataclasses import dataclass

# config USED to pin this — it did NOT resolve (WS 4004 model_not_found,
# 2026-07-18); the date was fictional, no such snapshot shipped. This PR repoints
# config to gpt-realtime-mini-2025-12-15 (a real dated snapshot). Kept here as the
# record of what the spike found.
PINNED_IN_CONFIG_BEFORE_FIX = "gpt-realtime-2.1-mini-2026-07-06"

# What the spike actually calls: the resolvable family alias (matches this price
# sheet). For prod, pin a real dated snapshot, e.g. gpt-realtime-mini-2025-12-15.
MODEL = "gpt-realtime-2.1-mini"

# USD per 1,000,000 tokens. Keyed by modality and tier.
PRICES_PER_1M: dict[str, float] = {
    "audio_input": 10.00,
    "audio_input_cached": 0.30,  # 33x cheaper than uncached — the hinge (see FINDINGS)
    "audio_output": 20.00,
    "text_input": 0.60,
    "text_input_cached": 0.06,
    "text_output": 2.40,
}

# Flagship, for the "why mini" comparison line in the findings.
FLAGSHIP_AUDIO_PER_1M = {"input": 32.00, "cached_input": 0.40, "output": 64.00}

O7_MONTHLY_TARGET_USD = (
    25.0  # PMP O7 — "affordable operation", the bar we project against
)


@dataclass(frozen=True, slots=True)
class TokenBreakdown:
    """Billed tokens for one turn or a whole session, split by tier."""

    audio_input: int = 0
    audio_input_cached: int = 0
    audio_output: int = 0
    text_input: int = 0
    text_input_cached: int = 0
    text_output: int = 0

    def __add__(self, other: TokenBreakdown) -> TokenBreakdown:
        return TokenBreakdown(
            audio_input=self.audio_input + other.audio_input,
            audio_input_cached=self.audio_input_cached + other.audio_input_cached,
            audio_output=self.audio_output + other.audio_output,
            text_input=self.text_input + other.text_input,
            text_input_cached=self.text_input_cached + other.text_input_cached,
            text_output=self.text_output + other.text_output,
        )


def cost_usd(tokens: TokenBreakdown) -> float:
    """Convert a token breakdown to USD via PRICES_PER_1M."""
    return (
        tokens.audio_input * PRICES_PER_1M["audio_input"]
        + tokens.audio_input_cached * PRICES_PER_1M["audio_input_cached"]
        + tokens.audio_output * PRICES_PER_1M["audio_output"]
        + tokens.text_input * PRICES_PER_1M["text_input"]
        + tokens.text_input_cached * PRICES_PER_1M["text_input_cached"]
        + tokens.text_output * PRICES_PER_1M["text_output"]
    ) / 1_000_000
