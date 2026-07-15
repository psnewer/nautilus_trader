"""Slice 9(#49):`MeanRebateCheck` 算法 + 写 scratch。"""

from unittest.mock import MagicMock

from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _ctx(*, books: dict, infos: dict, instrument_ids: list | None = None, outcomes: list | None = None) -> EvalContext:
    snap = OpportunitySnapshot(
        pair_id="p",
        instrument_ids=instrument_ids or list(books.keys()),
        order_books=books,
        instrument_info=infos,
        outcomes=outcomes or ["home", "away"],
    )
    ctx = EvalContext(pair_id="p", snapshot=snap, strategy_defaults={"share": 40.0})
    return ctx


# ── #228:3-way 拆分后的 [yes,no] pair(旧"三 role 一 pair"分支退役)──

def test_3way_split_pair_yes_no_arb_triggers_above_threshold():
    """#228:3-way 拆分 pair 内按 claim 分组([yes,no]);decimal no 腿概率 = 1−1/lay。

    yes: min(PM 0.40, OE 1/2.86≈0.3497)≈0.3497;no: min(PM 0.45, OE no 1−1/2.5=0.60)=0.45
    → rate = 1 − 0.7997 ≈ 0.20 > 0.05 命中。
    """
    books = {
        "HY.POLYMARKET": _fake_book(0.40),    # home market YES token
        "HN.POLYMARKET": _fake_book(0.45),    # home market NO token(独立盘口)
        "H.ORBITEXCH":   _fake_book(2.86),    # yes 腿(back)prob≈0.3497
        "HNO.ORBITEXCH": _fake_book(2.5),     # 合成 no 腿:ask=lay 原值 → prob=1−1/2.5=0.60
    }
    infos = {
        "HY.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "HN.POLYMARKET": {"selection_role": "home", "claim": "no"},
        "H.ORBITEXCH":   {"selection_role": "home", "claim": "yes"},
        "HNO.ORBITEXCH": {"selection_role": "home", "claim": "no",
                          "exec_instrument_id": "H.ORBITEXCH"},
    }
    ctx = _ctx(books=books, infos=infos, outcomes=["yes", "no"])
    ok = MeanRebateCheck(min_rate=0.05).passes(ctx)
    assert ok is True
    legs = ctx.scratch["legs"]
    assert len(legs) == 2
    assert {l["role"] for l in legs} == {"yes", "no"}
    assert {l["share_if_wins"] for l in legs} == {40.0}
    chosen_no = next(l for l in legs if l["role"] == "no")
    assert chosen_no["instrument_id"] == "HN.POLYMARKET"   # PM no 0.45 < OE no 0.60
    assert chosen_no["claim"] == "no"
    rate = ctx.scratch["mean_rebate_rate"]
    assert abs(rate - (1.0 - (1.0 / 2.86 + 0.45))) < 1e-9


def test_3way_split_pair_decimal_no_leg_carries_lay_and_exec_redirect():
    """#228:decimal no 腿被选中时,leg 带 claim/lay_price/exec_instrument_id 直通 place_bets。"""
    books = {
        "HY.POLYMARKET": _fake_book(0.40),
        "HN.POLYMARKET": _fake_book(0.62),     # PM no 更贵
        "H.ORBITEXCH":   _fake_book(2.86),
        "HNO.ORBITEXCH": _fake_book(2.5),      # OE no prob=0.60 ← 被选中
    }
    infos = {
        "HY.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "HN.POLYMARKET": {"selection_role": "home", "claim": "no"},
        "H.ORBITEXCH":   {"selection_role": "home", "claim": "yes"},
        "HNO.ORBITEXCH": {"selection_role": "home", "claim": "no",
                          "exec_instrument_id": "H.ORBITEXCH"},
    }
    ctx = _ctx(books=books, infos=infos, outcomes=["yes", "no"])
    assert MeanRebateCheck(min_rate=0.01).passes(ctx) is True
    chosen_no = next(l for l in ctx.scratch["legs"] if l["role"] == "no")
    assert chosen_no["instrument_id"] == "HNO.ORBITEXCH"
    assert chosen_no["lay_price"] == 2.5
    assert chosen_no["exec_instrument_id"] == "H.ORBITEXCH"


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


def test_2way_arb_works_with_sharpexch_as_decimal_odds_venue():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.45),
        "H.SHARPEXCH": _fake_book(2.5),    # 0.40
        "A.SHARPEXCH": _fake_book(2.5),    # 0.40
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.SHARPEXCH": {"selection_role": "home"},
        "A.SHARPEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert MeanRebateCheck(min_rate=0.05).passes(ctx) is True

    assert {leg["venue"] for leg in ctx.scratch["legs"]} == {"SHARPEXCH"}
    assert ctx.scratch["mean_rebate_rate"] == 0.19999999999999996


def test_explicit_share_overrides_strategy_default():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.45),
        "H.ORBITEXCH":  _fake_book(2.5),
        "A.ORBITEXCH":  _fake_book(2.5),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH":  {"selection_role": "home"},
        "A.ORBITEXCH":  {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)
    ctx.strategy_defaults["share"] = 40.0

    assert MeanRebateCheck(min_rate=0.05, share=25.0).passes(ctx) is True
    assert {leg["share_if_wins"] for leg in ctx.scratch["legs"]} == {25.0}
