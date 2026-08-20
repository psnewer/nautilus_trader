"""OBD 驱动的 pair 级撤单补偿; 价格帧过滤由 Evaluator 入口统一负责。"""

from __future__ import annotations

from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class PriceChangeRecoveryCheck(Check):
    """OBD 唤醒本轮评估且 pair 存在挂单时, 撤销整个 pair 的挂单。"""

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.event_name != "OrderBookDeltas" or ctx.cache is None or ctx.pair_registry is None:
            return False

        instrument_ids = tuple(pair_instrument_ids(ctx))
        open_orders = []
        seen = set()
        for instrument_id in instrument_ids:
            for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                client_order_id = str(getattr(order, "client_order_id", "") or "")
                key = client_order_id or id(order)
                if key in seen:
                    continue
                seen.add(key)
                open_orders.append({
                    "client_order_id": client_order_id,
                    "instrument_id": str(instrument_id),
                })
        if not open_orders:
            return False

        ctx.scratch["cancel_pair_orders"] = {
            "reason": "price_change_recovery",
            "open_orders": open_orders,
        }
        return True
