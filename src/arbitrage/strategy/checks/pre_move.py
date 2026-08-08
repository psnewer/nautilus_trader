"""PreMoveCheck —— pre_rebate 赛前追"赔率变大(概率变小)"的腿(#326)。

读 `PairPriceStore.first_price` 与当前 PM best ask 概率向量,对每个 outcome 算**绝对概率跌幅**
`first - now`;跌幅最大且 `>= move_threshold` 的 outcome = mover(赔率变大),写单条 PM `BUY`
leg(量 = `strategy_defaults["share"]`)到 `ctx.scratch["legs"]`。

**解耦**:本 Check 只**读** first_price,不负责采集——采集条件/late-join 治理归 §3.8.2。
first_price 空 → 安全 no-op(不追腿)。赛前/赛中门由 self_hits `in_game` 负责,本 Check 不自判 phase。
详细设计见 strategy §3.10。
"""

from __future__ import annotations

import logging

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


class PreMoveCheck(Check):
    """某 outcome 现价较 first_price 绝对概率跌幅 >= move_threshold → 写单条 PM BUY leg。"""

    def __init__(self, move_threshold: float) -> None:
        self._move_threshold = float(move_threshold)

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        state = PairPriceStore(ctx.cache).get(ctx.pair_id)
        if state is None or not state.first_price:
            return False  # 无赛前基准 → 不追(采集是 §3.8.2 的责任,与本 Check 解耦)

        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if share <= _EPS:
            return False

        pm_legs = _pm_legs_by_outcome(ctx)

        best_outcome = None
        best_drop = float("-inf")
        best_leg = None
        for outcome, first_prob in state.first_price.items():
            leg = pm_legs.get(outcome)
            if leg is None or first_prob is None:
                continue
            now_prob = leg.get("prob")
            if now_prob is None or float(now_prob) <= 0:
                continue
            drop = float(first_prob) - float(now_prob)
            # 跌幅达阈(赔率变大),取最大跌幅的 outcome;等于阈值命中(>=)。
            if drop + _EPS >= self._move_threshold and drop > best_drop:
                best_outcome, best_drop, best_leg = outcome, drop, leg

        if best_leg is None:
            return False

        qty = qty_from_share(POLYMARKET, share, float(best_leg["price"]))
        if qty <= _EPS:
            return False

        leg = dict(best_leg)
        leg["side"] = "BUY"
        leg["qty"] = qty
        leg["share_if_wins"] = share
        ctx.scratch["legs"] = [leg]
        _LOG.info(
            f"PreMove: pair={ctx.pair_id} buy outcome={best_outcome} "
            f"first={state.first_price.get(best_outcome)} now={best_leg.get('prob')} "
            f"drop={best_drop:.4f} >= {self._move_threshold} qty={qty}",
        )
        return True


def _pm_legs_by_outcome(ctx: EvalContext) -> dict[str, dict]:
    """每个 outcome 取其 PM 报价腿(每 pair 每 outcome 唯一 PM instrument)。"""
    result: dict[str, dict] = {}
    for outcome, legs in quote_legs_by_outcome(ctx).items():
        for leg in legs:
            if str(leg.get("venue", "")).upper() == POLYMARKET:
                result[outcome] = leg
                break
    return result
