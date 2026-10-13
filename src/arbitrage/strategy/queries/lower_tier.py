"""赛事级别 self_hits 判据。"""

from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.competition_tier import lower_tier_oe_competition
from src.arbitrage.strategy.condition import EvalContext


class LowerTierQuery(StateQuery):
    """OE 原始 competition 名称命中低级别赛事标记。"""

    def matches(self, ctx: EvalContext) -> bool:
        return lower_tier_oe_competition(ctx) is not None
