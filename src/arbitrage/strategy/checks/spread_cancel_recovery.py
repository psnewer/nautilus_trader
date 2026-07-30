"""挂单价格接近当前 ask 时触发 pair 级撤单补偿。"""

from __future__ import annotations

import math

from src.arbitrage.common.venues import leg_economics
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.leg_plan import resolve_side_and_price


class SpreadCancelRecoveryCheck(Check):
    """任一挂单与对应当前可执行 ask 的价差小于阈值时请求撤销整个 pair。"""

    def __init__(self, spread: float) -> None:
        self._spread = float(spread)
        if not math.isfinite(self._spread) or not 0 < self._spread < 1:
            raise ValueError(
                f"spread must be a finite value in (0, 1), got {spread!r}",
            )

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False

        current_asks = _current_asks_by_execution_leg(ctx)
        if not current_asks:
            return False

        matches = []
        legs = []
        for instrument_id in pair_instrument_ids(ctx):
            for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                side = _side_name(getattr(order, "side", None))
                order_price = _order_price(order)
                current_leg = current_asks.get((str(instrument_id), side))
                if order_price is None or current_leg is None:
                    continue
                ask_price = float(current_leg["ask_price"])
                difference = abs(order_price - ask_price)
                if difference >= self._spread:
                    continue
                quantity = _number(getattr(order, "quantity", None))
                if quantity is None or quantity <= 0:
                    continue
                matches.append({
                    "client_order_id": str(getattr(order, "client_order_id", "") or ""),
                    "instrument_id": str(instrument_id),
                    "side": side,
                    "order_price": order_price,
                    "ask_price": ask_price,
                    "difference": difference,
                })
                leg = dict(current_leg["leg"])
                leg["qty"] = quantity
                leg["share_if_wins"] = leg_economics(
                    leg["venue"],
                    ask_price,
                    quantity,
                    is_lay=side == "SELL",
                ).share_if_wins
                legs.append(leg)

        if not matches:
            return False

        ctx.scratch["legs"] = legs
        ctx.scratch["cancel_pair_orders"] = {
            "reason": "spread_cancel_recovery",
            "spread": self._spread,
            "matches": matches,
        }
        return True


def _current_asks_by_execution_leg(ctx: EvalContext) -> dict[tuple[str, str], dict]:
    """把语义报价腿投影为实际提交 instrument/side，供挂单做同口径比较。"""
    result = {}
    for legs in quote_legs_by_outcome(ctx).values():
        for leg in legs:
            venue = str(leg.get("venue") or "")
            side, price = resolve_side_and_price(leg, venue, {})
            if price is None or price <= 0:
                continue
            instrument_id = str(leg.get("exec_instrument_id") or leg.get("instrument_id") or "")
            if instrument_id:
                result[(instrument_id, side)] = {
                    "ask_price": float(price),
                    "leg": leg,
                }
    return result


def _side_name(value) -> str:
    return str(getattr(value, "name", value) or "").upper()


def _order_price(order) -> float | None:
    has_price = getattr(order, "has_price", False)
    if callable(has_price):
        has_price = has_price()
    if not has_price:
        return None
    price = getattr(order, "price", None)
    if price is None:
        return None
    return _number(price)


def _number(value) -> float | None:
    if value is None:
        return None
    as_double = getattr(value, "as_double", None)
    try:
        return float(as_double()) if callable(as_double) else float(value)
    except (TypeError, ValueError):
        return None
