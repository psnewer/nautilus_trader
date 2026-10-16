"""按仓位初始订单的下单盘筛选平仓腿。"""

from __future__ import annotations

import logging

from src.arbitrage.common.opportunity import order_score
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


_LOG = logging.getLogger(__name__)


class SameSetAction(Action):
    """启用时只保留仍处于仓位 opening order 下单盘的平仓腿。"""

    def __init__(self, limit: bool | None = None) -> None:
        if limit is not None and not isinstance(limit, bool):
            raise ValueError("same_set: limit must be a boolean")
        self._limit = True if limit is None else limit

    async def execute(self, ctx: EvalContext) -> None:
        if not self._limit:
            return
        current_score = _current_score_key(ctx)
        positions = _positions_by_id(ctx)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            self._filter_selected(ctx, selected, positions, current_score)
            return

        if "candidates" in ctx.scratch:
            self._filter_candidates(ctx, positions, current_score)
            return

        legs = ctx.scratch.get("legs")
        if isinstance(legs, list) and legs:
            ctx.scratch["legs"] = self._filter_legs(ctx, legs, positions, current_score)

    def _filter_selected(self, ctx, selected, positions, current_score) -> None:
        if selected.get("cancel_pair_orders"):
            return
        legs = selected.get("legs")
        if not isinstance(legs, list) or not legs:
            return
        kept = self._filter_legs(ctx, legs, positions, current_score)
        filtered = dict(selected)
        filtered["legs"] = kept
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = kept

    def _filter_candidates(self, ctx, positions, current_score) -> None:
        candidates = ctx.scratch.get("candidates")
        if not isinstance(candidates, list):
            ctx.scratch["candidates"] = []
            return
        filtered_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("cancel_pair_orders"):
                filtered_candidates.append(candidate)
                continue
            legs = candidate.get("legs")
            if not isinstance(legs, list) or not legs:
                continue
            kept = self._filter_legs(ctx, legs, positions, current_score)
            if kept:
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
        ctx.scratch["candidates"] = filtered_candidates

    def _filter_legs(
        self,
        ctx: EvalContext,
        legs: list[dict],
        positions: dict[str, object],
        current_score,
    ) -> list[dict]:
        kept = []
        for leg in legs:
            position_id = str(leg.get("position_id") or "")
            position = positions.get(position_id)
            opening_score = _opening_score_key(ctx, position)
            if opening_score is not None and current_score is not None and same_set(
                opening_score,
                current_score,
            ):
                kept.append(leg)
                continue
            _LOG.info(
                f"SameSet: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"position_id={position_id or None} opening_score={opening_score} "
                f"current_score={current_score}",
            )
        return kept


def _positions_by_id(ctx: EvalContext) -> dict[str, object]:
    result = {}
    for position in pair_positions(ctx):
        position_id = getattr(position, "id", None) or getattr(position, "position_id", None)
        if position_id is not None:
            result[str(position_id)] = position
    return result


def _opening_score_key(ctx: EvalContext, position):
    if position is None or ctx.cache is None:
        return None
    opening_order_id = getattr(position, "opening_order_id", None)
    if opening_order_id is None:
        return None
    opening_order = ctx.cache.order(opening_order_id)
    return score_key(order_score(opening_order)) if opening_order is not None else None


def _current_score_key(ctx: EvalContext):
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return None
    state = ctx.sports_store.get(game_id)
    return score_key(getattr(state, "score", "") if state is not None else "")
