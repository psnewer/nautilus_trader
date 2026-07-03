"""MeanRebateRecoveryCheck:缺口补齐 legs + 修复后 rebate 阈值。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.identifiers import InstrumentId

from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


class _Qty:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _position(iid, qty, price):
    return SimpleNamespace(instrument_id=iid, quantity=_Qty(qty), avg_px_open=price)


class _InstrumentIdOnlyInfoMap(dict):
    def get(self, key, default=None):
        if not isinstance(key, InstrumentId):
            raise TypeError("expected InstrumentId")
        return super().get(key, default)


def _ctx(*, books, infos, positions):
    snap = OpportunitySnapshot(
        pair_id="p",
        instrument_ids=list(books.keys()),
        order_books=books,
        instrument_info=infos,
        positions=positions,
    )
    return EvalContext(pair_id="p", snapshot=snap)


def test_recovery_adds_missing_outcome_to_max_actual_share():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", qty=5.0, price=0.50)],
    )

    ok = MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx)

    assert ok is True
    assert ctx.scratch["legs"] == [{
        "instrument_id": "A.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.50,
        "prob": 0.50,
        "role": "away",
        "qty": 5.0,
    }]
    assert ctx.scratch["mean_rebate_recovery"]["target_share"] == 5.0


def test_recovery_rejects_when_repaired_rebate_below_threshold():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.90),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", qty=5.0, price=0.50)],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_recovery_noops_when_no_missing_share():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[
            _position("H.POLYMARKET", qty=5.0, price=0.50),
            _position("A.POLYMARKET", qty=5.0, price=0.50),
        ],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_recovery_uses_oe_qty_for_gross_payout_gap():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.ORBITEXCH": _fake_book(2.0),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.ORBITEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", qty=5.0, price=0.50)],
    )

    ok = MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx)

    assert ok is True
    assert ctx.scratch["legs"][0]["instrument_id"] == "A.ORBITEXCH"
    assert ctx.scratch["legs"][0]["qty"] == 2.5


def test_recovery_uses_sharpexch_qty_for_gross_payout_gap():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.SHARPEXCH": _fake_book(2.0),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.SHARPEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", qty=5.0, price=0.50)],
    )

    ok = MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx)

    assert ok is True
    assert ctx.scratch["legs"][0]["instrument_id"] == "A.SHARPEXCH"
    assert ctx.scratch["legs"][0]["venue"] == "SHARPEXCH"
    assert ctx.scratch["legs"][0]["qty"] == 2.5


def test_recovery_handles_typed_instrument_info_map():
    home = InstrumentId.from_str("H.POLYMARKET")
    away = InstrumentId.from_str("A.POLYMARKET")
    books = {
        home: _fake_book(0.50),
        away: _fake_book(0.50),
    }
    infos = _InstrumentIdOnlyInfoMap({
        home: {"selection_role": "home"},
        away: {"selection_role": "away"},
    })
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position(home, qty=5.0, price=0.50)],
    )

    ok = MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx)

    assert ok is True
    assert ctx.scratch["legs"][0]["instrument_id"] == away


def test_recovery_rejects_when_existing_avg_price_missing():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("H.POLYMARKET", qty=5.0, price=0.0)],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is False
    assert "legs" not in ctx.scratch
