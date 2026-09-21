"""VenueSelectAction 按 PM / 非 PM 逐腿过滤。"""

import asyncio

import pytest

from src.arbitrage.strategy.actions.venue_select import VenueSelectAction
from tests.arbitrage.strategy._live_state import live_context


def _run(ctx, *, pm=True):
    asyncio.run(VenueSelectAction(pm=pm).execute(ctx))


def _ctx():
    return live_context(infos={}, books={}, instrument_ids=[])


def test_default_keeps_only_pm_legs():
    ctx = _ctx()
    pm_leg = {"instrument_id": "Y.POLYMARKET", "venue": "POLYMARKET"}
    ctx.scratch["legs"] = [pm_leg, {"instrument_id": "Y.ORBITEXCH", "venue": "ORBITEXCH"}]

    _run(ctx)

    assert ctx.scratch["legs"] == [pm_leg]


def test_pm_false_drops_only_pm_legs():
    ctx = _ctx()
    oe_leg = {"instrument_id": "Y.ORBITEXCH", "venue": "ORBITEXCH"}
    se_leg = {"instrument_id": "Y.SHARPEXCH", "venue": "SHARPEXCH"}
    ctx.scratch["legs"] = [
        {"instrument_id": "Y.POLYMARKET", "venue": "polymarket"},
        oe_leg,
        se_leg,
    ]

    _run(ctx, pm=False)

    assert ctx.scratch["legs"] == [oe_leg, se_leg]


def test_selected_candidate_updates_legs_and_keeps_metadata():
    ctx = _ctx()
    pm_leg = {"instrument_id": "Y.POLYMARKET", "venue": "POLYMARKET"}
    selected = {
        "candidate_id": "chosen",
        "legs": [pm_leg, {"instrument_id": "Y.ORBITEXCH", "venue": "ORBITEXCH"}],
    }
    ctx.scratch.update(selected_candidate=selected, legs=list(selected["legs"]))

    _run(ctx)

    assert ctx.scratch["selected_candidate"] == {
        "candidate_id": "chosen",
        "legs": [pm_leg],
    }
    assert ctx.scratch["legs"] == [pm_leg]


def test_candidate_pool_filters_legs_and_preserves_cancel():
    ctx = _ctx()
    cancel = {"candidate_id": "cancel", "cancel_pair_orders": True, "legs": []}
    ctx.scratch["candidates"] = [
        {"candidate_id": "drop", "legs": [{"venue": "ORBITEXCH"}]},
        {
            "candidate_id": "keep",
            "rate": 0.04,
            "legs": [{"venue": "POLYMARKET"}, {"venue": "ORBITEXCH"}],
        },
        cancel,
    ]

    _run(ctx)

    assert ctx.scratch["candidates"] == [
        {
            "candidate_id": "keep",
            "rate": 0.04,
            "legs": [{"venue": "POLYMARKET"}],
        },
        cancel,
    ]


def test_cancel_only_selected_candidate_is_noop():
    ctx = _ctx()
    cancel = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = cancel

    _run(ctx)

    assert ctx.scratch["selected_candidate"] is cancel


@pytest.mark.parametrize("pm", [None, 1, "true"])
def test_pm_requires_boolean(pm):
    with pytest.raises(ValueError, match="pm must be a boolean"):
        VenueSelectAction(pm=pm)
