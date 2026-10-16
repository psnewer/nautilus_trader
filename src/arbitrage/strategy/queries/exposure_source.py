"""补偿树的订单/仓位来源分类,并向命中分支提交目标 exposure 子集。"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_magic
from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.position_orders import targets_existing_position


_SOURCES = {
    "pre_game_order": ("order", "pre_game"),
    "in_game_order": ("order", "in_game"),
    "pre_game_position": ("position", "pre_game"),
    "in_game_position": ("position", "in_game"),
}
CANCEL_CANDIDATES = "cancel_candidates"
POSITION_CANDIDATES = "position_candidates"


class ExposureSourceQuery(StateQuery):
    """按配置来源独立筛订单或仓位;命中子集写入本树 scratch。"""

    def __init__(
        self,
        source: str,
        position_order: bool | None = None,
        magic: str | None = None,
    ) -> None:
        if source not in _SOURCES:
            raise ValueError(f"exposure_source: unsupported source {source!r}")
        self._kind, self._origin = _SOURCES[source]
        if position_order is not None and not isinstance(position_order, bool):
            raise ValueError("exposure_source: position_order must be a boolean")
        if self._kind != "order" and position_order is not None:
            raise ValueError("exposure_source: position_order is only valid for order sources")
        if magic is not None and (not isinstance(magic, str) or not magic.strip()):
            raise ValueError("exposure_source: magic must be a non-empty string")
        if self._kind != "order" and magic is not None:
            raise ValueError("exposure_source: magic is only valid for order sources")
        self._position_order = position_order
        self._magic = magic.strip() if magic is not None else None

    def matches(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        if self._kind == "order":
            orders = _pair_open_orders(ctx)
            candidates = [
                str(order.client_order_id)
                for order in orders
                if order_origin(order) == self._origin
                and (self._magic is None or order_magic(order) == self._magic)
                and (
                    self._position_order is None
                    or targets_existing_position(ctx, order) is self._position_order
                )
                and str(getattr(order, "client_order_id", "") or "")
            ]
            if not candidates:
                return False
            ctx.scratch[CANCEL_CANDIDATES] = sorted(set(candidates))
            return True
        positions = pair_positions(ctx)
        if not positions:
            return False
        candidates = [
            _position_id(position)
            for position in positions
            if _position_origin(ctx, position) == self._origin
        ]
        candidates = [candidate for candidate in candidates if candidate]
        if not candidates:
            return False
        ctx.scratch[POSITION_CANDIDATES] = sorted(set(candidates))
        return True


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


def _position_id(position) -> str:
    return str(
        getattr(position, "id", None)
        or getattr(position, "position_id", None)
        or "",
    )
