"""市价提交在 execution 边界使用的价格辅助函数。"""

from __future__ import annotations

from src.arbitrage.common.venues import price_from_probability


def worst_decimal_lay_price(book, venue: str, fallback: float) -> float:
    """从真实 selection book 的最差 bid 还原当前最差 LAY 赔率。

    decimal venue 的真实 selection instrument 以隐含概率维护 book：
    LAY 档位写在 bid 侧，且 `bids()` 按概率从高到低排列。因此最后一档对应当前
    可成交深度中的最高（最差）LAY 赔率。缺 book 或数据异常时保留计划价。
    """
    if book is None:
        return fallback
    bids = getattr(book, "bids", None)
    if not callable(bids):
        return fallback
    try:
        levels = bids()
        if not levels:
            return fallback
        probability = float(levels[-1].price)
        price = float(price_from_probability(venue, probability, "yes"))
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return fallback
    return price if price > 1.0 else fallback
