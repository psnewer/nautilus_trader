"""SharpExch execution 纯映射测试。

只测 NT order + instrument → SE legacy order,不触发 Playwright / 下单。
"""

from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.sharpexch.execution import bet_order_progress
from nautilus_trader.adapters.sharpexch.execution import current_bets_to_fills
from nautilus_trader.adapters.sharpexch.execution import current_bets_to_positions
from nautilus_trader.adapters.sharpexch.execution import normalize_current_bets_to_usd
from nautilus_trader.adapters.sharpexch.execution import nt_order_to_legacy_order
from nautilus_trader.adapters.sharpexch.execution import parse_cancel_bets_response
from nautilus_trader.adapters.sharpexch.execution import parse_place_bets_response
from nautilus_trader.adapters.sharpexch.execution import se_balance_to_account_balances
from nautilus_trader.adapters.sharpexch.execution import se_order_to_cancel_bets_payload
from nautilus_trader.adapters.sharpexch.execution import se_order_to_place_bets_payload
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments.betting import null_handicap


def _inst(market_id="1.259502313", selection_id="111", handicap=None):
    return SimpleNamespace(
        market_id=market_id,
        selection_id=selection_id,
        selection_handicap=handicap if handicap is not None else null_handicap(),
    )


def _nt(side=OrderSide.BUY, price=2.5, qty=10.0):
    return SimpleNamespace(instrument_id="x.SHARPEXCH", side=side, price=price, quantity=qty)


def test_balance_to_account_balances_uses_usd_free_equals_total():
    balances = se_balance_to_account_balances(37.49)

    assert len(balances) == 1
    balance = balances[0]
    assert balance.total.as_double() == pytest.approx(37.49)
    assert balance.free.as_double() == pytest.approx(37.49)
    assert balance.locked.as_double() == pytest.approx(0.0)
    assert str(balance.total.currency) == "USD"


def test_buy_maps_to_back():
    order = nt_order_to_legacy_order(_nt(side=OrderSide.BUY), _inst())
    assert order is not None
    assert order.venue == "sharpexch"
    assert order.side == "BACK"
    assert order.market_id == "1.259502313"
    assert order.selection_id == "111"
    assert order.price == 2.5
    assert order.size == 10.0
    assert order.order_type == "GTC"


def test_place_bets_payload_keeps_usd_size():
    order = nt_order_to_legacy_order(_nt(side=OrderSide.BUY, price=2.345, qty=20.0), _inst())
    payload, bet_uuid = se_order_to_place_bets_payload(order, fx=1.25, timestamp_ms=123456, uuid_suffix="hy64m")

    bet = payload["1.259502313"][0]
    assert bet_uuid == "1.259502313_111_0__123456-hy64m"
    assert bet["betUuid"] == bet_uuid
    assert bet["selectionId"] == 111
    assert bet["handicap"] == 0
    assert bet["side"] == "BACK"
    assert bet["price"] == 2.35
    assert bet["size"] == pytest.approx(20.0)
    assert bet["persistenceType"] == "LAPSE"
    assert bet["page"] == "competition"
    assert bet["showLayOddsEnabled"] is False
    assert "fillOrKill" not in bet


@pytest.mark.parametrize(
    ("side", "expected_price"),
    [("BACK", 1.01), ("LAY", 2.5)],
)
def test_market_order_price_is_applied_at_place_bets_boundary(side, expected_price):
    order = nt_order_to_legacy_order(
        _nt(side=OrderSide.BUY if side == "BACK" else OrderSide.SELL, price=2.5),
        _inst(),
    )
    payload, _ = se_order_to_place_bets_payload(
        order,
        fx=1.0,
        timestamp_ms=123456,
        market_order_enabled=True,
    )

    assert payload["1.259502313"][0]["price"] == expected_price


def test_place_bets_payload_rejects_bad_fx():
    order = nt_order_to_legacy_order(_nt(), _inst())
    with pytest.raises(ValueError, match="Invalid fx"):
        se_order_to_place_bets_payload(order, fx=0, timestamp_ms=123456)


