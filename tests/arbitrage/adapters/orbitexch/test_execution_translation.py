"""Gap C(#63):OE exec `nt_order_to_legacy_order` 纯映射(NT Order → executor 旧 Order)。

真 `executor.place_order`(Playwright)+ `_connect`(登录/page/WS)经 /live-test 验(真钱,
skip_execution=false);本文件只测无 Playwright 的翻译逻辑。
"""

from types import SimpleNamespace

from nautilus_trader.adapters.orbitexch.execution import bet_order_progress
from nautilus_trader.adapters.orbitexch.execution import current_bets_to_fills
from nautilus_trader.adapters.orbitexch.execution import nt_order_to_legacy_order
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments.betting import null_handicap


def _inst(market_id="1-258848983", selection_id="39835947", handicap=None):
    return SimpleNamespace(
        market_id=market_id,
        selection_id=selection_id,
        selection_handicap=handicap if handicap is not None else null_handicap(),
    )


def _nt(side=OrderSide.BUY, price=2.5, qty=10.0):
    return SimpleNamespace(instrument_id="x.ORBITEXCH", side=side, price=price, quantity=qty)


def test_buy_maps_to_back():
    o = nt_order_to_legacy_order(_nt(side=OrderSide.BUY), _inst())
    assert o.side.value == "BACK"
    assert o.market_id == "1-258848983" and o.selection_id == "39835947"
    assert o.price == 2.5 and o.size == 10.0
    assert o.order_type.value == "GTC"
    assert o.venue.value == "orbitexch"


def test_sell_maps_to_lay():
    assert nt_order_to_legacy_order(_nt(side=OrderSide.SELL), _inst()).side.value == "LAY"


def test_null_handicap_coerced_to_zero():
    """null_handicap(NT sentinel -9999999.0)→ 0.0(match-odds 无 handicap)。"""
    assert nt_order_to_legacy_order(_nt(), _inst()).handicap == 0.0


def test_real_handicap_preserved():
    assert nt_order_to_legacy_order(_nt(), _inst(handicap=2.5)).handicap == 2.5


def test_missing_market_or_selection_returns_none():
    assert nt_order_to_legacy_order(_nt(), _inst(market_id="")) is None
    assert nt_order_to_legacy_order(_nt(), _inst(selection_id="")) is None


# ─── current_bets_to_fills:CURRENT_BETS 快照 → 成交 delta(回执核心)─────────────
#
# 实测 schema(2026-06-06 抓帧,offerId==venue_order_id):unmatched item =
#   {"offerId":"221832455","selectionId":"19924823","averagePrice":0.0,"profitNet":"0.00","liability":"0.00"}
# (+ 派生用 marketId/sizeRemaining/sizeMatched)。matched 态填充值待真成交确认。

def _bet(offer_id="221832455", size_matched=0.0, avg_price=0.0):
    return {"offerId": offer_id, "selectionId": "19924823",
            "sizeMatched": size_matched, "averagePrice": avg_price,
            "profitNet": "0.00", "liability": "0.00"}


def test_empty_snapshot_no_fills():
    assert current_bets_to_fills([], {}) == []


def test_unmatched_bet_no_fill():
    """实测 unmatched 帧(sizeMatched=0/averagePrice=0)→ 不产成交。"""
    assert current_bets_to_fills([_bet(size_matched=0.0, avg_price=0.0)], {}) == []


def test_newly_matched_emits_full_delta():
    fills = current_bets_to_fills([_bet(size_matched=5.0, avg_price=2.0)], {})
    assert len(fills) == 1
    f = fills[0]
    assert f["offer_id"] == "221832455"
    assert f["delta_qty"] == 5.0 and f["avg_price"] == 2.0 and f["size_matched"] == 5.0


def test_incremental_match_emits_only_delta():
    """快照非增量:prev 累积 5,本次 8 → delta=3。"""
    fills = current_bets_to_fills([_bet(size_matched=8.0, avg_price=2.0)], {"221832455": 5.0})
    assert len(fills) == 1 and fills[0]["delta_qty"] == 3.0


def test_no_new_match_no_fill():
    """同一累积值再推一次 → 无新增 → 不重复发成交。"""
    assert current_bets_to_fills([_bet(size_matched=8.0, avg_price=2.0)], {"221832455": 8.0}) == []


def test_matched_size_without_price_skipped():
    """delta>0 但 averagePrice<=0(矛盾/未填价)→ 跳过(下帧价填充后再发)。"""
    assert current_bets_to_fills([_bet(size_matched=5.0, avg_price=0.0)], {}) == []


def test_missing_offer_id_skipped():
    assert current_bets_to_fills([_bet(offer_id="", size_matched=5.0, avg_price=2.0)], {}) == []


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
