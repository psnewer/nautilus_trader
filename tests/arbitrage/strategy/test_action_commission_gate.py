"""CommissionGateAction:按 PM yes/no best-ask 概率和过滤下单计划。"""

import asyncio

import pytest

from src.arbitrage.strategy.actions.commission_gate import CommissionGateAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
    "Y.ORBITEXCH": {"claim": "yes"},
    "N.ORBITEXCH": {"claim": "no"},
}
_LEGS = [{"instrument_id": "Y.POLYMARKET", "side": "BUY", "claim": "yes"}]


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(*, pm_yes=0.49, pm_no=0.50):
    books = {
        "Y.POLYMARKET": {"ask": pm_yes},
        "N.POLYMARKET": {"ask": pm_no},
        # 非 PM 更优也不参与 commission 计算。
        "Y.ORBITEXCH": {"ask": 0.20},
        "N.ORBITEXCH": {"ask": 0.20},
    }
    if pm_no is None:
        books.pop("N.POLYMARKET")
    return live_context(infos=_INFOS, books=books, instrument_ids=list(_INFOS))


def test_below_threshold_passes_legs_unchanged():
    ctx = _ctx(pm_yes=0.49, pm_no=0.50)
    ctx.scratch["legs"] = list(_LEGS)

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["legs"] == _LEGS


@pytest.mark.parametrize("pm_no", [0.51, 0.52])
def test_equal_or_above_threshold_blocks_legs(pm_no):
    ctx = _ctx(pm_yes=0.49, pm_no=pm_no)
    ctx.scratch["legs"] = list(_LEGS)

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_missing_pm_outcome_fails_closed():
    ctx = _ctx(pm_no=None)
    ctx.scratch["legs"] = list(_LEGS)

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_blocks_selected_candidate_and_synchronizes_legs():
    ctx = _ctx(pm_yes=0.50, pm_no=0.50)
    selected = {"candidate_id": "chosen", "rate": 0.04, "legs": list(_LEGS)}
    ctx.scratch.update(selected_candidate=selected, legs=list(_LEGS))

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["selected_candidate"] == {
        "candidate_id": "chosen",
        "rate": 0.04,
        "legs": [],
    }
    assert ctx.scratch["legs"] == []


def test_blocks_submit_candidates_but_preserves_cancel_candidate():
    ctx = _ctx(pm_yes=0.50, pm_no=0.50)
    cancel = {"candidate_id": "cancel", "cancel_pair_orders": True, "legs": []}
    ctx.scratch["candidates"] = [
        {"candidate_id": "submit", "legs": list(_LEGS)},
        cancel,
    ]

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["candidates"] == [cancel]


def test_cancel_only_input_is_noop_even_without_pm_quotes():
    ctx = _ctx(pm_no=None)
    cancel = {"candidate_id": "cancel", "cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = cancel

    _run(CommissionGateAction(commission=1.0).execute(ctx))

    assert ctx.scratch["selected_candidate"] is cancel


@pytest.mark.parametrize("value", [None, "bad", float("nan"), float("inf")])
def test_invalid_commission_fails_fast(value):
    with pytest.raises(ValueError, match="finite number"):
        CommissionGateAction(commission=value)
