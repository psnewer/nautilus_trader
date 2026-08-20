"""OneSideRebateCheck:定向返水 candidate 生成。"""

from unittest.mock import MagicMock

from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.checks.one_side_rebate import OneSideRebateCheck
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.strategy._live_state import live_context


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _ctx(*, books: dict, infos: dict, outcomes: list | None = None) -> EvalContext:
    return live_context(
        books=books,
        infos=infos,
        instrument_ids=list(infos.keys()),
        strategy_defaults={"share": 100.0},
    )


def test_2way_enumerates_all_venue_combos_and_targets_above_threshold():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
        "H.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.0)),   # prob=0.50
        "A.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.0)),   # prob=0.50
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
        "H.ORBITEXCH": {"selection_role": "home"},
        "A.ORBITEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    ok = OneSideRebateCheck(min_rate=0.09, share=100.0).passes(ctx)

    assert ok is True
    candidates = ctx.scratch["candidates"]
    assert len(candidates) == 4
    assert {c["target_role"] for c in candidates} == {"yes", "no"}
    combo_keys = {tuple(leg["instrument_id"] for leg in c["legs"]) for c in candidates}
    assert ("H.POLYMARKET", "A.POLYMARKET") in combo_keys
    assert ("H.POLYMARKET", "A.ORBITEXCH") in combo_keys


def test_candidate_quantities_put_rebate_on_one_side():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.10, share=100.0).passes(ctx) is True

    candidate = ctx.scratch["candidates"][0]
    assert candidate["target_role"] == "yes"
    assert round(candidate["rate"], 6) == round((1.0 - 0.95) / 0.45, 6)
    by_role = {leg["role"]: leg for leg in candidate["legs"]}
    assert round(by_role["yes"]["share_if_wins"], 6) == round(50.0 / 0.45, 6)
    assert round(by_role["yes"]["qty"], 6) == round(50.0 / 0.45, 6)
    assert by_role["yes"]["cost"] == 50.0
    assert by_role["no"]["share_if_wins"] == 100.0
    assert by_role["no"]["qty"] == 100.0
    assert by_role["no"]["cost"] == 50.0

    explicit_true_ctx = _ctx(books=books, infos=infos)
    assert OneSideRebateCheck(
        min_rate=0.10,
        share=100.0,
        one_side=True,
    ).passes(explicit_true_ctx) is True
    assert explicit_true_ctx.scratch["candidates"] == ctx.scratch["candidates"]


def test_one_side_false_buys_configured_share_on_both_outcomes():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(
        min_rate=0.10,
        share=100.0,
        one_side=False,
    ).passes(ctx) is True

    candidate = ctx.scratch["candidates"][0]
    by_role = {leg["role"]: leg for leg in candidate["legs"]}
    assert by_role["yes"]["share_if_wins"] == 100.0
    assert by_role["yes"]["qty"] == 100.0
    assert by_role["yes"]["cost"] == 45.0
    assert by_role["no"]["share_if_wins"] == 100.0
    assert by_role["no"]["qty"] == 100.0
    assert by_role["no"]["cost"] == 50.0


def test_one_side_false_keeps_decimal_qty_conversion():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.SHARPEXCH": _fake_book(probability_from_price(SHARPEXCH, 2.0)),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.SHARPEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(
        min_rate=0.10,
        share=100.0,
        one_side=False,
    ).passes(ctx) is True

    candidate = ctx.scratch["candidates"][0]
    no_leg = next(leg for leg in candidate["legs"] if leg["role"] == "no")
    assert no_leg["share_if_wins"] == 100.0
    assert no_leg["qty"] == 50.0
    assert no_leg["cost"] == 50.0


def test_one_side_must_be_boolean():
    try:
        OneSideRebateCheck(one_side="false")
    except ValueError as exc:
        assert str(exc) == "one_side must be a boolean"
    else:
        raise AssertionError("expected invalid one_side to fail")


def test_sharpexch_candidate_uses_decimal_odds_qty():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.SHARPEXCH": _fake_book(probability_from_price(SHARPEXCH, 2.0)),   # prob=0.50
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.SHARPEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.10, share=100.0).passes(ctx) is True

    candidate = ctx.scratch["candidates"][0]
    by_role = {leg["role"]: leg for leg in candidate["legs"]}
    assert by_role["no"]["venue"] == "SHARPEXCH"
    assert by_role["no"]["share_if_wins"] == 100.0
    assert by_role["no"]["qty"] == 50.0
    assert by_role["no"]["cost"] == 50.0


