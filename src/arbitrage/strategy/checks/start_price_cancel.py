"""StartPriceCancelCheck —— 撤销领先或长时间无比分的 start_game 挂单。"""

from __future__ import annotations

import math

from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.strategy.actions.score_selection import _pair_roles
from src.arbitrage.strategy.actions.score_selection import _should_keep
from src.arbitrage.strategy.actions.score_selection import _side_role
from src.arbitrage.strategy.actions.score_selection import _standings
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class StartPriceCancelCheck(Check):
    """start_game 挂单领先，或提交超时且仍无比分时，生成定向撤单请求。"""

    def __init__(self, timeout_ms: int = 600_000) -> None:
        value = float(timeout_ms)
        if isinstance(timeout_ms, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("start_price_cancel: timeout_ms must be a positive finite number")
        self._timeout_ns = int(value * 1_000_000)

    def passes(self, ctx: EvalContext) -> bool:
        if (
            ctx.cache is None
            or ctx.pair_registry is None
            or ctx.sports_store is None
            or ctx.ts_now_ns is None
        ):
            return False

        orders = [
            order
            for instrument_id in pair_instrument_ids(ctx)
            for order in (ctx.cache.orders_open(instrument_id=instrument_id) or ())
            if _was_submitted(order) and order_intent(order) == "start_game"
        ]
        if not orders:
            return False

        game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return False
        state = ctx.sports_store.get(game_id)
        score = str(getattr(state, "score", "") or "").strip()
        standings = _standings(ctx, tie_break=False) if score else {}
        pair_roles = _pair_roles(ctx)

        targets = []
        reasons = set()
        for order in orders:
            if _order_is_leading(ctx, order, standings, pair_roles):
                targets.append(str(order.client_order_id))
                reasons.add("score_leading")
                continue
            if not score and int(ctx.ts_now_ns) - int(order.ts_submitted) > self._timeout_ns:
                targets.append(str(order.client_order_id))
                reasons.add("no_score_timeout")

        if not targets:
            return False
        ctx.scratch["cancel_pair_orders"] = {
            "reason": f"start_price_cancel:{'+'.join(sorted(reasons))}",
            "client_order_ids": targets,
        }
        return True


def _order_is_leading(ctx, order, standings, pair_roles) -> bool:
    leg = {
        "instrument_id": str(order.instrument_id),
        "side": _side_name(getattr(order, "side", None)),
    }
    role = _side_role(ctx, leg, pair_roles)
    return _should_keep(leg["side"], standings.get(role), frozenset({"win"}))


def _side_name(value) -> str:
    return str(getattr(value, "name", value) or "").upper()


def _was_submitted(order) -> bool:
    return int(getattr(order, "ts_submitted", 0) or 0) > 0
