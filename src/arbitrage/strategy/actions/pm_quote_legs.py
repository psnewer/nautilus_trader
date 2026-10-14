"""以当前 PM best ask 生成完整二元 BUY 腿,供后续 Action 筛选和改价。"""

from __future__ import annotations

import math

from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


class PmQuoteLegsAction(Action):
    """生成每个 outcome 唯一的 PM 实时报价腿,不做机会率或比分判断。"""

    async def execute(self, ctx: EvalContext) -> None:
        ctx.scratch["legs"] = []
        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if not math.isfinite(share) or share <= 0:
            return

        legs_by_outcome = quote_legs_by_outcome(ctx)
        legs = []
        for outcome in VALID_OUTCOMES:
            pm_legs = [
                leg
                for leg in legs_by_outcome.get(outcome, ())
                if leg.get("venue") == POLYMARKET
            ]
            if len(pm_legs) != 1:
                return
            leg = dict(pm_legs[0])
            price = float(leg.get("price") or 0.0)
            if not math.isfinite(price) or not 0 < price < 1:
                return
            qty = qty_from_share(POLYMARKET, share, price)
            if not math.isfinite(qty) or qty <= 0:
                return
            leg.update({
                "qty": qty,
                "share_if_wins": share,
            })
            legs.append(leg)

        ctx.scratch["legs"] = legs
