"""StartGameQuery —— 赛中且该 pair 没有阻断 start_game 的提交历史。"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import EvalContext


class StartGameQuery(StateQuery):
    """明确 IN_PLAY 且 NT Cache 中该 pair 没有阻断提交历史。"""

    def __init__(self, repeat_after_cancel: bool = False) -> None:
        if not isinstance(repeat_after_cancel, bool):
            raise ValueError("start_game: repeat_after_cancel must be a boolean")
        self._repeat_after_cancel = repeat_after_cancel

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
                _blocks_start_game(order, repeat_after_cancel=self._repeat_after_cancel)
                for instrument_id in instrument_ids
                for order in (ctx.cache.orders(instrument_id=instrument_id) or ())
            )
        except (AttributeError, TypeError, ValueError):
            return False


def _was_submitted(order) -> bool:
    """NT Order 一旦应用 OrderSubmitted,`ts_submitted` 在后续终态仍保留。"""
    return int(getattr(order, "ts_submitted", 0) or 0) > 0


def _blocks_start_game(order, *, repeat_after_cancel: bool) -> bool:
    if not _was_submitted(order):
        return False
    if not repeat_after_cancel:
        return True
    return not (
        order_intent(order) == "start_game"
        and _status_name(order) == "CANCELED"
        and _filled_qty(order) == 0.0
    )


def _status_name(order) -> str:
    status = getattr(order, "status", None)
    return str(getattr(status, "name", status) or "").upper()


def _filled_qty(order) -> float:
    try:
        return float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return float("inf")
