"""赛前偏移挂单的实时低价方向反转撤单检查。"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import pm_ask_by_outcome
from src.arbitrage.strategy.checks.select_orders import CANCEL_CANDIDATES
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class PreGameReverseCheck(Check):
    """订单经济方向不再是 PM ask 最低方向时,生成定向撤单请求。"""

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        candidates = ctx.scratch.get(CANCEL_CANDIDATES)
        if not isinstance(candidates, list) or not candidates:
            return False
        orders = _pre_game_orders(ctx, set(candidates))
        if not orders:
            return False
        resolved = [_order_outcome(ctx, order) for order in orders]
        if any(outcome is None for outcome in resolved):
            return False
        order_outcomes = set(resolved)
        if len(order_outcomes) != 1:
            return False
        current = order_outcomes.pop()
        opponents = [value for value in VALID_OUTCOMES if value != current]
        if len(opponents) != 1:
            return False
        current_ask = pm_ask_by_outcome(ctx, current)
        opponent_ask = pm_ask_by_outcome(ctx, opponents[0])
        if current_ask is None or opponent_ask is None or current_ask <= opponent_ask:
            return False

        ctx.scratch["cancel_pair_orders"] = {
            "reason": "pre_game_reverse",
            "client_order_ids": [str(order.client_order_id) for order in orders],
        }
        return True


def _pre_game_orders(ctx: EvalContext, candidate_ids: set[str]) -> list:
    orders = []
    seen = set()
    for instrument_id in pair_instrument_ids(ctx):
        for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            key = client_order_id or id(order)
            if key in seen:
                continue
            if client_order_id not in candidate_ids:
                continue
            if _was_submitted(order) and order_origin(order) == "pre_game":
                seen.add(key)
                orders.append(order)
    return orders


def _order_outcome(ctx: EvalContext, order) -> str | None:
    instrument = ctx.cache.instrument(order.instrument_id)
    info = getattr(instrument, "info", None) or {}
    outcome = str(info.get("claim") or info.get("selection_role") or "").strip().lower()
    if outcome not in VALID_OUTCOMES:
        return None
    side = str(getattr(getattr(order, "side", None), "name", getattr(order, "side", "BUY"))).upper()
    if side != "SELL":
        return outcome
    opponents = [value for value in VALID_OUTCOMES if value != outcome]
    return opponents[0] if len(opponents) == 1 else None


def _was_submitted(order) -> bool:
    return int(getattr(order, "ts_submitted", 0) or 0) > 0
