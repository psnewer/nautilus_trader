"""从 OpportunitySnapshot 构建策略检查共用的可执行报价腿。"""

from __future__ import annotations

from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.common.venues import venue_id_from_instrument_id


def quote_legs_by_outcome(snapshot) -> dict[str, list[dict]]:
    """按 snapshot 声明的 outcome 收集带执行重定向信息的报价腿。"""
    valid_outcomes = tuple(getattr(snapshot, "outcomes", None) or ("home", "away"))
    result: dict[str, list[dict]] = {}
    for instrument_id in snapshot.instrument_ids:
        info = snapshot.instrument_info.get(instrument_id) or {}
        claim = str(info.get("claim") or "").lower()
        outcome = claim or str(info.get("selection_role") or "").lower()
        if outcome not in valid_outcomes:
            continue
        book = snapshot.order_books.get(instrument_id)
        if book is None:
            continue
        venue = venue_of(instrument_id)
        price = best_ask(book)
        if price is None or price <= 0:
            continue
        quote_claim = str(info.get("quote_claim") or "yes").lower()
        probability = to_probability(venue, price, quote_claim)
        if probability <= 0:
            continue
        leg = {
            "instrument_id": instrument_id,
            "venue": venue,
            "side": "BUY",
            "price": price,
            "prob": probability,
            "role": outcome,
        }
        if claim:
            leg["claim"] = claim
        exec_instrument_id = info.get("exec_instrument_id")
        if exec_instrument_id:
            leg["lay_price"] = price
            leg["exec_instrument_id"] = str(exec_instrument_id)
        result.setdefault(outcome, []).append(leg)
    return result


def best_ask(book) -> float | None:
    """从 NT OrderBook 或测试字典读取 best ask。"""
    fn = getattr(book, "best_ask_price", None)
    if callable(fn):
        try:
            value = fn()
            return float(value) if value is not None else None
        except Exception:
            return None
    if isinstance(book, dict):
        return book.get("ask") or book.get("best_ask")
    return None


def to_probability(venue: str, price: float, claim: str = "yes") -> float:
    try:
        return probability_from_price(venue, price, claim or "yes")
    except KeyError:
        return 0.0


def venue_of(instrument_id) -> str:
    return venue_id_from_instrument_id(instrument_id)
