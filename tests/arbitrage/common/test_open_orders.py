"""订单集合摘要契约(`orders_digest`;#317 pair 级 `pair_open_orders_digest` 已删)。"""

from types import SimpleNamespace

from src.arbitrage.common.open_orders import orders_digest


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


def test_digest_is_stable_across_order_object_identity_and_ordering():
    assert orders_digest([_order("a"), _order("b")]) == orders_digest([_order("b"), _order("a")])


def test_digest_changes_when_order_progress_changes():
    baseline = orders_digest([_order("a")])

    assert orders_digest([_order("a", filled="2", leaves="8")]) != baseline


def test_digest_changes_when_order_disappears():
    baseline = orders_digest([_order("a")])

    assert orders_digest([]) != baseline


def test_digest_supports_order_has_price_method_and_unpriced_orders():
    priced = _order("a")
    priced.has_price = lambda: True
    unpriced = _order("b")
    unpriced.has_price = lambda: False
    del unpriced.price

    orders_digest([priced, unpriced])


def test_orders_digest_detects_event_generation_change_without_economic_change():
    baseline = orders_digest([_order("a", event_count=1, ts_last=1)])

    assert orders_digest([_order("a", event_count=2, ts_last=2)]) != baseline