def test_place_bets_payload_clamps_odds_and_marks_fok():
    order = nt_order_to_legacy_order(_nt(side=OrderSide.SELL, price=0.5, qty=7.0), _inst())
    order = order.__class__(**{**order.__dict__, "order_type": "FOK"})
    payload, _ = se_order_to_place_bets_payload(order, fx=1.0, timestamp_ms=123456)
    bet = payload["1.259502313"][0]

    assert bet["side"] == "LAY"
    assert bet["price"] == 1.01
    assert bet["fillOrKill"] is True


def test_place_bets_payload_clamps_high_odds():
    order = nt_order_to_legacy_order(_nt(price=1200.0), _inst())
    payload, _ = se_order_to_place_bets_payload(order, fx=1.0, timestamp_ms=123456)
    assert payload["1.259502313"][0]["price"] == 1000


def test_parse_place_bets_response_success_uses_matching_offer_id():
    response = {
        "1.259502313": {
            "status": "OK",
            "offerIds": {"bet-uuid": 221973242},
        },
    }

    out = parse_place_bets_response(response, "1.259502313", "bet-uuid")

    assert out == {
        "success": True,
        "venue_order_id": "221973242",
        "message": "Order placed successfully",
    }


def test_parse_place_bets_response_success_falls_back_to_first_offer_id():
    response = {"1.259502313": {"status": "OK", "offerIds": {"other": 221973242}}}
    out = parse_place_bets_response(response, "1.259502313", "bet-uuid")
    assert out["success"] is True
    assert out["venue_order_id"] == "221973242"


def test_parse_place_bets_response_success_falls_back_to_bet_uuid_without_offer_ids():
    response = {"1.259502313": {"status": "OK", "offerIds": {}}}
    out = parse_place_bets_response(response, "1.259502313", "bet-uuid")
    assert out["success"] is True
    assert out["venue_order_id"] == "bet-uuid"


def test_parse_place_bets_response_global_errors():
    empty = parse_place_bets_response({}, "M1", "B1")
    invalid = parse_place_bets_response(["bad"], "M1", "B1")
    assert empty["message"] == "No response" and empty["transport_unknown"] is True
    assert invalid["message"] == "Invalid response" and invalid["transport_unknown"] is True
    assert parse_place_bets_response({"error": "csrf"}, "M1", "B1")["message"] == "csrf"
    assert parse_place_bets_response({"code": 405, "message": "rejected"}, "M1", "B1")["message"] == "rejected"


def test_parse_place_bets_response_market_errors():
    out = parse_place_bets_response({"M1": {"status": "FAIL"}}, "M1", "B1")
    assert out == {"success": False, "venue_order_id": None, "message": "FAIL"}

    out = parse_place_bets_response({}, "M1", "B1")
    assert out["message"] == "No response"

    out = parse_place_bets_response({"M2": {"status": "OK"}}, "M1", "B1")
    assert out["message"] == "No response for market M1"
    assert out["transport_unknown"] is True


def test_cancel_bets_payload_uses_market_and_offer_id():
    payload = se_order_to_cancel_bets_payload("1.259502313", "221973242")
    assert payload == {
        "1.259502313": [
            {
                "offerId": "221973242",
                "betType": "EXCHANGE",
            },
        ],
    }


def test_cancel_bets_payload_uses_full_open_bet_when_available():
    bet = {
        "offerId": 22155999,
        "marketId": "1.259494210",
        "selectionId": 8960879,
        "side": "BACK",
        "sizeRemaining": 12,
        "betType": "EXCHANGE",
    }

    payload = se_order_to_cancel_bets_payload("1.259494210", "22155999", bet=bet)

    assert payload == {"1.259494210": [bet]}
    assert payload["1.259494210"][0] is not bet


def test_cancel_bets_payload_requires_market_and_offer_id():
    assert se_order_to_cancel_bets_payload("", "221973242") is None
    assert se_order_to_cancel_bets_payload("1.259502313", "") is None


