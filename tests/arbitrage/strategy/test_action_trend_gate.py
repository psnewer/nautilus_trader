"""TrendGateAction：按当前跨 venue 最优概率相对 trend_price 的方向过滤 leg。"""

import asyncio

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.trend_gate import TrendGateAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
    "Y.ORBITEXCH": {"claim": "yes"},
    "N.ORBITEXCH": {"claim": "no"},
}


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(*, books=None, baseline=None):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        books=books or {
            "Y.POLYMARKET": {"ask": 0.48},
            "N.POLYMARKET": {"ask": 0.58},
            "Y.ORBITEXCH": {"ask": 0.46},
            "N.ORBITEXCH": {"ask": 0.56},
        },
    )
    store = PairPriceStore(ctx.cache)
    store.initialize(ctx.pair_id, ["yes", "no"])
    if baseline is not None:
        store.update_trend(ctx.pair_id, baseline)
    return ctx


def _install(ctx, legs=None):
    legs = legs or [
        {"instrument_id": "Y.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "N.POLYMARKET", "side": "BUY", "claim": "no"},
    ]
    ctx.scratch["selected_candidate"] = {"candidate_id": "chosen", "rate": 0.1, "legs": legs}
    ctx.scratch["legs"] = legs


def test_up_keeps_outcome_above_baseline_using_cross_venue_best_ask():
    ctx = _ctx(baseline={"yes": 0.45, "no": 0.57})
    _install(ctx)

    _run(TrendGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]
    assert ctx.scratch["selected_candidate"]["rate"] == 0.1


def test_up_false_keeps_outcome_below_baseline():
    ctx = _ctx(baseline={"yes": 0.45, "no": 0.57})
    _install(ctx)

    _run(TrendGateAction(up=False).execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["N.POLYMARKET"]


def test_outcomes_are_compared_independently_without_complement_direction_requirement():
    ctx = _ctx(
        books={
            "Y.POLYMARKET": {"ask": 0.47},
            "N.POLYMARKET": {"ask": 0.59},
            "Y.ORBITEXCH": {"ask": 0.46},
            "N.ORBITEXCH": {"ask": 0.58},
        },
        baseline={"yes": 0.45, "no": 0.57},
    )
    _install(ctx)

    _run(TrendGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == [
        "Y.POLYMARKET",
        "N.POLYMARKET",
    ]


def test_flat_outcome_is_dropped():
    ctx = _ctx(baseline={"yes": 0.46, "no": 0.57})
    _install(ctx)

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize("baseline", [None, {}])
def test_missing_trend_price_drops_all(baseline):
    ctx = _ctx(baseline=baseline)
    _install(ctx)

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_incomplete_current_quotes_drop_all():
    ctx = _ctx(
        books={
            "Y.POLYMARKET": {"ask": 0.47},
            "Y.ORBITEXCH": {"ask": 0.46},
        },
        baseline={"yes": 0.45, "no": 0.57},
    )
    _install(ctx)

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_noop_without_selected_candidate():
    ctx = _ctx(baseline={"yes": 0.45, "no": 0.57})
    _run(TrendGateAction().execute(ctx))
    assert "legs" not in ctx.scratch


def test_filters_legs_without_selected_candidate():
    ctx = _ctx(baseline={"yes": 0.45, "no": 0.57})
    ctx.scratch["legs"] = [
        {"instrument_id": "Y.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "N.POLYMARKET", "side": "BUY", "claim": "no"},
    ]

    _run(TrendGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]
    assert "selected_candidate" not in ctx.scratch


def test_missing_trend_price_drops_legs_without_selected_candidate():
    ctx = _ctx(baseline=None)
    ctx.scratch["legs"] = [
        {"instrument_id": "Y.POLYMARKET", "side": "BUY", "claim": "yes"},
    ]

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []
    assert "selected_candidate" not in ctx.scratch


def test_skips_cancel_pair_candidate():
    ctx = _ctx(baseline={"yes": 0.45, "no": 0.57})
    candidate = {"candidate_id": "c", "cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = candidate

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["selected_candidate"] is candidate


def test_invalid_up_param_raises():
    with pytest.raises(ValueError, match="up must be a boolean"):
        TrendGateAction(up="false")
