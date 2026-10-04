"""StartPriceBelowCheck 按记录的 PM start_price 生成最低价单腿。"""

import asyncio

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.checks.start_price_below import StartPriceBelowCheck
from tests.arbitrage.strategy._live_state import live_context


def _ctx(prices):
    ctx = live_context(
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        strategy_defaults={"share": 10.0, "max_leg_share": 10.0},
    )
    store = PairPriceStore(ctx.cache)
    store.initialize("p", ("yes", "no"))
    if prices is not None:
        store.capture_start("p", prices)
    return ctx


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_start_price_below_writes_pm_buy_at_recorded_start_price():
    ctx = _ctx({"yes": 0.35, "no": 0.65})

    assert StartPriceBelowCheck(price=0.4).passes(ctx) is True

    assert ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.35,
        "prob": 0.35,
        "role": "yes",
        "claim": "yes",
        "qty": 10.0,
        "share_if_wins": 10.0,
    }]


def test_start_price_below_then_place_bets_applies_spread_to_final_order_price():
    ctx = _ctx({"yes": 0.35, "no": 0.65})

    assert StartPriceBelowCheck(price=0.4).passes(ctx) is True
    _run(PlaceBetsAction(spread=0.05).execute(ctx))

    order = ctx.scratch["execution_plan"].orders[0]
    assert order.spec["instrument_id"] == "Y.POLYMARKET"
    assert order.spec["side"] == "BUY"
    assert order.spec["price"] == pytest.approx(0.30)


@pytest.mark.parametrize(
    ("prices", "threshold"),
    [
        (None, 0.4),
        ({"yes": 0.40, "no": 0.60}, 0.4),
        ({"yes": 0.45, "no": 0.55}, 0.4),
        ({"yes": 0.30, "no": 0.30}, 0.4),
    ],
)
def test_start_price_below_fails_closed_without_unique_eligible_start(prices, threshold):
    assert StartPriceBelowCheck(price=threshold).passes(_ctx(prices)) is False


@pytest.mark.parametrize("price", [0, 1, -0.1, float("nan"), True])
def test_start_price_below_rejects_invalid_threshold(price):
    with pytest.raises(ValueError, match="price must be a finite number"):
        StartPriceBelowCheck(price=price)
