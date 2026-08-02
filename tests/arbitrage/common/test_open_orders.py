"""Pair open-order baseline 摘要契约。"""

from types import SimpleNamespace

from src.arbitrage.common.open_orders import orders_digest
from src.arbitrage.common.open_orders import pair_open_orders_digest


def _order(
    client_order_id,
    *,
    filled="0",
    leaves="10",
    status="ACCEPTED",
    event_count=1,
    ts_last=1,
):
    return SimpleNamespace(
        client_order_id=client_order_id,
        venue_order_id=f"venue-{client_order_id}",
        instrument_id="A.POLYMARKET",
        side="BUY",
        status=status,
        quantity="10",
        filled_qty=filled,
        leaves_qty=leaves,
        price="0.4",
        has_price=True,
        event_count=lambda: event_count,
        ts_last=ts_last,
    )


class _Cache:
    def __init__(self, orders):
        self.orders = list(orders)

    def orders_open(self, *, instrument_id):
        return [
            order
            for order in self.orders
            if str(order.instrument_id) == str(instrument_id)
        ]


def test_digest_is_stable_across_order_object_identity_and_cache_order():
    first = _Cache([_order("a"), _order("b")])
    second = _Cache([_order("b"), _order("a")])

    assert pair_open_orders_digest(first, ["A.POLYMARKET"]) == pair_open_orders_digest(
        second,
        ["A.POLYMARKET"],
    )


def test_digest_changes_when_open_order_progress_changes():
    cache = _Cache([_order("a")])
    baseline = pair_open_orders_digest(cache, ["A.POLYMARKET"])
    cache.orders = [_order("a", filled="2", leaves="8")]

    assert pair_open_orders_digest(cache, ["A.POLYMARKET"]) != baseline


def test_digest_changes_when_open_order_disappears():
    cache = _Cache([_order("a")])
    baseline = pair_open_orders_digest(cache, ["A.POLYMARKET"])
    cache.orders = []

    assert pair_open_orders_digest(cache, ["A.POLYMARKET"]) != baseline


def test_digest_supports_order_has_price_method_and_unpriced_orders():
    priced = _order("a")
    priced.has_price = lambda: True
    unpriced = _order("b")
    unpriced.has_price = lambda: False
    del unpriced.price

    pair_open_orders_digest(_Cache([priced, unpriced]), ["A.POLYMARKET"])


def test_orders_digest_detects_event_generation_change_without_economic_change():
    baseline = orders_digest([_order("a", event_count=1, ts_last=1)])

    assert orders_digest([_order("a", event_count=2, ts_last=2)]) != baseline
