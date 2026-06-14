"""PM instrument 交易最小值映射。"""

from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument


def test_parse_polymarket_instrument_sets_min_quantity_from_order_min_size():
    token_id = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
    market_info = {
        "condition_id": "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
        "question": "Test market?",
        "minimum_tick_size": 0.001,
        "minimum_order_size": 5,
        "end_date_iso": "2025-12-31T00:00:00Z",
        "tokens": [{"token_id": token_id, "outcome": "Yes"}],
    }

    instrument = parse_polymarket_instrument(
        market_info=market_info,
        token_id=token_id,
        outcome="Yes",
        ts_init=0,
    )

    assert instrument.min_quantity.as_double() == 5.0
