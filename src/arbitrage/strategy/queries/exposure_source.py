"""补偿树的订单/仓位来源分类。"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.condition import EvalContext


_SOURCES = {
    "pre_game_order": ("order", "pre_game"),
    "in_game_order": ("order", "in_game"),
    "pre_game_position": ("position", "pre_game"),
    "in_game_position": ("position", "in_game"),
}


class ExposureSourceQuery(StateQuery):
    """有 open order 时优先查询是否存在目标来源订单,否则查询目标来源仓位。"""

    def __init__(self, source: str) -> None:
        if source not in _SOURCES:
            raise ValueError(f"exposure_source: unsupported source {source!r}")
        self._kind, self._origin = _SOURCES[source]

    def matches(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        orders = _pair_open_orders(ctx)
        if orders:
            if self._kind != "order":
                return False
            return any(order_origin(order) == self._origin for order in orders)
        if self._kind != "position":
            return False
        positions = pair_positions(ctx)
        if not positions:
            return False
        return any(_position_origin(ctx, position) == self._origin for position in positions)


def _pair_open_orders(ctx: EvalContext) -> list:
    orders = []
    seen = set()
    for instrument_id in pair_instrument_ids(ctx):
        for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
            key = str(getattr(order, "client_order_id", "") or "") or id(order)
            if key not in seen:
                seen.add(key)
                orders.append(order)
    return orders


def _position_origin(ctx: EvalContext, position) -> str | None:
    opening_order_id = getattr(position, "opening_order_id", None)
    if opening_order_id is None:
        return None
    opening_order = ctx.cache.order(opening_order_id)
    return order_origin(opening_order) if opening_order is not None else None
