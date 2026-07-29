"""Execution 市价提交价格辅助函数测试。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.execution.market_price import worst_decimal_lay_price


class _Book:
    def __init__(self, probabilities):
        self._levels = [SimpleNamespace(price=value) for value in probabilities]

    def bids(self):
        return self._levels


@pytest.mark.parametrize("venue", ["ORBITEXCH", "SHARPEXCH"])
def test_worst_decimal_lay_price_uses_last_bid_level(venue):
    book = _Book([0.5, 0.4, 0.25])

    assert worst_decimal_lay_price(book, venue, 2.0) == pytest.approx(4.0)


@pytest.mark.parametrize("book", [None, _Book([])])
def test_worst_decimal_lay_price_falls_back_without_depth(book):
    assert worst_decimal_lay_price(book, "ORBITEXCH", 2.5) == pytest.approx(2.5)
