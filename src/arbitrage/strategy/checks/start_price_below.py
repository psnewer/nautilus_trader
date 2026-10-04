"""StartPriceBelowCheck —— 用低于阈值的 PM 开赛价生成单腿计划。"""

from __future__ import annotations

import math

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class StartPriceBelowCheck(Check):
    """选择唯一最低且严格小于阈值的 outcome, 并按其 start_price 生成 PM BUY。"""

    def __init__(self, price: float) -> None:
        value = float(price)
        if isinstance(price, bool) or not math.isfinite(value) or not 0 < value < 1:
            raise ValueError("start_price_below: price must be a finite number in (0, 1)")
        self._price = value

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        state = PairPriceStore(ctx.cache).get(ctx.pair_id)
        if state is None or set(state.start_price) != set(state.outcomes):
            return False

        eligible = []
        for outcome in state.outcomes:
            value = float(state.start_price[outcome])
            if not math.isfinite(value) or not 0 < value < self._price:
                continue
            eligible.append((value, outcome))
        if not eligible:
            return False

        eligible.sort()
        start_price, outcome = eligible[0]
        if len(eligible) > 1 and math.isclose(eligible[1][0], start_price, abs_tol=1e-12):
            return False

        instrument_id = _pm_instrument_for_outcome(ctx, outcome)
        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if instrument_id is None or not math.isfinite(share) or share <= 0:
            return False
        qty = qty_from_share(POLYMARKET, share, start_price)
        if not math.isfinite(qty) or qty <= 0:
            return False

        ctx.scratch["legs"] = [{
            "instrument_id": str(instrument_id),
            "venue": POLYMARKET,
            "side": "BUY",
            "price": start_price,
            "prob": start_price,
            "role": outcome,
            "claim": outcome,
            "qty": qty,
            "share_if_wins": share,
        }]
        return True


def _pm_instrument_for_outcome(ctx: EvalContext, outcome: str) -> InstrumentId | None:
    found = []
    for value in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id):
        if venue_id_from_instrument_id(value) != POLYMARKET:
            continue
        instrument_id = (
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        )
        instrument = ctx.cache.instrument(instrument_id)
        info = getattr(instrument, "info", None) or {}
        candidate = str(info.get("claim") or info.get("selection_role") or "").lower()
        if candidate == outcome:
            found.append(instrument_id)
    return found[0] if len(found) == 1 else None
