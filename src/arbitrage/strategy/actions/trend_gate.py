"""TrendGateAction —— 按当前最优概率相对 pair 趋势基准的方向筛 leg。"""

from __future__ import annotations

import logging

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.checks.quote_legs import best_probabilities_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class TrendGateAction(Action):
    """只保留相对 `PairPriceStore.trend_price` 朝指定方向变化的 outcome 腿。

    `up` 缺失或为 True 时保留 `current_best_ask > trend_price` 的 outcome；False 时保留
    `current_best_ask < trend_price` 的 outcome。相等为 flat，不保留。基准或当前完整报价缺失时
    fail-closed，全删 candidate 腿。
    """

    def __init__(self, up: bool = True) -> None:
        if not isinstance(up, bool):
            raise ValueError(f"trend_gate: up must be a boolean, got {up!r}")
        self._keep_up = up

    async def execute(self, ctx: EvalContext) -> None:
        selected = ctx.scratch.get("selected_candidate")
        if not isinstance(selected, dict) or selected.get("cancel_pair_orders"):
            return
        legs = selected.get("legs")
        if not isinstance(legs, list) or not legs:
            return

        directions = _directions(ctx)
        target = "up" if self._keep_up else "down"
        kept = []
        for leg in legs:
            outcome = _outcome(leg)
            if directions.get(outcome) == target:
                kept.append(leg)
                continue
            _LOG.info(
                f"TrendGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"outcome={outcome} direction={directions.get(outcome)} target={target}",
            )
        filtered = dict(selected)
        filtered["legs"] = kept
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = kept


def _directions(ctx: EvalContext) -> dict[str, str]:
    if ctx.cache is None or ctx.pair_registry is None:
        return {}
    state = PairPriceStore(ctx.cache).get(ctx.pair_id)
    if state is None or not state.trend_price:
        return {}
    current = best_probabilities_by_outcome(ctx.cache, ctx.pair_registry, ctx.pair_id)
    if current is None or set(current) != set(state.trend_price):
        return {}
    result = {}
    for outcome, price in current.items():
        baseline = state.trend_price[outcome]
        if price > baseline:
            result[outcome] = "up"
        elif price < baseline:
            result[outcome] = "down"
        else:
            result[outcome] = "flat"
    return result


def _outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()
