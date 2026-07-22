"""The cost meter — §6.10.6's mandatory instrumentation, as a bus subscriber (#105).

The smoke detector for the one failure mode SDS §6.10.3 proves is otherwise **silent**:
broken prompt-caching looks *identical* to working caching until the invoice — $4/month
becomes $27/month (mini), $12 becomes $85 (flagship), with nothing in the logs to say so.
This service is the tripwire. It subscribes to ``conversation.turn_ended`` — exactly the
consumer SDS §9.1.3 (line 2013) already names as "Observability (cost meter, §6.10.6)", so
wiring it makes that catalogued row true — accumulates the turn's :class:`~avid.domain.TokenUsage`,
and logs a **projected monthly spend** against the O7 budget (≤$25/month, PMP §5.2) plus the
**cached-input ratio** whose collapse is the failure's only early sign.

Placement is ``services/`` — not ``adapters/`` — because it is a bus subscriber that computes
and logs, naming no device and no vendor (P1, P5); it is reactive and owns no task, exactly like
:class:`~avid.services.affect.AffectService`. **Rates and dollars live here, never in the domain**
(SDS §6.10.6, ``domain/conversation.py``): :class:`~avid.domain.TokenUsage` counts tokens only,
and this is the "adapter/observability cost meter" that turns those counts into money. The rate
table is keyed by the injected ``[ai] model`` name, so swapping the model stays a **config edit**
(the vendor boundary, CLAUDE.md §3): a different model selects a different row.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import EventBus
from avid.domain import ConversationTurnEnded, TokenUsage

_log = logging.getLogger(__name__)

# The component name stamped in this service's log lines (SDS §9.1.3). It publishes nothing —
# it is a pure consumer — so this never reaches an Event envelope; it names the meter in logs.
_SOURCE = "CostMeterService"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Rates:
    """Per-1M-token USD rates for one model (SDS §6.10.1). Cached input is ~98.75% cheaper than
    uncached — the single fact §6.4's whole cache strategy turns on (SDS §6.10.2, Fact 1)."""

    cached_input: float
    uncached_input: float
    output: float


# SDS §6.10.1 published rates, per 1M tokens. Mini is the default (§6.10.5); flagship is here so a
# model swap (a config edit) is metered too. Rates belong to the meter, not the domain (§6.10.6).
_MINI_RATES = _Rates(cached_input=0.30, uncached_input=10.00, output=20.00)
_FLAGSHIP_RATES = _Rates(cached_input=0.40, uncached_input=32.00, output=64.00)

# Keyed by the pinned dated snapshot ([ai] model). An unlisted snapshot roll still meters via the
# family heuristic below (mini vs flagship) with a warning, rather than going silent on the exact
# number the O7 gate watches — the model pin churns fast (SDS §6.10 volatility).
_RATES: dict[str, _Rates] = {
    "gpt-realtime-mini-2025-12-15": _MINI_RATES,
}

# The §6.10.3 usage model the monthly projection extrapolates from: ~20 turns/day.
_MODELED_TURNS_PER_DAY = 20
_DAYS_PER_MONTH = 30
# O7 (PMP §5.2): affordable operation is ≤ $25/month at the target usage profile.
_O7_MONTHLY_BUDGET_USD = 25.0
_TOKENS_PER_MILLION = 1_000_000


def _resolve_rates(model: str) -> _Rates | None:
    """Pick the rate row for *model* (SDS §6.10.1): exact pinned snapshot first, then a family
    fallback (``mini`` vs a bare ``realtime`` flagship) so an unlisted snapshot roll still meters.
    An unrecognised model returns ``None`` — the meter then counts tokens but skips dollars."""
    if model in _RATES:
        return _RATES[model]
    if "mini" in model:
        return _MINI_RATES
    if "realtime" in model:
        return _FLAGSHIP_RATES
    return None


