"""DashGateAction —— 按开赛价格过滤胜出的 candidate 买腿。"""

from __future__ import annotations

import logging

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class DashGateAction(Action):
    """删除概率严格低于对应开赛概率一半的 BUY 腿。"""

    async def execute(self, ctx: EvalContext) -> None:
        selected = ctx.scratch.get("selected_candidate")
        if not isinstance(selected, dict) or selected.get("cancel_pair_orders"):
            return
        legs = selected.get("legs")
        if not isinstance(legs, list) or not legs or ctx.cache is None:
            return

        state = PairPriceStore(ctx.cache).get(ctx.pair_id)
        if state is None:
            return

        kept = []
        for leg in legs:
            if _should_drop(leg, state.start_price):
                _LOG.info(
                    f"DashGate: pair={ctx.pair_id} drop BUY leg="
                    f"{leg.get('instrument_id')} outcome={_outcome(leg)} "
                    f"prob={leg.get('prob')} start_price="
                    f"{state.start_price.get(_outcome(leg))}",
                )
                continue
            kept.append(leg)

        filtered = dict(selected)
        filtered["legs"] = kept
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = kept


def _should_drop(leg: dict, start_prices: dict[str, float]) -> bool:
    if str(leg.get("side") or "BUY").upper() != "BUY":
        return False
    outcome = _outcome(leg)
    start_price = _positive_float(start_prices.get(outcome))
    probability = _positive_float(leg.get("prob"))
    if start_price is None or probability is None:
        return False
    return probability < 0.5 * start_price


def _outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()


def _positive_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
