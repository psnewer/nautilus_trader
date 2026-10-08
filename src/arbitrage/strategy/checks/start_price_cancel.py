"""StartPriceCancelCheck —— 按配置比分状态撤销 start_game 挂单。"""

from __future__ import annotations

from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.strategy.actions.score_selection import _pair_roles
from src.arbitrage.strategy.actions.score_selection import _parse_standings
from src.arbitrage.strategy.actions.score_selection import _should_keep
from src.arbitrage.strategy.actions.score_selection import _side_role
from src.arbitrage.strategy.actions.score_selection import _standings
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class StartPriceCancelCheck(Check):
    """start_game 挂单命中目标比分状态时,生成定向撤单请求。"""

    def __init__(self, standing: str = "win") -> None:
        try:
            self._standings = _parse_standings(standing)
        except ValueError as exc:
            raise ValueError(
                "start_price_cancel: standing must contain win, draw, or lose separated by '|'",
            ) from exc
        if self._standings is None:
            raise ValueError("start_price_cancel: standing must be a string")

    def passes(self, ctx: EvalContext) -> bool:
        if (
            ctx.cache is None
            or ctx.pair_registry is None
            or ctx.sports_store is None
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
        for order in orders:
            if _order_matches_standing(ctx, order, standings, pair_roles, self._standings):
                targets.append(str(order.client_order_id))

        if not targets:
            return False
        reason = _cancel_reason(self._standings)
        ctx.scratch["cancel_pair_orders"] = {
            "reason": f"start_price_cancel:{reason}",
            "client_order_ids": targets,
        }
        return True


def _order_matches_standing(ctx, order, standings, pair_roles, allowed_standings) -> bool:
    leg = {
        "instrument_id": str(order.instrument_id),
        "side": _side_name(getattr(order, "side", None)),
    }
    role = _side_role(ctx, leg, pair_roles)
    return _should_keep(leg["side"], standings.get(role), allowed_standings)


def _cancel_reason(standings: frozenset[str]) -> str:
    labels = {"win": "leading", "draw": "draw", "lose": "losing"}
    ordered = [labels[value] for value in ("win", "lose", "draw") if value in standings]
    return "score_" + "_or_".join(ordered)


def _side_name(value) -> str:
    return str(getattr(value, "name", value) or "").upper()


def _was_submitted(order) -> bool:
    return int(getattr(order, "ts_submitted", 0) or 0) > 0
