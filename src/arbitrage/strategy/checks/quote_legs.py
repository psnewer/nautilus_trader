"""从 live Cache 构建策略检查共用的可执行报价腿。"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.venues import price_from_probability
from src.arbitrage.common.venues import venue_id_from_instrument_id


VALID_OUTCOMES = ("yes", "no")


def quote_legs_by_outcome(ctx) -> dict[str, list[dict]]:
    """按二元 outcome 收集带执行重定向信息的 live 报价腿。"""
    if ctx.cache is None or ctx.pair_registry is None:
        return {}
    result: dict[str, list[dict]] = {}
    for instrument_id in pair_instrument_ids(ctx):
        instrument = ctx.cache.instrument(instrument_id)
        info = getattr(instrument, "info", None) or {}
        claim = str(info.get("claim") or "").lower()
        outcome = claim or str(info.get("selection_role") or "").lower()
        if outcome not in VALID_OUTCOMES:
            continue
        book = ctx.cache.order_book(instrument_id)
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
            "instrument_id": str(instrument_id),
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


def pair_instrument_ids(ctx) -> tuple[InstrumentId, ...]:
    if ctx.pair_registry is None:
        return ()
    return tuple(
        value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        for value in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id)
    )


def pair_positions(ctx) -> list:
    """返回该 pair 当前 open positions，不持有跨 evaluation 的引用。"""
    if ctx.cache is None:
        return []
    positions = []
    seen = set()
    for instrument_id in pair_instrument_ids(ctx):
        for position in ctx.cache.positions_open(instrument_id=instrument_id) or ():
            key = str(getattr(position, "id", None) or getattr(position, "position_id", None) or id(position))
            if key in seen:
                continue
            seen.add(key)
            positions.append(position)
    return positions


def instrument_info(ctx, instrument_id) -> dict:
    if ctx.cache is None:
        return {}
    iid = instrument_id if isinstance(instrument_id, InstrumentId) else InstrumentId.from_str(str(instrument_id))
    instrument = ctx.cache.instrument(iid)
    return getattr(instrument, "info", None) or {}


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