def test_parse_cancel_bets_response_success_and_failures():
    assert parse_cancel_bets_response({"status": "OK"}) == {
        "success": True,
        "message": "Order cancelled via API",
    }
    assert parse_cancel_bets_response({}) == {
        "success": False,
        "message": "Cancel failed",
        "transport_unknown": True,
    }
    assert parse_cancel_bets_response(["bad"]) == {
        "success": False,
        "message": "Invalid response",
        "transport_unknown": True,
    }
    assert parse_cancel_bets_response({"error": "csrf"}) == {
        "success": False,
        "message": "csrf",
    }


def test_sell_maps_to_lay():
    order = nt_order_to_legacy_order(_nt(side=OrderSide.SELL), _inst())
    assert order is not None
    assert order.side == "LAY"


def test_null_handicap_coerced_to_zero():
    order = nt_order_to_legacy_order(_nt(), _inst())
    assert order is not None
    assert order.handicap == 0.0


def test_real_handicap_preserved():
    order = nt_order_to_legacy_order(_nt(), _inst(handicap=2.5))
    assert order is not None
    assert order.handicap == 2.5


def test_missing_market_or_selection_returns_none():
    assert nt_order_to_legacy_order(_nt(), _inst(market_id="")) is None
    assert nt_order_to_legacy_order(_nt(), _inst(selection_id="")) is None


def test_current_bets_amount_fields_normalized_to_usd():
    bets = [{
        "offerId": "1",
        "selectionId": "111",
        "sizeMatched": "7.00",
        "sizeRemaining": "3.00",
        "sizePlaced": "10.00",
        "liability": "7.00",
        "profitNet": "9.10",
        "averagePrice": "2.30",
        "price": "2.30",
    }]

    out = normalize_current_bets_to_usd(bets, fx=1.3)[0]

    assert out["sizeMatched"] == pytest.approx(9.1)
    assert out["sizeRemaining"] == pytest.approx(3.9)
    assert out["sizePlaced"] == pytest.approx(13.0)
    assert out["liability"] == pytest.approx(9.1)
    assert out["profitNet"] == pytest.approx(11.83)
    assert out["averagePrice"] == "2.30"
    assert out["price"] == "2.30"


def test_current_bets_amount_normalization_ignores_non_numeric_and_bad_fx():
    bets = [{"sizeMatched": "", "liability": "7.00", "averagePrice": "2.30"}]
    out = normalize_current_bets_to_usd(bets, fx=0)[0]
    assert out["sizeMatched"] == ""
    assert out["liability"] == pytest.approx(7.0)
    assert out["averagePrice"] == "2.30"


def _bet(offer_id="221832455", size_matched=0.0, avg_price=0.0):
    return {
        "offerId": offer_id,
        "selectionId": "111",
        "sizeMatched": size_matched,
        "averagePrice": avg_price,
        "profitNet": "0.00",
        "liability": "0.00",
    }


def test_empty_snapshot_no_fills():
    assert current_bets_to_fills([]) == []


def test_unmatched_bet_no_fill():
    assert current_bets_to_fills([_bet(size_matched=0.0, avg_price=0.0)]) == []


def test_matched_emits_cumulative_size_matched():
    fills = current_bets_to_fills([_bet(size_matched=5.0, avg_price=2.0)])
    assert len(fills) == 1
    assert fills[0] == {
        "offer_id": "221832455",
        "avg_price": 2.0,
        "size_matched": 5.0,
    }


def test_larger_snapshot_still_emits_full_cumulative_size():
    fills = current_bets_to_fills([_bet(size_matched=8.0, avg_price=2.0)])
    assert len(fills) == 1
    assert fills[0]["size_matched"] == 8.0


def test_matched_size_without_price_skipped():
    assert current_bets_to_fills([_bet(size_matched=5.0, avg_price=0.0)]) == []


def test_missing_offer_id_skipped():
    assert current_bets_to_fills([_bet(offer_id="", size_matched=5.0, avg_price=2.0)]) == []


def _pbet(offer_id="221832455", size_remaining=0.0, size_matched=0.0, avg_price=0.0, **extra):
    bet = {
        "offerId": offer_id,
        "marketId": "1.259502313",
        "selectionId": "111",
        "side": "BACK",
        "sizeRemaining": size_remaining,
        "sizeMatched": size_matched,
        "averagePrice": avg_price,
        "price": 2.5,
    }
    bet.update(extra)
    return bet


