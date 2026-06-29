"""ShareLimitModification:单一 legs + candidate 数组直接缩放。"""

import asyncio

from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.condition import EvalContext


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Portfolio:
    def __init__(self, pm=None, oe=None):
        self._pm = pm or {}
        self._oe = oe or {}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        if venue == "polymarket":
            return self._pm
        if venue == "orbitexch":
            return self._oe
        return {}


def test_single_legs_are_adjusted_in_share_limit():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 60.0}, oe={"away": 20.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4},
        {"venue": "ORBITEXCH", "role": "away", "price": 2.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0, share=50.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["adjusted_share"] == 40.0
    assert ctx.scratch["legs"][0]["qty"] == 40.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == 40.0
    assert ctx.scratch["legs"][1]["qty"] == 20.0
    assert ctx.scratch["legs"][1]["share_if_wins"] == 40.0


def test_candidates_are_individually_share_limited_and_output_as_array():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 20.0}, oe={"away": 50.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "A",
            "base_share": 100.0,
            "legs": [
                {"venue": "POLYMARKET", "role": "home", "qty": 100.0, "share_if_wins": 100.0},
                {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "qty": 50.0, "share_if_wins": 100.0},
            ],
        },
        {
            "candidate_id": "B",
            "base_share": 40.0,
            "legs": [
                {"venue": "POLYMARKET", "role": "home", "qty": 40.0, "share_if_wins": 40.0},
                {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "qty": 20.0, "share_if_wins": 40.0},
            ],
        },
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    adjusted = ctx.scratch["candidates"]
    assert [c["candidate_id"] for c in adjusted] == ["A", "B"]
    assert adjusted[0]["share_limit_scale"] == 0.5
    assert adjusted[0]["adjusted_share"] == 50.0
    assert adjusted[0]["legs"][0]["qty"] == 50.0
    assert adjusted[0]["legs"][1]["qty"] == 25.0
    assert adjusted[1]["share_limit_scale"] == 1.0
    assert adjusted[1]["adjusted_share"] == 40.0


def test_candidates_with_no_remaining_are_removed():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 100.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "blocked",
            "base_share": 20.0,
            "legs": [{"venue": "POLYMARKET", "role": "home", "qty": 20.0, "share_if_wins": 20.0}],
        },
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["candidates"] == []
