"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check 算好,如 MeanRebateCheck),按 venue 算 size:
  - POLYMARKET: size = share(PM 单位是 shares,1 share = $1 win)
  - ORBITEXCH: size = share / price(stake = share / odds,确保 win = share)

**当前是 log-only**(Q-D1 阶段 A;Q-D1=B 后改为 `await ctx.submitter(order)` 真下单,
跟 SkipExecutionClient 配合做 mock 全成 smoke)。
"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class PlaceBetsAction(Action):
    """通用下单 Action。"""

    def __init__(self, share: float = 22.5) -> None:
        self._share = float(share)

    async def execute(self, ctx: EvalContext) -> None:
        legs = ctx.scratch.get("legs", [])
        if not legs:
            _LOG.debug(f"PlaceBets: pair={ctx.pair_id} no legs (Check 未写),skip")
            return

        rate = ctx.scratch.get("mean_rebate_rate")
        submitter = ctx.submitter      # slice 10a:None → log-only fallback;否则真出单
        mode = "submit" if submitter is not None else "smoke"
        _LOG.info(
            f"PlaceBets[{mode}]: pair={ctx.pair_id} legs={len(legs)} share={self._share} "
            f"mean_rebate_rate={rate}",
        )
        for leg in legs:
            size = _compute_size(leg["venue"], self._share, leg["price"])
            spec = {
                "instrument_id": leg["instrument_id"],
                "side": leg["side"],
                "qty": size,
                "price": leg["price"],
            }
            if submitter is not None:
                # slice 10a(#50):真出单(SkipExecutionClient 在 debug.skip_execution=true 下兜底 mock 全成)
                await submitter(spec)
            else:
                # log-only fallback(无 submitter 注入;单测 / smoke)
                _LOG.info(
                    f"  would submit: instrument={leg['instrument_id']} side={leg['side']} "
                    f"role={leg['role']} venue={leg['venue']} qty={size:.4f} price={leg['price']}",
                )


def _compute_size(venue: str, share: float, price: float) -> float:
    """PM=share;OE=share/price(stake)。Mean rebate 数学:确保 win 一致。"""
    if venue == "POLYMARKET":
        return share
    if venue == "ORBITEXCH":
        if price <= 0:
            return 0.0
        return share / price
    return 0.0
