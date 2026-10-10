"""按当前盘局分筛选执行腿，不比较已完成盘或抢七小分。"""

from __future__ import annotations

from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.competition_tier import lower_tier_oe_competition
from src.arbitrage.strategy.condition import EvalContext


class CurrentSetGameSelectionAction(ScoreSelectionAction):
    """按最新一盘局分筛腿，可选择仅对低级别赛事生效。"""

    def __init__(
        self,
        standing: str | None = None,
        tier_only: bool | None = None,
    ) -> None:
        if tier_only is not None and not isinstance(tier_only, bool):
            raise ValueError("current_set_game_selection: tier_only must be a boolean")
        super().__init__(standing=standing)
        self._tier_only = bool(tier_only)

    async def execute(self, ctx: EvalContext) -> None:
        if self._tier_only and lower_tier_oe_competition(ctx) is None:
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
        if not parts or any(_SCORE_PART.match(part) is None for part in parts):
            return {}
        left, right, _, _ = _SCORE_PART.match(parts[-1]).groups()
        if int(left) > int(right):
            return {"home": "win", "away": "lose"}
        if int(left) < int(right):
            return {"home": "lose", "away": "win"}
        return {"home": "draw", "away": "draw"}
