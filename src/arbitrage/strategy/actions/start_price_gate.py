"""StartPriceGateAction —— 只保留开赛价(start_price)最大的 outcome 对应的执行腿。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class StartPriceGateAction(Action):
    """按 pair 的 start_price 最大 outcome 过滤执行腿;无法判定开赛价时 fail-closed。"""

    async def execute(self, ctx: EvalContext) -> None:
        max_outcomes = _max_start_outcomes(ctx)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(ctx, legs, max_outcomes)
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
                kept = _filter_legs(ctx, legs, max_outcomes)
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
        ctx.scratch["legs"] = _filter_legs(ctx, legs, max_outcomes)


def _filter_legs(ctx: EvalContext, legs: list[dict], max_outcomes: set[str] | None) -> list[dict]:
    kept = []
    for leg in legs:
        outcome = _leg_outcome(leg)
        if max_outcomes is not None and outcome in max_outcomes:
            kept.append(leg)
            continue
        _LOG.info(
            f"StartPriceGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"outcome={outcome} max_outcomes={sorted(max_outcomes) if max_outcomes else None}",
        )
    return kept


def _max_start_outcomes(ctx: EvalContext) -> set[str] | None:
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
    top = max(values.values())
    return {outcome for outcome, value in values.items() if math.isclose(value, top, abs_tol=1e-12)}


def _leg_outcome(leg: dict) -> str:
    return str(leg.get("role") or leg.get("claim") or "").strip().lower()
