"""当前盘局分 Action：筛选订单方向，不看已完成盘或抢七小分。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.current_set_game_selection import CurrentSetGameSelectionAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


class _SportsStore:
    def __init__(self, score):
        self._state = SimpleNamespace(score=score) if score is not None else None

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _ctx(score):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        sports_store=_SportsStore(score),
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "side": "BUY"},
        {"instrument_id": "A.POLYMARKET", "side": "BUY"},
        {"instrument_id": "H.POLYMARKET", "side": "SELL"},
        {"instrument_id": "A.POLYMARKET", "side": "SELL"},
    ]
    return ctx


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_current_set_ignores_completed_set_and_reverses_sell_standing():
    ctx = _ctx("6-4, 2-3")

    _run(CurrentSetGameSelectionAction(standing="win").execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("A.POLYMARKET", "BUY"),
        ("H.POLYMARKET", "SELL"),
    ]


@pytest.mark.parametrize("score", ["6-6", "6-4, 6-6(3-4)"])
def test_tie_break_points_are_ignored_and_six_all_is_draw(score):
    ctx = _ctx(score)

    _run(CurrentSetGameSelectionAction(standing="draw").execute(ctx))
    assert len(ctx.scratch["legs"]) == 4


def test_candidate_pool_filters_each_candidate_without_choosing_one():
    ctx = _ctx("6-4, 2-3")
    legs = ctx.scratch.pop("legs")
    ctx.scratch["candidates"] = [
        {"candidate_id": "home", "rate": 0.1, "legs": [legs[0]]},
        {"candidate_id": "away", "rate": 0.2, "legs": [legs[1]]},
    ]

    _run(CurrentSetGameSelectionAction(standing="win").execute(ctx))
    assert ctx.scratch["candidates"] == [
        {"candidate_id": "away", "rate": 0.2, "legs": [legs[1]]},
    ]


def test_missing_or_invalid_score_fails_closed():
    for score in (None, "6-4, bad"):
        ctx = _ctx(score)
        _run(CurrentSetGameSelectionAction(standing="win|draw").execute(ctx))
        assert ctx.scratch["legs"] == []


def test_cancel_candidate_is_preserved_without_score():
    ctx = _ctx(None)
    ctx.scratch.clear()
    cancel = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["candidates"] = [cancel]

    _run(CurrentSetGameSelectionAction(standing="win").execute(ctx))
    assert ctx.scratch["candidates"] == [cancel]


def test_missing_standing_is_noop():
    ctx = _ctx(None)
    before = ctx.scratch["legs"]

    _run(CurrentSetGameSelectionAction().execute(ctx))

    assert ctx.scratch["legs"] is before


@pytest.mark.parametrize("standing", ["", "win||lose", "ahead", True])
def test_invalid_standing_is_rejected(standing):
    with pytest.raises(ValueError):
        CurrentSetGameSelectionAction(standing=standing)
