"""StartPriceGateAction:只保留 start_price 最大 outcome 的执行腿。"""

import asyncio
import math

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.start_price_gate import StartPriceGateAction
from tests.arbitrage.strategy._live_state import live_context


_PAIR = "p"


def _ctx(start_price=None):
    ctx = live_context(
        pair_id=_PAIR,
        infos={"Y.POLYMARKET": {"claim": "yes"}, "N.POLYMARKET": {"claim": "no"}},
    )
    if start_price is not None:
        store = PairPriceStore(ctx.cache)
        store.initialize(_PAIR, ["yes", "no"])
        store.capture_start(_PAIR, start_price)
    return ctx


def _run(ctx, value=None):
    asyncio.run(StartPriceGateAction(value=value).execute(ctx))


def _candidate(outcome):
    return {"candidate_id": outcome, "legs": [{"instrument_id": f"{outcome}.POLYMARKET", "role": outcome}]}


def test_keeps_only_max_start_price_candidate():
    ctx = _ctx(start_price={"yes": 0.7, "no": 0.3})
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx)

    kept = [c["candidate_id"] for c in ctx.scratch["candidates"]]
    assert kept == ["yes"]


def test_bare_legs_filtered_to_max():
    ctx = _ctx(start_price={"yes": 0.2, "no": 0.8})
    ctx.scratch["legs"] = [
        {"instrument_id": "Y.POLYMARKET", "role": "yes"},
        {"instrument_id": "N.POLYMARKET", "role": "no"},
    ]

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["no"]


def test_missing_start_price_fails_closed():
    ctx = _ctx(start_price=None)
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx)

    assert ctx.scratch["candidates"] == []


def test_selected_candidate_is_filtered_and_synchronized_to_legs():
    ctx = _ctx(start_price={"yes": 0.7, "no": 0.3})
    selected = {
        "candidate_id": "chosen",
        "legs": [
            {"instrument_id": "Y.POLYMARKET", "role": "yes"},
            {"instrument_id": "N.POLYMARKET", "role": "no"},
        ],
    }
    ctx.scratch["selected_candidate"] = selected
    ctx.scratch["legs"] = list(selected["legs"])

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["selected_candidate"]["legs"]] == ["yes"]
    assert ctx.scratch["legs"] == ctx.scratch["selected_candidate"]["legs"]


def test_equal_maximum_keeps_both_outcomes():
    ctx = _ctx(start_price={"yes": 0.5, "no": 0.5})
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx)

    assert [candidate["candidate_id"] for candidate in ctx.scratch["candidates"]] == [
        "yes",
        "no",
    ]


@pytest.mark.parametrize("start_price", [0.6, 0.7])
def test_value_keeps_outcome_at_or_above_threshold(start_price):
    ctx = _ctx(start_price={"yes": start_price, "no": 1.0 - start_price})
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx, value=0.6)

    assert [candidate["candidate_id"] for candidate in ctx.scratch["candidates"]] == ["yes"]


def test_value_drops_all_outcomes_below_threshold():
    ctx = _ctx(start_price={"yes": 0.55, "no": 0.45})
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx, value=0.6)

    assert ctx.scratch["candidates"] == []


def test_value_filters_each_outcome_without_requiring_pair_maximum():
    ctx = _ctx(start_price={"yes": 0.6, "no": 0.4})
    ctx.scratch["candidates"] = [_candidate("yes"), _candidate("no")]

    _run(ctx, value=0.4)

    assert [candidate["candidate_id"] for candidate in ctx.scratch["candidates"]] == [
        "yes",
        "no",
    ]


@pytest.mark.parametrize("bad_value", ["bad", math.nan, math.inf, -math.inf])
def test_invalid_value_fails_fast(bad_value):
    with pytest.raises(ValueError, match="start_price_gate: value must be finite"):
        StartPriceGateAction(value=bad_value)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_start_price_fails_closed(bad_value):
    ctx = _ctx(start_price={"yes": bad_value, "no": 0.5})
    ctx.scratch["legs"] = [{"instrument_id": "N.POLYMARKET", "role": "no"}]

    _run(ctx)

    assert ctx.scratch["legs"] == []
