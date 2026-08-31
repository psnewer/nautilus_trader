"""InGameQuery —— pre_rebate 赛前/赛中 self_hits 判据(#326)。

框架首个实用 `self_hits` 叶子(此前全是 `{}` = vacuous truth)。只读 sports 现状,
不保存跨轮状态。详细设计见 strategy §4.3 / §3.10。
"""

from __future__ import annotations

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class InGameQuery(StateQuery):
    """比赛进行中 = phase_store 该 game 的有效阶段为 ``IN_PLAY``。

    经 `ctx.pair_registry.game_id_for_pair(pair_id)` → `ctx.phase_store.get(gid)` 查现状。
    赛前门用 `{"NOT": {"type": "in_game"}}`,同时覆盖 PRE 与无 phase 状态。
    **边界**:POST 落入 `NOT in_game`(与赛前同支),因 ended 后 pair 立即回收(§3.8.1)、
    只触及最后一次评估,影响可忽略。缺 game_id / phase_store → False(fail-closed)。
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
        return getattr(state, "phase", None) == "IN_PLAY"
