"""PriceGateAction 按执行腿统一隐含概率逐腿过滤。"""

import asyncio

import pytest

from src.arbitrage.strategy.actions.price_gate import PriceGateAction
from tests.arbitrage.strategy._live_state import live_context


def _run(ctx, *, price=0.40, below=False):
    asyncio.run(PriceGateAction(price=price, below=below).execute(ctx))


def _ctx():
    return live_context(infos={}, books={}, instrument_ids=[])


def test_default_blocks_only_prices_below_threshold():
    ctx = _ctx()
    legs = [
        {"venue": "POLYMARKET", "price": 0.39},
        {"venue": "POLYMARKET", "price": 0.40},
        {"venue": "POLYMARKET", "price": 0.41},
    ]
    ctx.scratch["legs"] = legs

    _run(ctx)

    assert ctx.scratch["legs"] == legs[1:]


def test_below_blocks_only_prices_above_threshold():
    ctx = _ctx()
    legs = [
        {"venue": "POLYMARKET", "price": 0.39},
        {"venue": "POLYMARKET", "price": 0.40},
        {"venue": "POLYMARKET", "price": 0.41},
    ]
    ctx.scratch["legs"] = legs

    _run(ctx, below=True)

    assert ctx.scratch["legs"] == legs[:2]


def test_missing_or_invalid_leg_probability_fails_closed():
    ctx = _ctx()
    valid = {"venue": "POLYMARKET", "price": 0.40}
    ctx.scratch["legs"] = [
        {},
        {"venue": "POLYMARKET", "price": "bad"},
        {"venue": "POLYMARKET", "price": float("nan")},
        valid,
    ]

    _run(ctx)

    assert ctx.scratch["legs"] == [valid]


def test_decimal_price_is_converted_to_probability():
    ctx = _ctx()
    yes = {"venue": "ORBITEXCH", "role": "yes", "price": 2.0}
    no = {"venue": "ORBITEXCH", "role": "no", "price": 1.25}
    ctx.scratch["legs"] = [yes, no]

    _run(ctx, price=0.50)

    assert ctx.scratch["legs"] == [yes]


def test_committed_probability_takes_precedence_over_native_price():
    ctx = _ctx()
    leg = {"venue": "ORBITEXCH", "role": "yes", "price": 1.42, "prob": 0.70}
    ctx.scratch["legs"] = [leg]

    _run(ctx, price=0.59, below=True)

    assert ctx.scratch["legs"] == []


def test_selected_candidate_updates_legs_and_keeps_metadata():
    ctx = _ctx()
    ctx.scratch["selected_candidate"] = {
        "candidate_id": "chosen",
        "legs": [
            {"venue": "POLYMARKET", "price": 0.39},
            {"venue": "POLYMARKET", "price": 0.40},
        ],
    }
    ctx.scratch["legs"] = list(ctx.scratch["selected_candidate"]["legs"])

    _run(ctx)

    assert ctx.scratch["selected_candidate"] == {
        "candidate_id": "chosen",
        "legs": [{"venue": "POLYMARKET", "price": 0.40}],
    }
    assert ctx.scratch["legs"] == [{"venue": "POLYMARKET", "price": 0.40}]


def test_candidate_pool_filters_legs_and_preserves_cancel():
    ctx = _ctx()
    cancel = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "drop",
            "legs": [{"venue": "POLYMARKET", "price": 0.39}],
        },
        {
            "candidate_id": "keep",
            "legs": [
                {"venue": "POLYMARKET", "price": 0.39},
                {"venue": "POLYMARKET", "price": 0.40},
            ],
        },
        cancel,
    ]

    _run(ctx)

    assert ctx.scratch["candidates"] == [
        {
            "candidate_id": "keep",
            "legs": [{"venue": "POLYMARKET", "price": 0.40}],
        },
        cancel,
    ]


def test_cancel_only_selected_candidate_is_noop():
    ctx = _ctx()
    cancel = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = cancel

    _run(ctx)

    assert ctx.scratch["selected_candidate"] is cancel


@pytest.mark.parametrize("price", [None, "bad", float("nan"), float("inf")])
def test_invalid_threshold_rejected(price):
    with pytest.raises(ValueError, match="price must be finite"):
        PriceGateAction(price=price)


def test_below_requires_boolean():
    with pytest.raises(ValueError, match="below must be a boolean"):
        PriceGateAction(price=0.40, below="true")