class CostMeterService:
    """Accumulate per-turn :class:`~avid.domain.TokenUsage` and log projected monthly spend (§6.10.6).

    Satisfies the :class:`~avid.core.ports.Service` shape (``name``/``start``/``stop``/
    ``subscriptions``). Purely reactive — no owned task, ``start``/``stop`` are no-ops — so the
    composition root registers its one subscription and then drops it; the bound-method
    subscription keeps the instance alive for the life of the bus (like ``AffectService``).
    Depends only on the :class:`~avid.core.ports.EventBus` **Protocol** (P2); the ``model`` name is
    injected from ``[ai] model`` (P7), never read here.
    """

    name = _SOURCE

    def __init__(self, *, bus: EventBus, model: str) -> None:
        self._bus = bus
        self._model = model
        self._rates = _resolve_rates(model)
        if self._rates is None:
            _log.warning(
                "cost meter has no rate row for model %r — token counts will accrue but "
                "dollar projection is disabled (SDS §6.10.1); add its rates to _RATES",
                model,
            )
        # The running daily-total accumulators (TokenUsage.__add__ exists for exactly this,
        # §6.10.6): every turn's counts folded in, plus a turn count for the per-turn average.
        self._total = TokenUsage(input_tokens=0, cached_input_tokens=0, output_tokens=0)
        self._turns = 0
        self._total_cost_usd = 0.0

    # --- read surface (what a future /metrics endpoint reports, SDS §2180) ---------------

    @property
    def total_usage(self) -> TokenUsage:
        """The running daily-total token counts across every metered turn (§6.10.6)."""
        return self._total

    @property
    def turns(self) -> int:
        """How many ``conversation.turn_ended`` facts have been metered."""
        return self._turns

    @property
    def total_cost_usd(self) -> float:
        """Cumulative dollar cost so far (0.0 when no rate row is known for the model)."""
        return self._total_cost_usd

    @property
    def projected_monthly_usd(self) -> float:
        """Projected monthly spend to compare against the O7 $25 budget (§6.10.6)."""
        return self._projected_monthly_usd()

    @property
    def cached_ratio(self) -> float:
        """Fraction of input tokens served from cache — the §6.10.3 caching-failure tripwire."""
        return self._cached_ratio()

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """No owned task: the meter is purely reactive. Present for the §9.2 shape."""

    async def stop(self) -> None:
        """Idempotent and instant — nothing to unwind (§9.2's 5 s budget)."""

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare the single ``conversation.turn_ended`` subscription (SDS §9.2, §9.1.3).

        This is the catalogued observability consumer of that fact (SDS §9.1.3, line 2013).
        DROP_OLDEST, matching every other wired subscriber: ``turn_ended`` is low-frequency so the
        queue never backs up in practice, and the §9.1.5 drift check sees it by its mandatory name.
        """
        return (
            Subscription(
                event_type=ConversationTurnEnded,
                handler=cast(Handler, self._on_turn_ended),
                name="CostMeterService.turn_ended",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the meter -----------------------------------------------------------------------

    async def _on_turn_ended(self, event: ConversationTurnEnded) -> None:
        """Fold this turn's usage into the running total and log the projection (§6.10.6).

        The cached-input ratio is logged every turn because its *collapse* is the only early
        sign of the §6.10.3 caching failure — dollars follow later, on the invoice. A projected
        monthly spend over the O7 budget escalates to a warning, so the gate's tripwire is loud."""
        self._total = self._total + event.usage
        self._turns += 1
        self._total_cost_usd += self._turn_cost_usd(event.usage)

        cached_ratio = self._cached_ratio()
        if self._rates is None:
            _log.info(
                "cost meter [%s]: %d turns, %d tokens, cached-input %.1f%% (no rate row — "
                "dollars disabled)",
                self._model,
                self._turns,
                self._total.total_tokens,
                cached_ratio * 100,
            )
            return

        projected = self._projected_monthly_usd()
        message = (
            "cost meter [%s]: %d turns, $%.5f so far, projected $%.2f/mo (O7 $%.0f), "
            "cached-input %.1f%%"
        )
        args = (
            self._model,
            self._turns,
            self._total_cost_usd,
            projected,
            _O7_MONTHLY_BUDGET_USD,
            cached_ratio * 100,
        )
        if projected > _O7_MONTHLY_BUDGET_USD:
            _log.warning(message + " — OVER O7 BUDGET (§6.10.6 tripwire)", *args)
        else:
            _log.info(message, *args)

    def _turn_cost_usd(self, usage: TokenUsage) -> float:
        """Dollar cost of one turn (SDS §6.10.1): cached + uncached input + output, each at its
        own per-1M rate. Zero when no rate row is known — the caller still accrues token counts."""
        if self._rates is None:
            return 0.0
        return (
            usage.cached_input_tokens * self._rates.cached_input
            + usage.uncached_input_tokens * self._rates.uncached_input
            + usage.output_tokens * self._rates.output
        ) / _TOKENS_PER_MILLION

    def _projected_monthly_usd(self) -> float:
        """Extrapolate a monthly spend from the average turn cost and the §6.10.3 usage model
        (~20 turns/day × 30 days). Zero before any turn has been metered."""
        if self._turns == 0:
            return 0.0
        cost_per_turn = self._total_cost_usd / self._turns
        return cost_per_turn * _MODELED_TURNS_PER_DAY * _DAYS_PER_MONTH

    def _cached_ratio(self) -> float:
        """Fraction of all input tokens served from the prompt cache (SDS §6.10.2, Fact 1). Its
        collapse is the §6.10.3 caching-failure tripwire. Zero when no input has been billed yet."""
        if self._total.input_tokens == 0:
            return 0.0
        return self._total.cached_input_tokens / self._total.input_tokens
