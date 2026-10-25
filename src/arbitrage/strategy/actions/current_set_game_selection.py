"""按当前盘局分筛选执行腿，不比较已完成盘或抢七小分。"""

from __future__ import annotations

from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.actions.score_selection import _unit_is_complete
from src.arbitrage.strategy.competition_tier import lower_tier_oe_competition
from src.arbitrage.strategy.condition import EvalContext


class CurrentSetGameSelectionAction(ScoreSelectionAction):
    """按最新一盘局分筛腿，可选择仅对低级别赛事生效。"""

    def __init__(
        self,
        standing: str | None = None,
        tie_break: bool = True,
        tier_only: bool | None = None,
        min: int | None = None,
        max: int | None = None,
    ) -> None:
        if tier_only is not None and not isinstance(tier_only, bool):
            raise ValueError("current_set_game_selection: tier_only must be a boolean")
        if min is not None and (isinstance(min, bool) or not isinstance(min, int) or min <= 0):
            raise ValueError("current_set_game_selection: min must be a positive integer")
        if max is not None and (isinstance(max, bool) or not isinstance(max, int) or max <= 0):
            raise ValueError("current_set_game_selection: max must be a positive integer")
        if min is not None and max is not None and min > max:
            raise ValueError("current_set_game_selection: min must be <= max")
        super().__init__(standing=standing, tie_break=tie_break)
        self._tier_only = bool(tier_only)
        self._min = min
        self._max = max

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
        match = _SCORE_PART.match(parts[-1])
        left, right, tie_left, tie_right = match.groups()
        latest = (
            int(left),
            int(right),
            int(tie_left) if tie_left is not None else None,
            int(tie_right) if tie_right is not None else None,
        )
        # 新盘 0-0 到达前，数据源会短暂只保留刚结束的一盘。此时没有“当前盘”，
        # 不能把上一盘终局误当成当前局分领先关系。
        if _unit_is_complete(latest):
            return {}
        if int(left) == 0 and int(right) == 0:
            return {}
        if int(left) == int(right) == 6 and not self._tie_break:
            return {}
        left_games = int(left)
        right_games = int(right)
        game_gap = abs(left_games - right_games)
        if self._min is not None and game_gap < self._min:
            return {}
        if self._max is not None and game_gap > self._max:
            return {}
        if left_games > right_games:
            return {"home": "win", "away": "lose"}
        if left_games < right_games:
            return {"home": "lose", "away": "win"}
        return {"home": "draw", "away": "draw"}
