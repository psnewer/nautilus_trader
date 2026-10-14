"""
按来源及是否针对已有仓位筛选本 pair 的 open 订单子集,写入 scratch 供后续 cancel Check 消费。

把"订单分类"从 cancel 判定里剥离:本 Check 只产出候选子集 `scratch["cancel_candidates"]`
(client_order_ids),`order_cancel` 从中读入、只对这批单跑漂移判定。放在 `checktion` 的
`AND` 里、`order_cancel` 之前;子集为空即返回 False(AND 短路,本支 miss)。origin 由分支的
`exposure_source` self_hits 只做“是否存在”路由,实际 origin 子集由本 Check 逐单筛选。
"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.position_orders import targets_existing_position


CANCEL_CANDIDATES = "cancel_candidates"


class SelectOrdersCheck(Check):
    """按 origin / position_order 筛出 pair open 订单子集,非空即通过。"""

    def __init__(
        self,
        position_order: bool | None = None,
        origin: str | None = None,
    ) -> None:
        if position_order is not None and not isinstance(position_order, bool):
            raise ValueError("select_orders: position_order must be a boolean")
        if origin is not None and origin not in {"pre_game", "in_game"}:
            raise ValueError("select_orders: origin must be pre_game or in_game")
        self._position_order = position_order
        self._origin = origin

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        candidates = []
        seen = set()
        for instrument_id in pair_instrument_ids(ctx):
            for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                client_order_id = str(getattr(order, "client_order_id", "") or "")
                if not client_order_id or client_order_id in seen:
                    continue
                if self._origin is not None and order_origin(order) != self._origin:
                    continue
                if (
                    self._position_order is not None
                    and targets_existing_position(ctx, order) is not self._position_order
                ):
                    continue
                seen.add(client_order_id)
                candidates.append(client_order_id)
        if not candidates:
            return False
        ctx.scratch[CANCEL_CANDIDATES] = sorted(candidates)
        return True
