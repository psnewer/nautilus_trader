"""LowerTierCheck —— 只允许 OE 原始赛事名命中低级别赛事。"""

from __future__ import annotations

from src.arbitrage.strategy.competition_tier import lower_tier_oe_competition
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class LowerTierCheck(Check):
    """Challenger/WTA/UTR/ITF 命中; 缺 OE 原始赛事名时 fail-closed。"""

    def passes(self, ctx: EvalContext) -> bool:
        return lower_tier_oe_competition(ctx) is not None
