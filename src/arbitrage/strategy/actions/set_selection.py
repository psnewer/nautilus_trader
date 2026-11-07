"""按已完成盘胜数筛选执行腿或定向撤单。"""

from __future__ import annotations

import logging

from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.common.opportunity import order_score
from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.actions.score_selection import _pair_roles
from src.arbitrage.strategy.actions.score_selection import _should_keep
from src.arbitrage.strategy.actions.score_selection import _side_role
from src.arbitrage.strategy.actions.score_selection import _unit_is_complete
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


_LOG = logging.getLogger(__name__)


class SetSelectionAction(ScoreSelectionAction):
    """只按双方已赢盘数比较 ``win/draw/lose``。"""

    def __init__(
        self,
        standing: str | None = None,
        current_set_exempt: bool = False,
    ) -> None:
        if not isinstance(current_set_exempt, bool):
            raise ValueError("set_selection: current_set_exempt must be a boolean")
        super().__init__(standing=standing)
        self._current_set_exempt = current_set_exempt

    async def execute(self, ctx: EvalContext) -> None:
        if self._standings is None:
            return
        if self._filter_cancel_request(ctx):
            return
        await super().execute(ctx)

    def _get_standings(self, ctx: EvalContext) -> dict[str, str]:
        if ctx.sports_store is None or ctx.pair_registry is None:
            return {}
        game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return {}
        state = ctx.sports_store.get(game_id)
        score = getattr(state, "score", "") if state is not None else ""
        parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
        matches = [_SCORE_PART.match(part) for part in parts]
        if not matches or any(match is None for match in matches):
            return {}
        parsed = [
            (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)) if match.group(3) is not None else None,
                int(match.group(4)) if match.group(4) is not None else None,
            )
            for match in matches
            if match is not None
        ]
        completed = parsed if _unit_is_complete(parsed[-1]) else parsed[:-1]
        if any(not _unit_is_complete(unit) for unit in completed):
            return {}
        home_sets = sum(left > right for left, right, _, _ in completed)
        away_sets = sum(left < right for left, right, _, _ in completed)
        if home_sets > away_sets:
            return {"home": "win", "away": "lose"}
        if home_sets < away_sets:
            return {"home": "lose", "away": "win"}
        return {"home": "draw", "away": "draw"}

    def _filter_legs(
        self,
        ctx: EvalContext,
        legs: list[dict],
        *,
        standings: dict[str, str] | None = None,
        pair_roles: set[str] | None = None,
    ) -> list[dict]:
        if not self._current_set_exempt:
            return super()._filter_legs(
                ctx,
                legs,
                standings=standings,
                pair_roles=pair_roles,
            )

        positions = _positions_by_id(ctx)
        current_score = _current_score_key(ctx)
        exempt_indexes = {
            index
            for index, leg in enumerate(legs)
            if str(leg.get("side") or "").upper() == "SELL"
            and _position_opened_in_current_set(
                ctx,
                positions.get(str(leg.get("position_id") or "")),
                current_score,
            )
        }
        filtered = super()._filter_legs(
            ctx,
            [leg for index, leg in enumerate(legs) if index not in exempt_indexes],
            standings=standings,
            pair_roles=pair_roles,
        )
        filtered_ids = {id(leg) for leg in filtered}
        kept = [
            leg
            for index, leg in enumerate(legs)
            if index in exempt_indexes or id(leg) in filtered_ids
        ]
        if exempt_indexes:
            _LOG.info(
                f"SetSelection: pair={ctx.pair_id} current_set_exempt_legs="
                f"{len(exempt_indexes)}",
            )
        return kept

    def _filter_cancel_request(self, ctx: EvalContext) -> bool:
        request = ctx.scratch.get("cancel_pair_orders")
        if not isinstance(request, dict):
            return False
        candidate_ids = request.get("client_order_ids")
        if not isinstance(candidate_ids, (list, tuple)):
            ctx.scratch.pop("cancel_pair_orders", None)
            return True

        standings = self._get_standings(ctx)
        pair_roles = _pair_roles(ctx)
        kept = []
        candidate_set = {str(value) for value in candidate_ids}
        if ctx.cache is not None:
            for instrument_id in pair_instrument_ids(ctx):
                role = _side_role(ctx, {"instrument_id": str(instrument_id)}, pair_roles)
                standing = standings.get(role)
                for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                    client_order_id = str(getattr(order, "client_order_id", "") or "")
                    if client_order_id in candidate_set and _should_keep(standing, self._standings):
                        kept.append(client_order_id)

        if kept:
            filtered = dict(request)
            filtered["client_order_ids"] = sorted(set(kept))
            ctx.scratch["cancel_pair_orders"] = filtered
        else:
            ctx.scratch.pop("cancel_pair_orders", None)
        _LOG.info(
            f"SetSelection: pair={ctx.pair_id} cancel_candidates={len(candidate_set)} "
            f"kept={len(set(kept))} allowed_standings={sorted(self._standings)}",
        )
        return True


def _positions_by_id(ctx: EvalContext) -> dict[str, object]:
    result = {}
    for position in pair_positions(ctx):
        position_id = getattr(position, "id", None) or getattr(position, "position_id", None)
        if position_id is not None:
            result[str(position_id)] = position
    return result


def _position_opened_in_current_set(ctx: EvalContext, position, current_score) -> bool:
    if position is None or current_score is None or ctx.cache is None:
        return False
    opening_order_id = getattr(position, "opening_order_id", None)
    if opening_order_id is None:
        return False
    opening_order = ctx.cache.order(opening_order_id)
    if opening_order is None or order_origin(opening_order) != "in_game":
        return False
    opening_score = score_key(order_score(opening_order))
    return opening_score is not None and same_set(opening_score, current_score)


def _current_score_key(ctx: EvalContext):
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return None
    state = ctx.sports_store.get(game_id)
    return score_key(getattr(state, "score", "") if state is not None else "")
