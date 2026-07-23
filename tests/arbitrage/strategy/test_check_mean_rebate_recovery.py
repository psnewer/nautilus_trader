"""MeanRebateRecoveryCheck:缺口补齐 legs + 修复后 rebate 阈值。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.strategy._live_state import live_context


class _Qty:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _position(iid, qty, price, side=PositionSide.LONG):
    return SimpleNamespace(instrument_id=iid, quantity=_Qty(qty), avg_px_open=price, side=side)


class _InstrumentIdOnlyInfoMap(dict):
    def get(self, key, default=None):
        if not isinstance(key, InstrumentId):
            raise TypeError("expected InstrumentId")
        return super().get(key, default)


def _ctx(*, books, infos, positions, outcomes=None):
    return live_context(
        books=books,
        infos=infos,
        positions=positions,
        instrument_ids=list(infos.keys()),
    )


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
        "role": "no",
        "qty": 5.0,
        "claim": "no",
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
        "A.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.0)),
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
        "A.SHARPEXCH": _fake_book(probability_from_price(SHARPEXCH, 2.0)),
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
    assert ctx.scratch["legs"][0]["instrument_id"] == str(away)


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


def test_recovery_supports_pm_no_long_position_in_yes_no_pair():
    books = {
        "Y.POLYMARKET": _fake_book(0.50),
        "N.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "Y.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "N.POLYMARKET": {"selection_role": "home", "claim": "no"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("N.POLYMARKET", qty=5.0, price=0.50)],
        outcomes=["yes", "no"],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is True
    assert ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.50,
        "prob": 0.50,
        "role": "yes",
        "qty": 5.0,
        "claim": "yes",
    }]


def test_recovery_maps_decimal_short_position_to_no_outcome():
    books = {
        "Y.POLYMARKET": _fake_book(0.50),
        "N.POLYMARKET": _fake_book(0.50),
        "Y.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.0)),
    }
    infos = {
        "Y.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "N.POLYMARKET": {"selection_role": "home", "claim": "no"},
        "Y.ORBITEXCH": {"selection_role": "home", "claim": "yes"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position(
            "Y.ORBITEXCH",
            qty=2.5,
            price=2.0,
            side=PositionSide.SHORT,
        )],
        outcomes=["yes", "no"],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is True
    assert ctx.scratch["mean_rebate_recovery"]["target_share"] == 5.0
    assert ctx.scratch["legs"][0]["role"] == "yes"


def test_recovery_rejects_probability_short_position():
    books = {
        "Y.POLYMARKET": _fake_book(0.50),
        "N.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "Y.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "N.POLYMARKET": {"selection_role": "home", "claim": "no"},
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position(
            "Y.POLYMARKET",
            qty=5.0,
            price=0.50,
            side=PositionSide.SHORT,
        )],
        outcomes=["yes", "no"],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_recovery_decimal_no_candidate_keeps_lay_execution_fields():
    books = {
        "Y.POLYMARKET": _fake_book(0.50),
        "N.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.0, "no")),
    }
    infos = {
        "Y.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "N.ORBITEXCH": {
            "selection_role": "home",
            "claim": "no",
            "quote_claim": "no",
            "exec_instrument_id": "Y.ORBITEXCH",
        },
    }
    ctx = _ctx(
        books=books,
        infos=infos,
        positions=[_position("Y.POLYMARKET", qty=5.0, price=0.50)],
        outcomes=["yes", "no"],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is True
    assert ctx.scratch["legs"] == [{
        "instrument_id": "N.ORBITEXCH",
        "venue": "ORBITEXCH",
        "side": "BUY",
        "price": 2.0,
        "prob": 0.5,
        "role": "no",
        "qty": 2.5,
        "claim": "no",
        "lay_price": 2.0,
        "exec_instrument_id": "Y.ORBITEXCH",
    }]


# ── #262:前置门 —— 当前已达标就不该补救 ──────────────────────────
def test_no_recovery_when_current_rebate_already_meets_threshold():
    """两腿已接近平衡 → 当前最差 rebate 已达标 → **不触发**补救。

    实盘症状(2026-07-22):缺口仅折合 0.16 USD 时仍触发,算出的补单被 Risk 以
    `min_notional=12.00` 拒掉,而状态不变 → 每个 OBD tick 重复一次,日志刷屏。
    根因不是"单太小",而是缺「当前是否需要补」这一半判断 —— `min_repaired_rebate`
    原先只作用于"补齐后",对"当前"完全不设防;且缺口越小、补齐后越漂亮,越容易放行。
    """
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
            _position("H.POLYMARKET", qty=10.0, price=0.50),
            _position("A.POLYMARKET", qty=9.99, price=0.50),   # 极小缺口
        ],
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is False
    assert "legs" not in ctx.scratch                          # 没写补救腿 → 不会下单


def test_recovery_still_fires_when_current_rebate_below_threshold():
    """当前不达标(单边持仓)→ 仍照常触发。前置门不能把正常补救一起挡掉。"""
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
        positions=[_position("H.POLYMARKET", qty=10.0, price=0.50)],   # 只有一边
    )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(ctx) is True
    assert ctx.scratch["legs"]


def test_threshold_governs_both_directions():
    """同一个 `min_repaired_rebate` 同时决定「要不要补」和「补了有没有用」。

    同一份接近平衡的仓位:阈值宽松(-0.05)→ 当前已达标 → 不补;
    阈值收紧到 0.0 → 当前不达标 → 触发补救。参数语义因此自洽。
    """
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }

    def _fresh_ctx():
        return _ctx(
            books=books,
            infos=infos,
            positions=[
                _position("H.POLYMARKET", qty=10.0, price=0.50),
                _position("A.POLYMARKET", qty=9.99, price=0.50),
            ],
        )

    assert MeanRebateRecoveryCheck(min_repaired_rebate=-0.05).passes(_fresh_ctx()) is False
    assert MeanRebateRecoveryCheck(min_repaired_rebate=0.0).passes(_fresh_ctx()) is True
