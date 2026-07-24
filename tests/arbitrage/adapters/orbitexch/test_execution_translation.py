"""OE execution 无状态请求翻译与 payload 映射。

真 `executor.place_order`(Playwright)+ `_connect`(登录/page/WS)经 /live-test 验(真钱,
skip_execution=false);本文件只测无 Playwright 的翻译逻辑。
"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.orbitexch.execution import bet_order_progress
from nautilus_trader.adapters.orbitexch.execution import current_bets_to_fills
from nautilus_trader.adapters.orbitexch.execution import normalize_current_bets_to_usd
from nautilus_trader.adapters.orbitexch.execution import nt_order_to_executor_order
from nautilus_trader.adapters.orbitexch.executor import OrbitExchExecutor
from nautilus_trader.adapters.orbitexch.executor import OrbitExchOrderRequest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.instruments.betting import null_handicap

from src.arbitrage.common.execution_config import ExecutionConfig


def _inst(market_id="1-258848983", selection_id="39835947", handicap=None):
    return SimpleNamespace(
        market_id=market_id,
        selection_id=selection_id,
        selection_handicap=handicap if handicap is not None else null_handicap(),
    )


def _nt(side=OrderSide.BUY, price=2.5, qty=10.0):
    return SimpleNamespace(
        client_order_id=ClientOrderId("O-1"),
        instrument_id="x.ORBITEXCH",
        side=side,
        price=price,
        quantity=qty,
    )


def _request(**overrides):
    values = {
        "client_order_id": "O-1",
        "market_id": "1.23",
        "selection_id": "456",
        "handicap": 0.0,
        "side": "BACK",
        "price": 2.5,
        "size": 20.0,
    }
    values.update(overrides)
    return OrbitExchOrderRequest(**values)


def test_buy_maps_to_back():
    o = nt_order_to_executor_order(_nt(side=OrderSide.BUY), _inst())
    assert o.side == "BACK"
    assert o.market_id == "1-258848983" and o.selection_id == "39835947"
    assert o.price == 2.5 and o.size == 10.0
    assert o.order_type == "GTC"
    assert o.client_order_id == "O-1"


def test_sell_maps_to_lay():
    assert nt_order_to_executor_order(_nt(side=OrderSide.SELL), _inst()).side == "LAY"


def test_null_handicap_coerced_to_zero():
    """null_handicap(NT sentinel -9999999.0)→ 0.0(match-odds 无 handicap)。"""
    assert nt_order_to_executor_order(_nt(), _inst()).handicap == 0.0


def test_real_handicap_preserved():
    assert nt_order_to_executor_order(_nt(), _inst(handicap=2.5)).handicap == 2.5


def test_missing_market_or_selection_returns_none():
    assert nt_order_to_executor_order(_nt(), _inst(market_id="")) is None
    assert nt_order_to_executor_order(_nt(), _inst(selection_id="")) is None


def test_executor_order_request_is_frozen():
    order = _request()

    with pytest.raises(FrozenInstanceError):
        order.size = 10.0


def test_oe_executor_converts_usd_size_to_gbp_payload():
    """adapter 外部 order.size 是 USD stake;OE placeBets payload size 是 GBP stake。"""
    import asyncio

    captured = {}

    class _Context:
        async def cookies(self):
            return [{"name": "CSRF-TOKEN", "value": "test-csrf-token"}]

    class _Page:
        def context(self):
            return _Context()

        async def evaluate(self, _script, payload):
            # payload 现在是 {"payload": ..., "csrfToken": ...}
            captured["payload"] = payload["payload"]
            bet_uuid = payload["payload"]["1.23"][0]["betUuid"]
            return {"1.23": {"status": "OK", "offerIds": {bet_uuid: "OID-1"}}}

    order = _request()
    executor = OrbitExchExecutor(config=ExecutionConfig(), fx_getter=lambda: 1.25)

    result = asyncio.run(executor.place_order(order, _Page()))

    assert result["success"] is True
    assert result["venue_order_id"] == "OID-1"
    assert captured["payload"]["1.23"][0]["size"] == 16.0
    assert not hasattr(executor, "_orders")
    assert not hasattr(executor, "_venue_orders")


def test_oe_executor_preserves_transport_error_marker():
    import asyncio

    class _Context:
        async def cookies(self):
            return [{"name": "CSRF-TOKEN", "value": "test-csrf-token"}]

    class _Page:
        def context(self):
            return _Context()

        async def evaluate(self, _script, payload):
            return {"error": "Failed to fetch", "_transport_error": True}

    order = _request()
    result = asyncio.run(OrbitExchExecutor(config=ExecutionConfig()).place_order(order, _Page()))

    assert result["success"] is False
    assert result["venue_response"]["_transport_error"] is True


def test_oe_executor_classifies_empty_response_as_transport_unknown():
    import asyncio

    class _Context:
        async def cookies(self):
            return [{"name": "CSRF-TOKEN", "value": "test-csrf-token"}]

    class _Page:
        def context(self):
            return _Context()

        async def evaluate(self, _script, payload):
            return None

    order = _request()
    result = asyncio.run(OrbitExchExecutor(config=ExecutionConfig()).place_order(order, _Page()))

    assert result["success"] is False
    assert result["venue_response"]["_transport_error"] is True


def test_cancel_all_unmatched_api_failure_has_no_ui_fallback():
    import asyncio

    class _Context:
        async def cookies(self):
            return [{"name": "CSRF-TOKEN", "value": "test-csrf-token"}]

    class _Page:
        def context(self):
            return _Context()

        async def evaluate(self, _script, _payload):
            return {"error": "cancel failed"}

        def locator(self, _selector):
            raise AssertionError("cancel-all must not fall back to UI")

    result = asyncio.run(OrbitExchExecutor(config=ExecutionConfig()).cancel_all_unmatched(_Page()))

    assert result["success"] is False
    assert result["message"] == "cancel failed"


def test_oe_executor_does_not_expose_legacy_take_at_market():
    executor = OrbitExchExecutor(config=ExecutionConfig())

    assert not hasattr(executor, "take_remaining_at_market")
    assert not hasattr(executor, "modify_size_and_take")


def test_current_bets_amount_fields_normalized_to_usd():
    bets = [{
        "offerId": "1",
        "selectionId": "2",
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


# ─── current_bets_to_fills:CURRENT_BETS 快照 → 累计成交(回执核心)─────────────
#
# 实测 schema(2026-06-06 抓帧,offerId==venue_order_id):unmatched item =
#   {"offerId":"221832455","selectionId":"19924823","averagePrice":0.0,"profitNet":"0.00","liability":"0.00"}
# (+ 派生用 marketId/sizeRemaining/sizeMatched)。matched 态填充值待真成交确认。

def _bet(offer_id="221832455", size_matched=0.0, avg_price=0.0):
    return {"offerId": offer_id, "selectionId": "19924823",
            "sizeMatched": size_matched, "averagePrice": avg_price,
            "profitNet": "0.00", "liability": "0.00"}


def test_empty_snapshot_no_fills():
    assert current_bets_to_fills([]) == []


def test_unmatched_bet_no_fill():
    """实测 unmatched 帧(sizeMatched=0/averagePrice=0)→ 不产成交。"""
    assert current_bets_to_fills([_bet(size_matched=0.0, avg_price=0.0)]) == []


def test_matched_emits_cumulative_size_matched():
    fills = current_bets_to_fills([_bet(size_matched=5.0, avg_price=2.0)])
    assert len(fills) == 1
    f = fills[0]
    assert f["offer_id"] == "221832455"
    assert f["avg_price"] == 2.0 and f["size_matched"] == 5.0
    assert "delta_qty" not in f


def test_larger_snapshot_still_emits_full_cumulative_size():
    """`sizeMatched` 是累计成交量,helper 不再持有 prevMatched 状态。"""
    fills = current_bets_to_fills([_bet(size_matched=8.0, avg_price=2.0)])
    assert len(fills) == 1 and fills[0]["size_matched"] == 8.0


def test_matched_size_without_price_skipped():
    """sizeMatched>0 但 averagePrice<=0(矛盾/未填价)→ 跳过(下帧价填充后再发)。"""
    assert current_bets_to_fills([_bet(size_matched=5.0, avg_price=0.0)]) == []


def test_missing_offer_id_skipped():
    assert current_bets_to_fills([_bet(offer_id="", size_matched=5.0, avg_price=2.0)]) == []


# ─── bet_order_progress:CURRENT_BETS 单 bet → 订单进度(reconcile 派生)────────────

def _pbet(offer_id="221832455", size_remaining=0.0, size_matched=0.0, avg_price=0.0, **extra):
    bet = {"offerId": offer_id, "marketId": "1-258848983", "selectionId": "19924823",
           "side": "BACK", "sizeRemaining": size_remaining, "sizeMatched": size_matched,
           "averagePrice": avg_price, "price": 2.5}
    bet.update(extra)
    return bet


def test_progress_missing_offer_id_none():
    assert bet_order_progress(_pbet(offer_id="")) is None


def test_progress_accepted_when_only_remaining():
    """实测 unmatched 态(sizeRemaining>0, sizeMatched=0)→ accepted。"""
    p = bet_order_progress(_pbet(size_remaining=7.0, size_matched=0.0))
    assert p["status"] == "accepted"
    assert p["original_qty"] == 7.0 and p["filled_qty"] == 0.0


def test_progress_partially_filled():
    p = bet_order_progress(_pbet(size_remaining=3.0, size_matched=5.0, avg_price=2.0))
    assert p["status"] == "partially_filled"
    assert p["original_qty"] == 8.0 and p["filled_qty"] == 5.0 and p["avg_px"] == 2.0


def test_progress_filled_when_no_remaining():
    p = bet_order_progress(_pbet(size_remaining=0.0, size_matched=5.0, avg_price=2.0))
    assert p["status"] == "filled" and p["filled_qty"] == 5.0


def test_progress_unknown_when_both_zero():
    assert bet_order_progress(_pbet())["status"] == "unknown"


def test_progress_exposes_bet_side_and_ids():
    """hardening:bet 自带 side/market/selection/price 直接透出(无需反查 NT order)。"""
    p = bet_order_progress(_pbet(side="LAY", size_remaining=7.0))
    assert p["side"] == "LAY"
    assert p["market_id"] == "1-258848983" and p["selection_id"] == "19924823"
    assert p["price"] == 2.5


def test_progress_prefers_size_placed_for_original_qty():
    """原始量优先用 OE 自带 sizePlaced(非 matched+remaining 兜底)。"""
    p = bet_order_progress(_pbet(size_remaining=2.0, size_matched=3.0, sizePlaced=10.0))
    assert p["original_qty"] == 10.0   # 10(sizePlaced),不是 5(2+3)


def test_progress_falls_back_to_sum_without_size_placed():
    bet = _pbet(size_remaining=2.0, size_matched=3.0)
    bet.pop("sizePlaced", None)
    assert bet_order_progress(bet)["original_qty"] == 5.0