def test_progress_missing_offer_id_none():
    assert bet_order_progress(_pbet(offer_id="")) is None


def test_progress_accepted_when_only_remaining():
    progress = bet_order_progress(_pbet(size_remaining=7.0, size_matched=0.0))
    assert progress["status"] == "accepted"
    assert progress["original_qty"] == 7.0
    assert progress["filled_qty"] == 0.0


def test_progress_partially_filled():
    progress = bet_order_progress(_pbet(size_remaining=3.0, size_matched=5.0, avg_price=2.0))
    assert progress["status"] == "partially_filled"
    assert progress["original_qty"] == 8.0
    assert progress["filled_qty"] == 5.0
    assert progress["avg_px"] == 2.0


def test_progress_filled_when_no_remaining():
    progress = bet_order_progress(_pbet(size_remaining=0.0, size_matched=5.0, avg_price=2.0))
    assert progress["status"] == "filled"
    assert progress["filled_qty"] == 5.0


def test_progress_unknown_when_both_zero():
    assert bet_order_progress(_pbet())["status"] == "unknown"


def test_progress_exposes_bet_side_and_ids():
    progress = bet_order_progress(_pbet(side="LAY", size_remaining=7.0))
    assert progress["side"] == "LAY"
    assert progress["market_id"] == "1.259502313"
    assert progress["selection_id"] == "111"
    assert progress["price"] == 2.5


def test_progress_prefers_size_placed_for_original_qty():
    progress = bet_order_progress(_pbet(size_remaining=2.0, size_matched=3.0, sizePlaced=10.0))
    assert progress["original_qty"] == 10.0


def test_progress_falls_back_to_sum_without_size_placed():
    bet = _pbet(size_remaining=2.0, size_matched=3.0)
    bet.pop("sizePlaced", None)
    assert bet_order_progress(bet)["original_qty"] == 5.0


def _pos_bet(market, sel, side, matched, avg, remaining=0.0):
    return {
        "offerId": f"{market}-{sel}-{side}-{int(matched)}",
        "marketId": market,
        "selectionId": sel,
        "side": side,
        "sizeMatched": matched,
        "averagePrice": avg,
        "sizeRemaining": remaining,
    }


def test_positions_single_back_long():
    out = current_bets_to_positions([_pos_bet("M1", "S1", "BACK", 10.0, 2.0)])
    assert len(out) == 1
    position = out[0]
    assert position["side"] == "LONG"
    assert position["qty"] == pytest.approx(10.0)
    assert position["avg_px"] == pytest.approx(2.0)


def test_positions_two_back_size_weighted_avg():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 100.0, 2.0),
        _pos_bet("M1", "S1", "BACK", 50.0, 2.2),
    ])
    position = out[0]
    assert position["qty"] == pytest.approx(150.0)
    assert position["avg_px"] == pytest.approx((100 * 2.0 + 50 * 2.2) / 150)


def test_positions_mixed_back_lay_dominant_side_avg():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 100.0, 2.0),
        _pos_bet("M1", "S1", "LAY", 40.0, 3.0),
    ])
    position = out[0]
    assert position["side"] == "LONG"
    assert position["qty"] == pytest.approx(60.0)
    assert position["avg_px"] == pytest.approx(2.0)


def test_positions_lay_dominant_short():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "LAY", 80.0, 3.0),
        _pos_bet("M1", "S1", "BACK", 30.0, 2.0),
    ])
    position = out[0]
    assert position["side"] == "SHORT"
    assert position["qty"] == pytest.approx(50.0)
    assert position["avg_px"] == pytest.approx(3.0)


def test_positions_net_zero_skipped():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 30.0, 2.0),
        _pos_bet("M1", "S1", "LAY", 30.0, 2.5),
    ])
    assert out == []


def test_positions_unmatched_skipped():
    out = current_bets_to_positions([_pos_bet("M1", "S1", "BACK", 0.0, 0.0, remaining=7.0)])
    assert out == []
