"""spread_cancel_recovery 策略检查。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.checks.spread_cancel_recovery import SpreadCancelRecoveryCheck
from tests.arbitrage.strategy._live_state import live_context


class _Book:
    def __init__(self, *, bid: float, ask: float):
        self._bid = bid
        self._ask = ask

    def best_bid_price(self):
        return self._bid

    def best_ask_price(self):
        return self._ask


def _order(
    instrument_id: str,
    side: str,
    price: float,
    client_order_id: str = "O-1",
    quantity: float = 10.0,
):
    return SimpleNamespace(
        instrument_id=instrument_id,
        side=side,
        price=price,
        has_price=True,
        client_order_id=client_order_id,
        quantity=quantity,
    )


def test_matches_open_buy_order_close_to_current_bid():
    ctx = live_context(
        books={"H.POLYMARKET": _Book(bid=0.45, ask=0.70)},
        infos={"H.POLYMARKET": {"selection_role": "home", "claim": "yes"}},
        orders=[_order("H.POLYMARKET", "BUY", 0.445)],
    )

    assert SpreadCancelRecoveryCheck(spread=0.01).passes(ctx) is True
    request = ctx.scratch["cancel_pair_orders"]
    assert request["reason"] == "spread_cancel_recovery"
    assert request["matches"][0]["book_side"] == "bid"
    assert request["matches"][0]["current_price"] == pytest.approx(0.45)
    assert request["matches"][0]["difference"] == pytest.approx(0.005)
    assert ctx.scratch["legs"] == [{
        "instrument_id": "H.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.45,
        "prob": 0.45,
        "role": "yes",
        "claim": "yes",
        "qty": 10.0,
        "share_if_wins": 10.0,
    }]


def test_matches_inventory_converted_polymarket_sell_close_to_current_ask():
    ctx = live_context(
        books={"H.POLYMARKET": _Book(bid=0.40, ask=0.62)},
        infos={"H.POLYMARKET": {"selection_role": "home", "claim": "yes"}},
        orders=[_order("H.POLYMARKET", "SELL", 0.625)],
    )

    assert SpreadCancelRecoveryCheck(spread=0.01).passes(ctx) is True
    match = ctx.scratch["cancel_pair_orders"]["matches"][0]
    assert match["book_side"] == "ask"
    assert match["current_price"] == pytest.approx(0.62)
    assert match["difference"] == pytest.approx(0.005)
    assert ctx.scratch["legs"][0]["side"] == "SELL"


def test_decimal_sell_compares_same_instrument_ask_probability_difference():
    ctx = live_context(
        books={
            "H.ORBITEXCH": _Book(
                bid=probability_from_price(ORBITEXCH, 1.85),
                ask=probability_from_price(ORBITEXCH, 1.88),
            ),
        },
        infos={
            "H.ORBITEXCH": {"selection_role": "home", "claim": "yes"},
        },
        orders=[_order("H.ORBITEXCH", "SELL", 1.91)],
    )

    assert SpreadCancelRecoveryCheck(spread=0.01).passes(ctx) is True
    match = ctx.scratch["cancel_pair_orders"]["matches"][0]
    assert match["book_side"] == "ask"
    assert match["current_price"] == pytest.approx(1.88)
    assert abs(match["order_price"] - match["current_price"]) == pytest.approx(0.03)
    assert match["order_probability"] == pytest.approx(1.0 / 1.91)
    assert match["current_probability"] == pytest.approx(1.0 / 1.88)
    assert match["difference"] == pytest.approx(abs(1.0 / 1.88 - 1.0 / 1.91))
    assert ctx.scratch["legs"][0]["side"] == "SELL"
    assert ctx.scratch["legs"][0]["qty"] == 10.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == pytest.approx(18.8)


def test_no_open_order_or_difference_not_below_spread_does_not_match():
    base = {
        "books": {"H.POLYMARKET": _Book(bid=0.45, ask=0.46)},
        "infos": {"H.POLYMARKET": {"selection_role": "home", "claim": "yes"}},
    }
    assert SpreadCancelRecoveryCheck(spread=0.01).passes(live_context(**base)) is False

    ctx = live_context(
        **base,
        orders=[_order("H.POLYMARKET", "BUY", 0.40)],
    )
    assert SpreadCancelRecoveryCheck(spread=0.01).passes(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


@pytest.mark.parametrize("spread", [0, -0.01, 1.0, float("inf"), float("nan")])
def test_rejects_invalid_spread(spread):
    with pytest.raises(ValueError, match="spread must be"):
        SpreadCancelRecoveryCheck(spread=spread)