def test_3way_split_pair_yes_no_generates_candidates():
    """#228:3-way 拆分 pair([yes,no])按 claim 分组枚举 candidate;旧三 role 一 pair 分支退役。"""
    books = {
        "HY.POLYMARKET": _fake_book(0.20),
        "HN.POLYMARKET": _fake_book(0.30),
        "HNO.ORBITEXCH": _fake_book(probability_from_price(ORBITEXCH, 2.5, "no")),   # 合成 no 腿:ask=lay 原值 → prob=1−1/2.5=0.60
    }
    infos = {
        "HY.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "HN.POLYMARKET": {"selection_role": "home", "claim": "no"},
        "HNO.ORBITEXCH": {"selection_role": "home", "claim": "no",
                          "quote_claim": "no",
                          "exec_instrument_id": "H.ORBITEXCH"},
    }
    ctx = _ctx(books=books, infos=infos, outcomes=["yes", "no"])

    assert OneSideRebateCheck(min_rate=0.20, share=100.0).passes(ctx) is True

    candidates = ctx.scratch["candidates"]
    assert {c["target_role"] for c in candidates} <= {"yes", "no"}
    assert all(tuple(c["roles"]) == ("yes", "no") for c in candidates)
    assert all(len(c["legs"]) == 2 for c in candidates)
    # 含 OE 合成 no 腿的 candidate,其 no leg 带 claim(place_bets 转 SELL@lay)
    oe_no_candidates = [
        c for c in candidates
        if any(leg["instrument_id"] == "HNO.ORBITEXCH" for leg in c["legs"])
    ]
    assert oe_no_candidates
    for c in oe_no_candidates:
        no_leg = next(leg for leg in c["legs"] if leg["instrument_id"] == "HNO.ORBITEXCH")
        assert no_leg["claim"] == "no"


def test_decimal_odds_target_qty_share_and_cost():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.SHARPEXCH": _fake_book(probability_from_price(SHARPEXCH, 2.0)),   # prob=0.50
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.SHARPEXCH": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.10, share=100.0).passes(ctx) is True

    away_target = next(c for c in ctx.scratch["candidates"] if c["target_role"] == "no")
    by_role = {leg["role"]: leg for leg in away_target["legs"]}
    assert round(away_target["rate"], 6) == round((1.0 - 0.95) / 0.50, 6)
    assert by_role["yes"]["share_if_wins"] == 100.0
    assert by_role["yes"]["cost"] == 45.0
    assert by_role["no"]["venue"] == "SHARPEXCH"
    assert round(by_role["no"]["share_if_wins"], 6) == round(55.0 / 0.50, 6)
    assert round(by_role["no"]["qty"], 6) == round((55.0 / 0.50) / 2.0, 6)
    assert round(by_role["no"]["cost"], 6) == 55.0


def test_rate_below_threshold_does_not_write_candidates():
    books = {
        "H.POLYMARKET": _fake_book(0.50),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.01, share=100.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch


def test_uses_strategy_default_share_when_param_absent():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)
    ctx.strategy_defaults["share"] = 40.0

    assert OneSideRebateCheck(min_rate=0.10).passes(ctx) is True
    candidate = ctx.scratch["candidates"][0]
    assert candidate["base_share"] == 40.0


def test_missing_live_state_does_not_write_candidates():
    ctx = EvalContext(pair_id="p", strategy_defaults={"share": 100.0})

    assert OneSideRebateCheck(min_rate=0.01, share=100.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch


def test_missing_role_does_not_write_candidates():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.01, share=100.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch


def test_missing_order_book_does_not_write_candidates():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.01, share=100.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch


def test_non_positive_price_does_not_write_candidates():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.0),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.01, share=100.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch


def test_non_positive_share_does_not_write_candidates():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home"},
        "A.POLYMARKET": {"selection_role": "away"},
    }
    ctx = _ctx(books=books, infos=infos)

    assert OneSideRebateCheck(min_rate=0.01, share=0.0).passes(ctx) is False
    assert "candidates" not in ctx.scratch
