"""
MeanRebateCheck —— 平均返水套利检查(slice 9 / #49;#228 outcome 化)。

算法(对应 requirements §8):
  1. 按 outcome 标签分组(#228:`info.get("claim") or info.get("selection_role")`,
     合法集合固定为 `[yes,no]`),
     每方向取所有 venue 中概率最小者(即 best_ask 最便宜方;decimal claim=no 腿的
     概率经 `probability_from_price(venue, price, claim)` 取补集)
  2. mean_rebate_rate = 1 - sum_outcomes(min_prob)
  3. >= `min_rate` 阈值 → True;同时写带 `share_if_wins` 的 `ctx.scratch["legs"]`
     供 Action 消费。`share` 可在本 Check params 中显式配置;未配置则读 Web Arbitrage 默认。

输出 legs 形态(每方向一条):
  {
    "instrument_id": InstrumentId,
    "venue": str,
    "side": "BUY",
    "price": float (原始价 — PM 是 0-1 概率,OE 是 stake odds;no 腿 = lay 原值),
    "prob": float,
    "role": canonical outcome 标签 yes/no,
    "share_if_wins": float,
    # 合成 no 腿另带(place_bets SELL@lay 转换 + 执行重定向):
    "claim": "no", "lay_price": float, "exec_instrument_id": str,
  }

PlaceBetsAction 用 leg 自带 `share_if_wins` 经 Venue Registry 推 qty。
"""

from __future__ import annotations

from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class MeanRebateCheck(Check):
    """平均返水检查。"""

    def __init__(self, min_rate: float = 0.01, share: float | None = None) -> None:
        self._min_rate = float(min_rate)
        self._share = float(share) if share is not None else None

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False

        valid_outcomes = VALID_OUTCOMES
        legs_by_outcome = quote_legs_by_outcome(ctx)

        # 必须 yes/no 集合齐；每方向至少 2 条可比腿。
        if sorted(legs_by_outcome.keys()) != sorted(valid_outcomes):
            return False
        for outcome in valid_outcomes:
            if len(legs_by_outcome[outcome]) < 2:
                return False  # 缺一边 → 算不了 mean_rebate

        # 每方向取 min(prob);相同 prob 时按 venue capability 稳定排序。
        chosen_legs = []
        total_prob = 0.0
        for outcome in sorted(valid_outcomes):
            cands = legs_by_outcome[outcome]
            best = min(cands, key=lambda lg: (lg["prob"], venue_preference_rank(lg["venue"])))
            chosen_legs.append(best)
            total_prob += best["prob"]

        mean_rebate_rate = 1.0 - total_prob
        if mean_rebate_rate < self._min_rate:
            return False

        share = self._configured_share(ctx)
        if share <= 0:
            return False

        for leg in chosen_legs:
            leg["share_if_wins"] = share

        ctx.scratch["legs"] = chosen_legs
        ctx.scratch["mean_rebate_rate"] = mean_rebate_rate
        return True

    def _configured_share(self, ctx: EvalContext) -> float:
        if self._share is not None:
            return self._share
        return float((ctx.strategy_defaults or {}).get("share") or 0.0)
