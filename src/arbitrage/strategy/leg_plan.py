"""
leg_plan —— 语义 leg → (side, price, qty) 的基础解析 + instrument constraints 读取。

单一真理源(#277):`CandiSelectAction` 的最小下注门控与 `PlaceBetsAction` 的下单转换
必须共用同一份解析,否则门控放行的腿与实际提交的订单会漂移。
只含纯转换与 Cache 读取,不含市价覆盖 / 减仓拆单等提交期才发生的变换。
"""

from __future__ import annotations

from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import qty_from_share


def try_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_instrument_id(value):
    from nautilus_trader.model.identifiers import InstrumentId

    return value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))


def object_value(value) -> float | None:
    if value is None:
        return None
    for method in ("as_double", "as_decimal"):
        fn = getattr(value, method, None)
        if callable(fn):
            return float(fn())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_synthetic_decimal_no(leg: dict, venue: str) -> bool:
    instrument_id = str(leg.get("instrument_id") or "")
    exec_instrument_id = str(leg.get("exec_instrument_id") or "")
    if not exec_instrument_id or exec_instrument_id == instrument_id:
        return False
    try:
        return is_decimal_odds_venue(venue)
    except KeyError:
        return False


def leg_bid_price(leg: dict) -> float | None:
    for key in ("bid", "best_bid", "lay", "lay_price"):
        value = try_float(leg.get(key))
        if value is not None and value > 0:
            return value
    return None


def execution_side(leg: dict, venue: str) -> str:
    if is_synthetic_decimal_no(leg, venue):
        return "SELL"
    return str(leg.get("side") or "BUY").upper()


def resolve_side_and_price(
    leg: dict,
    venue: str,
    price_overrides: dict[str, float],
) -> tuple[str, float | None]:
    """将语义 leg 转成最终 submit side/price。

    只有带执行重定向的 decimal 合成 no instrument 才转成 SELL @ bid/lay。
    真实 instrument 即使逻辑 claim=no 也保持正常 BUY/BACK。
    """
    venue_key = str(venue).upper()
    if venue_key in price_overrides:
        price = price_overrides[venue_key]
        return execution_side(leg, venue), price
    if is_synthetic_decimal_no(leg, venue):
        return "SELL", leg_bid_price(leg)
    return str(leg.get("side") or "BUY").upper(), try_float(leg.get("price"))


def compute_size(venue: str, share: float, price: float) -> float:
    """PM=share;OE/SE=share/price(stake)。Mean rebate 数学:确保 win 一致。"""
    if price <= 0:
        return 0.0
    try:
        return qty_from_share(venue, share, price)
    except KeyError:
        return 0.0


def compute_leg_size(
    leg: dict,
    venue: str,
    price: float,
    qty_overrides: dict[str, float],
) -> float | None:
    """按优先级决定最终 qty:显式 override > leg qty > leg share_if_wins。"""
    if venue in qty_overrides:
        return qty_overrides[venue]
    if is_synthetic_decimal_no(leg, venue) and leg.get("share_if_wins") is not None:
        return compute_size(venue, float(leg["share_if_wins"]), price)
    if "qty" in leg:
        return float(leg["qty"])
    leg_share = leg.get("share_if_wins")
    if leg_share is not None:
        return compute_size(venue, float(leg_share), price)
    return None


def instrument_constraints(cache, instrument_id) -> dict:
    instrument = cache.instrument(as_instrument_id(instrument_id))
    info = getattr(instrument, "info", None) or {}
    return {
        "min_quantity": object_value(getattr(instrument, "min_quantity", None)),
        "min_notional": object_value(getattr(instrument, "min_notional", None)),
        "min_buy_quantity": object_value(info.get("min_buy_quantity")),
        "min_buy_notional": object_value(info.get("min_buy_notional")),
        "size_increment": object_value(getattr(instrument, "size_increment", None)),
    }
