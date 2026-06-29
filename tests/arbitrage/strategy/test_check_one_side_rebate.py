"""OneSideRebateCheck:定向返水 candidate 生成。"""

from unittest.mock import MagicMock

from src.arbitrage.strategy.checks.one_side_rebate import OneSideRebateCheck
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.snapshot import OpportunitySnapshot


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    return book


def _ctx(*, books: dict, infos: dict) -> EvalContext:
    snap = OpportunitySnapshot(
        pair_id="p",
        instrument_ids=list(books.keys()),
        order_books=books,
        instrument_info=infos,
    )
    return EvalContext(pair_id="p", snapshot=snap)


def test_2way_enumerates_all_venue_combos_and_targets_above_threshold():
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.POLYMARKET": _fake_book(0.50),
        "H.ORBITEXCH": _fake_book(2.0),   # prob=0.50
        "A.ORBITEXCH": _fake_book(2.0),   # prob=0.50
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
    assert {c["target_role"] for c in candidates} == {"home", "away"}
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
    assert candidate["target_role"] == "home"
    assert round(candidate["rate"], 6) == round((1.0 - 0.95) / 0.45, 6)
    by_role = {leg["role"]: leg for leg in candidate["legs"]}
    assert round(by_role["home"]["share_if_wins"], 6) == round(50.0 / 0.45, 6)
    assert round(by_role["home"]["qty"], 6) == round(50.0 / 0.45, 6)
    assert by_role["home"]["cost"] == 50.0
    assert by_role["away"]["share_if_wins"] == 100.0
    assert by_role["away"]["qty"] == 100.0
    assert by_role["away"]["cost"] == 50.0


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
