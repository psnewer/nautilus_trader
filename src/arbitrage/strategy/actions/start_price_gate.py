"""StartPriceGateAction —— 按最大开赛价或显式阈值过滤 outcome 执行腿。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class StartPriceGateAction(Action):
    """缺省保留最大 start_price；配置 value 后保留大于等于阈值的 outcome。"""

    def __init__(self, value: float | None = None) -> None:
        if value is None:
            self._value = None
            return
        try:
            threshold = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"start_price_gate: value must be finite, got {value!r}",
            ) from exc
        if not math.isfinite(threshold):
            raise ValueError(f"start_price_gate: value must be finite, got {value!r}")
        self._value = threshold

    async def execute(self, ctx: EvalContext) -> None:
        eligible_outcomes = _eligible_start_outcomes(ctx, self._value)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(ctx, legs, eligible_outcomes, self._value)
            filtered = dict(selected)
            filtered["legs"] = kept
            ctx.scratch["selected_candidate"] = filtered
            ctx.scratch["legs"] = kept
            return

        if "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                ctx.scratch["candidates"] = []
                return
            filtered_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("cancel_pair_orders"):
                    filtered_candidates.append(candidate)
                    continue
                legs = candidate.get("legs")
                if not isinstance(legs, list) or not legs:
                    continue
                kept = _filter_legs(ctx, legs, eligible_outcomes, self._value)
                if not kept:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if not isinstance(legs, list) or not legs:
            return
        ctx.scratch["legs"] = _filter_legs(ctx, legs, eligible_outcomes, self._value)


def _filter_legs(
    ctx: EvalContext,
    legs: list[dict],
    eligible_outcomes: set[str] | None,
    threshold: float | None,
) -> list[dict]:
    kept = []
    for leg in legs:
        outcome = _leg_outcome(leg)
        if eligible_outcomes is not None and outcome in eligible_outcomes:
            kept.append(leg)
            continue
        _LOG.info(
            f"StartPriceGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"outcome={outcome} "
            f"eligible_outcomes={sorted(eligible_outcomes) if eligible_outcomes else None} "
            f"threshold={threshold}",
        )
    return kept


def _eligible_start_outcomes(
    ctx: EvalContext,
    threshold: float | None,
) -> set[str] | None:
    if ctx.cache is None or ctx.pair_registry is None:
        return None
    state = PairPriceStore(ctx.cache).get(ctx.pair_id)
    if state is None or set(state.start_price) != set(state.outcomes):
        return None
    values = {}
    for outcome in state.outcomes:
        value = float(state.start_price[outcome])
        if not math.isfinite(value):
            return None
        values[outcome] = value
    if not values:
        return None
    if threshold is not None:
        return {outcome for outcome, value in values.items() if value >= threshold}
    top = max(values.values())
    return {
        outcome
        for outcome, value in values.items()
        if math.isclose(value, top, abs_tol=1e-12)
    }


def _leg_outcome(leg: dict) -> str:
    return str(leg.get("role") or leg.get("claim") or "").strip().lower()
