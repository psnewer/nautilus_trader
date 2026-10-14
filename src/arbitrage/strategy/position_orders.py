"""识别显式针对已有 Position 的订单。"""

from __future__ import annotations

from src.arbitrage.strategy.condition import EvalContext


def targets_existing_position(ctx: EvalContext, order) -> bool:
    """SELL 且 NT Cache 已绑定 position_id 才视为针对仓位的订单。"""
    raw_side = getattr(order, "side", None)
    side = str(getattr(raw_side, "name", raw_side or "")).upper()
    if side != "SELL" or ctx.cache is None:
        return False
    position_id_fn = getattr(ctx.cache, "position_id", None)
    if not callable(position_id_fn):
        return False
    client_order_id = getattr(order, "client_order_id", None)
    return client_order_id is not None and position_id_fn(client_order_id) is not None
