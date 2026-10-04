"""StartGameQuery —— 赛中且该 pair 从未提交过订单。"""

from __future__ import annotations

from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import EvalContext


class StartGameQuery(StateQuery):
    """明确 IN_PLAY 且 NT Cache 中该 pair 没有曾进入 SUBMITTED 的订单。"""

    def matches(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None or ctx.phase_store is None:
            return False
        game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return False
        state = ctx.phase_store.get(game_id)
        if state is None or getattr(state, "phase", None) != PHASE_IN_PLAY:
            return False
        try:
            instrument_ids = pair_instrument_ids(ctx)
            if not instrument_ids:
                return False
            return not any(
                _was_submitted(order)
                for instrument_id in instrument_ids
                for order in (ctx.cache.orders(instrument_id=instrument_id) or ())
            )
        except (AttributeError, TypeError, ValueError):
            return False


def _was_submitted(order) -> bool:
    """NT Order 一旦应用 OrderSubmitted,`ts_submitted` 在后续终态仍保留。"""
    return int(getattr(order, "ts_submitted", 0) or 0) > 0
