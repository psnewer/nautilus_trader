"""InGameQuery —— pre_rebate 赛前/赛中 self_hits 判据(#326)。

框架首个实用 `self_hits` 叶子(此前全是 `{}` = vacuous truth)。只读 sports 现状,
不保存跨轮状态。详细设计见 strategy §4.3 / §3.10。
"""

from __future__ import annotations

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class InGameQuery(StateQuery):
    """比赛进行中 = sports_store 该 game 存在且 `live` 且 `not ended`。

    经 `ctx.pair_registry.game_id_for_pair(pair_id)` → `ctx.sports_store.get(gid)` 查现状。
    赛前门用 `{"NOT": {"type": "in_game"}}`,同时覆盖真赛前(sports_store 为 `None` / 未 live)。
    **边界**:`ended` 落入 `NOT in_game`(与赛前同支),因 ended 后 pair 立即回收(§3.8.1)、
    只触及最后一次评估,影响可忽略。缺 game_id / sports_store → False(fail-closed)。
    """

    def matches(self, ctx: EvalContext) -> bool:
        pair_registry = ctx.pair_registry
        sports_store = ctx.sports_store
        if pair_registry is None or sports_store is None:
            return False
        game_id = pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is None:
            return False
        state = sports_store.get(game_id)
        if state is None:
            return False
        return bool(getattr(state, "live", False)) and not bool(getattr(state, "ended", False))
