"""price_change_recovery 策略检查。"""

from types import SimpleNamespace

from src.arbitrage.strategy.checks.price_change_recovery import PriceChangeRecoveryCheck
from tests.arbitrage.strategy._live_state import live_context


def _order(instrument_id: str, client_order_id: str = "O-1"):
    return SimpleNamespace(
        instrument_id=instrument_id,
        client_order_id=client_order_id,
    )


def _context(*, event_name=None, orders=None):
    infos = {
        "Y.POLYMARKET": {"claim": "yes"},
        "N.POLYMARKET": {"claim": "no"},
    }
    return live_context(
        infos=infos,
        orders=orders,
        event_name=event_name,
    )


def test_hits_when_order_book_trigger_has_open_order():
    ctx = _context(
        event_name="OrderBookDeltas",
        orders=[_order("N.POLYMARKET")],
    )

    assert PriceChangeRecoveryCheck().passes(ctx) is True
    assert ctx.scratch["cancel_pair_orders"] == {
        "reason": "price_change_recovery",
        "open_orders": [{
            "client_order_id": "O-1",
            "instrument_id": "N.POLYMARKET",
        }],
    }


def test_non_order_book_trigger_does_not_cancel():
    ctx = _context(event_name="SportsGameUpdate", orders=[_order("Y.POLYMARKET")])

    assert PriceChangeRecoveryCheck().passes(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


def test_price_change_without_open_order_does_not_hit():
    ctx = _context(event_name="OrderBookDeltas")

    assert PriceChangeRecoveryCheck().passes(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch
