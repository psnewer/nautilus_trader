"""按当前较低 PM ask 的偏移价生成赛前单腿挂单。"""

from __future__ import annotations

import math

from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import pm_ask_by_outcome
from src.arbitrage.strategy.checks.start_price_below import _pm_instrument_for_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class PreGameOrderCheck(Check):
    """选择当前 PM ask 唯一较低的 outcome,并按该 ask 的偏移价下单。"""

    def __init__(self, offset: float) -> None:
        value = float(offset)
        if isinstance(offset, bool) or not math.isfinite(value) or not 0 < value < 1:
            raise ValueError("pre_game_order: offset must be a finite number in (0, 1)")
        self._offset = value

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        prices = []
        for outcome in VALID_OUTCOMES:
            price = pm_ask_by_outcome(ctx, outcome)
            if price is None:
                return False
            prices.append((price, outcome))
        prices.sort()
        current_ask, outcome = prices[0]
        if math.isclose(prices[1][0], current_ask, abs_tol=1e-12):
            return False

        order_price = current_ask * (1.0 - self._offset)
        instrument_id = _pm_instrument_for_outcome(ctx, outcome)
        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if (
            not math.isfinite(order_price)
            or not 0 < order_price < 1
            or instrument_id is None
            or not math.isfinite(share)
            or share <= 0
        ):
            return False
        qty = qty_from_share(POLYMARKET, share, order_price)
        if not math.isfinite(qty) or qty <= 0:
            return False

        ctx.scratch["legs"] = [{
            "instrument_id": str(instrument_id),
            "venue": POLYMARKET,
            "side": "BUY",
            "price": order_price,
            "prob": order_price,
            "role": outcome,
            "claim": outcome,
            "qty": qty,
            "share_if_wins": share,
        }]
        return True
