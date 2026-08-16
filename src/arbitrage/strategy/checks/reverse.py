"""ReverseCheck —— 动态 standard 的回撤阈值检查。"""

from __future__ import annotations

import math

from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.queries.position_mode import instant_rebate


class ReverseCheck(Check):
    """即时返水率低于或等于 `rt * standard - retrieve` 时命中。"""

    def __init__(self, rt: float, retrieve: float) -> None:
        self._rt = float(rt)
        self._retrieve = float(retrieve)
        if not math.isfinite(self._rt) or not math.isfinite(self._retrieve):
            raise ValueError("rt and retrieve must be finite")

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.runtime_store is None or not ctx.strategy_id:
            return False
        current = instant_rebate(ctx)
        standard = ctx.runtime_store.get(ctx.strategy_id, ctx.pair_id, "standard")
        if current is None or standard is None:
            return False
        try:
            standard_rate = float(standard)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(standard_rate):
            return False
        threshold = self._rt * standard_rate - self._retrieve
        return math.isfinite(threshold) and current <= threshold
