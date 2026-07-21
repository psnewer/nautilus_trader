"""从 OpportunitySnapshot 构建策略检查共用的可执行报价腿。"""

from __future__ import annotations

from src.arbitrage.common.venues import price_from_probability
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
        # #256:book 存的是 best_ask 处的隐含概率(写侧已用同一 quote_claim 换算,
        # decimal venue 见 `orbitexch/data.py::oe_runner_to_book_deltas`),读侧不再需要
        # `probability_from_price` 二次转换,直接就是 probability。
        probability = best_ask(book)
        if probability is None or probability <= 0:
            continue
        quote_claim = str(info.get("quote_claim") or "yes").lower()
        price = to_price(venue, probability, quote_claim)
        if price is None:
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
    """从 NT OrderBook 或测试字典读取 best ask。

    #256:返回值就是隐含概率(decimal venue 的 book 写侧已按 quote_claim 换算,见
    `orbitexch/data.py::oe_runner_to_book_deltas`;probability venue 本就是概率,不变)。
    测试字典('ask'/'best_ask' 键)同样应传概率而非原始赔率。
    """
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


def worst_ask(book) -> float | None:
    """从 NT OrderBook 读最深(最差)ask 档的隐含概率(#256 续,市价单用)。

    `book.asks()` 按价格升序(best 在前),`[-1]` 即最差档。纯 NT book 接口——不支持
    `best_ask` 那种测试字典 fallback,因为深度语义只有真实 `OrderBook` 才有意义。
    """
    fn = getattr(book, "asks", None)
    if not callable(fn):
        return None
    try:
        levels = fn()
    except Exception:
        return None
    if not levels:
        return None
    try:
        value = levels[-1].price
        return float(value) if value is not None else None
    except Exception:
        return None


def to_price(venue: str, probability: float, claim: str = "yes") -> float | None:
    """隐含概率 → 真实价格,`price_from_probability` 的容错包装(#256,取代旧
    `to_probability`——书方向反了,现在读到的已经是概率,要还原回真实赔率/概率)。"""
    try:
        return price_from_probability(venue, probability, claim or "yes")
    except (KeyError, ZeroDivisionError):
        return None


def venue_of(instrument_id) -> str:
    return venue_id_from_instrument_id(instrument_id)
