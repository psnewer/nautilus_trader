"""Slice 9(#49):`MeanRebateCheck` 算法 + 写 scratch。"""

from unittest.mock import MagicMock

from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _ctx(*, books: dict, infos: dict, instrument_ids: list | None = None) -> EvalContext:
    snap = OpportunitySnapshot(
        pair_id="p",
        instrument_ids=instrument_ids or list(books.keys()),
        order_books=books,
        instrument_info=infos,
    )
    ctx = EvalContext(pair_id="p", snapshot=snap)
    return ctx


# ── 基础:三方向套利场景 + rate > 阈值 ────────────────────────

def test_3way_arb_triggers_above_threshold():
    """每方向取 min(PM_prob, OE_prob):0.25*3=0.75 → rate=0.25 > 0.05 → 命中。"""
    books = {
        "H.POLYMARKET": _fake_book(0.30),    # prob 0.30
        "D.POLYMARKET": _fake_book(0.30),    # prob 0.30
        "A.POLYMARKET": _fake_book(0.30),    # prob 0.30
        "H.ORBITEXCH":  _fake_book(4.0),     # prob 0.25 ← 各方向 OE 更便宜
        "D.ORBITEXCH":  _fake_book(4.0),     # prob 0.25
        "A.ORBITEXCH":  _fake_book(4.0),     # prob 0.25
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "D.POLYMARKET": {"selection_role": "draw"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH":  {"selection_role": "home"},
        "D.ORBITEXCH":  {"selection_role": "draw"},
        "A.ORBITEXCH":  {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)
    ok = MeanRebateCheck(min_rate=0.05).passes(ctx)
    assert ok is True
    legs = ctx.scratch["legs"]
    assert len(legs) == 3
    assert {l["role"] for l in legs} == {"home", "draw", "away"}
    rate = ctx.scratch["mean_rebate_rate"]
    assert rate > 0.05


# ── 阈值控制:rate < min_rate → False,不写 scratch ──

def test_rate_below_threshold_returns_false():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.49),
        "H.ORBITEXCH":  _fake_book(2.0),
        "A.ORBITEXCH":  _fake_book(2.0),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH":  {"selection_role": "home"},
        "A.ORBITEXCH":  {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)
    assert MeanRebateCheck(min_rate=0.10).passes(ctx) is False
    assert "legs" not in ctx.scratch


# ── 缺方向:一边只有 PM 没 OE → False ──

def test_missing_venue_for_role_returns_false():
    books = {
        "H.POLYMARKET": _fake_book(0.40),
        "A.POLYMARKET": _fake_book(0.40),
        "H.ORBITEXCH":  _fake_book(2.5),
        # A.ORBITEXCH 缺
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH":  {"selection_role": "home"},
    }
    ctx = _ctx(books=books, infos=infos)
    assert MeanRebateCheck(min_rate=0.01).passes(ctx) is False


# ── 2-way 也支持(无 draw):rate > 阈值 ──

def test_2way_arb_works():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.45),
        "H.ORBITEXCH":  _fake_book(2.5),    # 0.40
        "A.ORBITEXCH":  _fake_book(2.5),    # 0.40
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH":  {"selection_role": "home"},
        "A.ORBITEXCH":  {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)
    ok = MeanRebateCheck(min_rate=0.05).passes(ctx)
    # total = 0.40 + 0.40 = 0.80 → rate = 0.20
    assert ok is True
    assert len(ctx.scratch["legs"]) == 2
