"""PreMoveCheck —— pre_rebate 买入概率下行的 outcome。

当前 PM best ask 必须是满足 commission 区间的完整概率向量。任一 outcome 从历史最高价
下跌比例达阈值时买它自身；从历史最低价上涨比例达阈值时买它的互补 outcome。多个信号同时命中时取变化比例最大者。
"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_COMMISSION_MIN = 0.95
_COMMISSION_MAX = 1.05
_LOG = logging.getLogger(__name__)


class PreMoveCheck(Check):
    """某 outcome 相对历史极值的变化比例达阈值 → 写概率下行方 PM BUY leg。"""

    def __init__(self, move_threshold: float) -> None:
        self._move_threshold = float(move_threshold)

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        state = PairPriceStore(ctx.cache).get(ctx.pair_id)
        if state is None or not state.up_price or not state.down_price:
            return False

        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if share <= _EPS:
            return False

        pm_legs = _pm_legs_by_outcome(ctx)
        outcomes = tuple(state.up_price)
        current = _current_probabilities(pm_legs, outcomes)
        if current is None:
            return False

        best_signal = _best_signal(state, outcomes, current, self._move_threshold)
        if best_signal is None:
            return False

        movement_ratio, buy_outcome, source_outcome, direction = best_signal
        best_leg = pm_legs[buy_outcome]

        qty = qty_from_share(POLYMARKET, share, float(best_leg["price"]))
        if qty <= _EPS:
            return False

        leg = dict(best_leg)
        leg["side"] = "BUY"
        leg["qty"] = qty
        leg["share_if_wins"] = share
        ctx.scratch["legs"] = [leg]
        _LOG.info(
            f"PreMove: pair={ctx.pair_id} buy outcome={buy_outcome} "
            f"source_outcome={source_outcome} direction={direction} "
            f"movement_ratio={movement_ratio:.4f} >= {self._move_threshold} qty={qty}",
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


def _opposite_outcome(outcomes: tuple[str, ...], outcome: str) -> str | None:
    if len(outcomes) != 2:
        return None
    return outcomes[1] if outcomes[0] == outcome else outcomes[0]


def _current_probabilities(
    pm_legs: dict[str, dict],
    outcomes: tuple[str, ...],
) -> dict[str, float] | None:
    if set(pm_legs) != set(outcomes):
        return None
    current = {}
    for outcome in outcomes:
        value = float(pm_legs[outcome].get("prob") or 0.0)
        if not math.isfinite(value) or value <= 0:
            return None
        current[outcome] = value
    if not _COMMISSION_MIN <= sum(current.values()) <= _COMMISSION_MAX:
        return None
    return current


def _best_signal(state, outcomes, current, threshold):
    best = None
    for outcome in outcomes:
        now = current[outcome]
        high = float(state.up_price[outcome])
        low = float(state.down_price[outcome])
        signals = []
        if math.isfinite(high) and high > _EPS:
            signals.append(((high - now) / high, outcome, outcome, "down"))
        opposite = _opposite_outcome(outcomes, outcome)
        if opposite is not None and math.isfinite(low) and low > _EPS:
            signals.append(((now - low) / low, opposite, outcome, "up"))
        for signal in signals:
            movement_ratio = signal[0]
            if movement_ratio + _EPS >= threshold and (
                best is None or movement_ratio > best[0]
            ):
                best = signal
    return best
