"""PreMoveCheck: 赛前追赔率变大(概率变小)的腿(#326)。strategy-4.pre_rebate.2。"""

from unittest.mock import MagicMock

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.checks.pre_move import PreMoveCheck
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.strategy._live_state import StrategyTestCache


class _Cache(StrategyTestCache):
    """StrategyTestCache(instrument/order_book)+ PairPriceStore 的 kv 接口。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._kv = {}

    def get(self, key):
        return self._kv.get(key)

    def add(self, key, value):
        self._kv[key] = value

    def delete(self, key):
        self._kv.pop(key, None)


def _fake_book(ask):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask)
    return book


def _ctx(*, now, first, share=5.0):
    infos = {"Y.POLYMARKET": {"claim": "yes"}, "N.POLYMARKET": {"claim": "no"}}
    books = {
        "Y.POLYMARKET": _fake_book(now["yes"]),
        "N.POLYMARKET": _fake_book(now["no"]),
    }
    cache = _Cache(books=books, infos=infos)
    registry = PairRegistry()
    registry.register("p", list(infos.keys()))
    store = PairPriceStore(cache)
    store.initialize("p", ["yes", "no"])
    if first is not None:
        store.capture_first("p", first)
    return EvalContext(
        pair_id="p", cache=cache, pair_registry=registry, strategy_defaults={"share": share},
    )


def test_buys_outcome_whose_probability_dropped_enough():
    ctx = _ctx(now={"yes": 0.38, "no": 0.62}, first={"yes": 0.50, "no": 0.50})

    assert PreMoveCheck(move_threshold=0.1).passes(ctx) is True
    legs = ctx.scratch["legs"]
    assert len(legs) == 1
    leg = legs[0]
    assert leg["instrument_id"] == "Y.POLYMARKET"
    assert leg["side"] == "BUY"
    assert leg["claim"] == "yes"
    assert leg["prob"] == 0.38
    assert leg["qty"] == 5.0          # PM:qty_from_share = share
    assert leg["share_if_wins"] == 5.0


def test_no_hit_when_drop_below_threshold():
    ctx = _ctx(now={"yes": 0.38, "no": 0.62}, first={"yes": 0.50, "no": 0.50})
    assert PreMoveCheck(move_threshold=0.15).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_equal_threshold_hits():
    ctx = _ctx(now={"yes": 0.40, "no": 0.60}, first={"yes": 0.50, "no": 0.50})
    assert PreMoveCheck(move_threshold=0.10).passes(ctx) is True
    assert ctx.scratch["legs"][0]["claim"] == "yes"


def test_no_first_price_is_noop():
    ctx = _ctx(now={"yes": 0.38, "no": 0.62}, first=None)  # first_price 空
    assert PreMoveCheck(move_threshold=0.1).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_zero_share_fails_closed():
    ctx = _ctx(now={"yes": 0.38, "no": 0.62}, first={"yes": 0.50, "no": 0.50}, share=0.0)
    assert PreMoveCheck(move_threshold=0.1).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_picks_largest_drop_outcome():
    # yes 跌 0.05(不够),no 跌 0.20(够)→ 买 no
    ctx = _ctx(now={"yes": 0.45, "no": 0.30}, first={"yes": 0.50, "no": 0.50})
    assert PreMoveCheck(move_threshold=0.1).passes(ctx) is True
    assert ctx.scratch["legs"][0]["claim"] == "no"
