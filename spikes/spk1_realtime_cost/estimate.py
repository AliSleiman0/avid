"""SPK-1 interim — PRICED estimate (no quota needed), pending the measured run.

The issue AC wants a *measured* number; the account is currently out of quota,
so this computes a defensible projection from published token<->time mappings x
the real price sheet (prices.py). The live measurement (measure.py) replaces
these once billing is enabled — its job is mainly to settle the ONE uncertainty
this estimate cannot: whether accumulated context audio is re-billed each turn,
and if so cached ($0.30/1M) or uncached ($10/1M).

Mappings (OpenAI Realtime, ~2026): user audio ~1 token / 100 ms (600 tok/min);
assistant audio ~1 token / 50 ms (1200 tok/min). These are rates the *measured*
run verifies against real usage.

Run:  uv run python spikes/spk1_realtime_cost/estimate.py
"""

from __future__ import annotations

from prices import O7_MONTHLY_TARGET_USD, TokenBreakdown, cost_usd

USER_AUDIO_TOK_PER_MIN = 600  # 1 tok / 100 ms
ASSISTANT_AUDIO_TOK_PER_MIN = 1200  # 1 tok / 50 ms


def per_active_minute(
    assistant_talk_fraction: float, context_rebill_mult: float, cached: bool
) -> TokenBreakdown:
    """Tokens for one active conversation-minute.

    - input audio: mic streams the whole minute while the session is open.
    - context_rebill_mult: extra input-audio multiplier if each turn re-bills
      accumulated context (1.0 = no re-bill; the measured run pins this).
    - cached: whether that re-billed context audio is billed at the cached tier.
    """
    base_in = USER_AUDIO_TOK_PER_MIN
    extra_in = int(base_in * (context_rebill_mult - 1.0))
    out = int(ASSISTANT_AUDIO_TOK_PER_MIN * assistant_talk_fraction)
    if cached:
        return TokenBreakdown(
            audio_input=base_in, audio_input_cached=extra_in, audio_output=out
        )
    return TokenBreakdown(audio_input=base_in + extra_in, audio_output=out)


def row(label: str, per_min_usd: float, minutes_per_month: float) -> str:
    monthly = per_min_usd * minutes_per_month
    verdict = "PASS" if monthly <= O7_MONTHLY_TARGET_USD else "FAIL"
    return f"  {label:<46} ${per_min_usd:6.4f}/min  ${monthly:8.2f}/mo  {verdict}"


def main() -> None:
    print(f"O7 target: <= ${O7_MONTHLY_TARGET_USD:.0f}/month\n")

    base = cost_usd(per_active_minute(0.4, 1.0, cached=False))
    print("Per active conversation-minute (assistant talks 40% of the minute):")
    print(f"  no context re-bill:            ${base:.4f}/min")
    reb_cached = cost_usd(per_active_minute(0.4, 5.0, cached=True))
    reb_uncached = cost_usd(per_active_minute(0.4, 5.0, cached=False))
    print(f"  5x context re-bill (cached):   ${reb_cached:.4f}/min")
    print(f"  5x context re-bill (uncached): ${reb_uncached:.4f}/min  <- the risk\n")

    print("VAD-gated monthly projection (base per-min, no re-bill):")
    for mins_day in (10, 30, 60):
        print(row(f"{mins_day} conv-min/day", base, mins_day * 30))

    print("\nAlways-on, no VAD gate (RISK-02 scenario):")
    always = cost_usd(per_active_minute(0.4, 1.0, cached=False))
    print(row("session open 24/7 (43,200 min/mo)", always, 43_200))

    print("\nUncached 5x re-bill, VAD-gated 30 min/day (worst realistic):")
    print(row("30 conv-min/day", reb_uncached, 30 * 30))


if __name__ == "__main__":
    main()
