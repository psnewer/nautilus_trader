"""挂单价格接近同 instrument 当前同侧挂单价时触发 pair 级撤单补偿。"""

from __future__ import annotations

import math

from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import price_from_probability
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import venue_of
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class SpreadCancelRecoveryCheck(Check):
    """任一 BUY/SELL 挂单接近同 instrument 的 best bid/ask 时撤销整个 pair。"""

    def __init__(self, spread: float) -> None:
        self._spread = float(spread)
        if not math.isfinite(self._spread) or not 0 < self._spread < 1:
            raise ValueError(
                f"spread must be a finite value in (0, 1), got {spread!r}",
            )

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False

        matches = []
        legs = []
        for instrument_id in pair_instrument_ids(ctx):
            instrument = ctx.cache.instrument(instrument_id)
            info = getattr(instrument, "info", None) or {}
            venue = venue_of(instrument_id)
            quote_claim = str(info.get("quote_claim") or "yes").lower()
            book = ctx.cache.order_book(instrument_id)
            for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                side = _side_name(getattr(order, "side", None))
                order_price = _order_price(order)
                book_side = "bid" if side == "BUY" else "ask" if side == "SELL" else None
                current_probability = _book_probability(book, book_side)
                if order_price is None or current_probability is None or not venue:
                    continue
                try:
                    current_price = price_from_probability(venue, current_probability, quote_claim)
                    order_probability = probability_from_price(venue, order_price, quote_claim)
                except (KeyError, ZeroDivisionError):
                    continue
                difference = abs(order_probability - current_probability)
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
                    "book_side": book_side,
                    "current_price": current_price,
                    "order_probability": order_probability,
                    "current_probability": current_probability,
                    "difference": difference,
                })
                outcome = str(info.get("claim") or info.get("selection_role") or "").lower()
                leg = {
                    "instrument_id": str(instrument_id),
                    "venue": venue,
                    "side": side,
                    "price": current_price,
                    "prob": current_probability,
                    "role": outcome,
                }
                if info.get("claim"):
                    leg["claim"] = str(info["claim"]).lower()
                leg["qty"] = quantity
                leg["share_if_wins"] = leg_economics(
                    venue,
                    current_price,
                    quantity,
                    is_lay=side == "SELL" and is_decimal_odds_venue(venue),
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


def _book_probability(book, book_side: str | None) -> float | None:
    if book is None or book_side is None:
        return None
    fn = getattr(book, f"best_{book_side}_price", None)
    if callable(fn):
        try:
            value = fn()
        except Exception:
            return None
    elif isinstance(book, dict):
        value = book.get(book_side) or book.get(f"best_{book_side}")
    else:
        return None
    probability = _number(value)
    if probability is None or not math.isfinite(probability) or not 0 < probability < 1:
        return None
    return probability


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
