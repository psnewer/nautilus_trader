"""OneSideRecoveryCheck —— 单冲机会触发的仓位补救检查。

先按 ``one_side_rebate`` 的口径确认当前盘口存在满足 ``min_rate`` 比较方向的单冲机会，
再按 ``mean_rebate_recovery`` 的口径把各 outcome 补到当前最大实际 share。

单冲检查只作为触发门，不向当前 condition 的 scratch 提交 candidates，避免后续 action
误执行单冲计划；命中后的唯一下单计划是 recovery legs。
"""

from __future__ import annotations

from dataclasses import replace

from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.checks.one_side_rebate import OneSideRebateCheck
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class OneSideRecoveryCheck(Check):
    """one_side_rebate 命中后，以当前最大持仓为目标执行 mean recovery。"""

    def __init__(
        self,
        min_rate: float = 0.01,
        min_repaired_rebate: float = -0.05,
        force: bool = False,
        less: bool = False,
    ) -> None:
        if not isinstance(less, bool):
            raise ValueError("one_side_recovery: less must be a boolean")
        self._min_rate = float(min_rate)
        self._less = less
        probe_min_rate = float("-inf") if less else self._min_rate
        self._one_side = OneSideRebateCheck(min_rate=probe_min_rate)
        self._recovery = MeanRebateRecoveryCheck(
            min_repaired_rebate=min_repaired_rebate,
            force=force,
        )

    def passes(self, ctx: EvalContext) -> bool:
        # one_side_rebate 仅负责确认触发条件。使用隔离 scratch，不能让它生成的
        # candidates 被 recovery action 链消费。
        probe_ctx = replace(ctx, scratch={})
        if not self._one_side.passes(probe_ctx):
            return False
        candidates = probe_ctx.scratch["candidates"]
        if self._less:
            candidates = [
                candidate for candidate in candidates if candidate["rate"] < self._min_rate
            ]
            if not candidates:
                return False
        if not self._recovery.passes(ctx):
            return False

        ctx.scratch["one_side_recovery"] = {
            "min_rate": self._min_rate,
            "candidate_count": len(candidates),
        }
        return True
