"""比赛阶段 self_hits 判据。

框架首个实用 `self_hits` 叶子(此前全是 `{}` = vacuous truth)。只读 sports 现状,
不保存跨轮状态。详细设计见 strategy §4.3 / §3.10。
"""

from __future__ import annotations

from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.common.sports_phase import PHASE_PRE
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class InGameQuery(StateQuery):
    """比赛进行中 = phase_store 该 game 的有效阶段为 ``IN_PLAY``。

    经 `ctx.pair_registry.game_id_for_pair(pair_id)` → `ctx.phase_store.get(gid)` 查现状。
    缺 game_id / phase_store / phase 状态均为 UNKNOWN，不命中。
    """

    def matches(self, ctx: EvalContext) -> bool:
        pair_registry = ctx.pair_registry
        phase_store = ctx.phase_store
        if pair_registry is None or phase_store is None:
            return False
        game_id = pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return False
        state = phase_store.get(game_id)
        if state is None:
            return False
        return getattr(state, "phase", None) == PHASE_IN_PLAY


class PreGameQuery(StateQuery):
    """明确赛前 = phase_store 该 game 的有效阶段为 ``PRE``。

    Store 无记录或 phase 缺失均是 UNKNOWN，不能由 ``NOT in_game`` 反推为赛前。
    """

    def matches(self, ctx: EvalContext) -> bool:
        pair_registry = ctx.pair_registry
        phase_store = ctx.phase_store
        if pair_registry is None or phase_store is None:
            return False
        game_id = pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return False
        state = phase_store.get(game_id)
        if state is None:
            return False
        return getattr(state, "phase", None) == PHASE_PRE
