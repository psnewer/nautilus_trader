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
    def __init__(self, pm=None, oe=None, se=None):
        self._pm = pm or {}
        self._oe = oe or {}
        self._se = se or {}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        if venue == "polymarket":
            return self._pm
        if venue == "orbitexch":
            return self._oe
        if venue == "sharpexch":
            return self._se
        return {}


class _RecordingPortfolio(_Portfolio):
    def __init__(self, shares=None):
        super().__init__()
        self.calls = []
        self._shares = shares or {}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        self.calls.append((pair_id, venue, account_id))
        return self._shares


def test_single_legs_are_adjusted_in_share_limit():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 60.0}, oe={"away": 20.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
        {"venue": "ORBITEXCH", "role": "away", "price": 2.0, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["adjusted_share"] == 40.0
    assert ctx.scratch["legs"][0]["qty"] == 40.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == 40.0
    assert ctx.scratch["legs"][1]["qty"] == 20.0
    assert ctx.scratch["legs"][1]["share_if_wins"] == 40.0


def test_single_legs_missing_share_are_cleared():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 0.0}))
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["legs"] == []


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


def test_sharpexch_legs_are_adjusted_like_decimal_odds_venue():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(se={"home": 60.0, "away": 40.0}))
    ctx.scratch["legs"] = [
        {"venue": "SHARPEXCH", "role": "home", "price": 2.0, "share_if_wins": 100.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["adjusted_share"] == 80.0
    assert ctx.scratch["legs"][0]["qty"] == 40.0
    assert ctx.scratch["legs"][0]["share_if_wins"] == 80.0


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


def test_candidates_missing_share_are_removed():
    ctx = EvalContext(pair_id="p", portfolio=_Portfolio(pm={"home": 0.0}))
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "missing",
            "legs": [{"venue": "POLYMARKET", "role": "home", "price": 0.4}],
        },
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert ctx.scratch["candidates"] == []


def test_share_limit_uses_strategy_default_max_leg_share_when_param_absent():
    ctx = EvalContext(
        pair_id="p",
        portfolio=_Portfolio(pm={"home": 60.0}),
        strategy_defaults={"share": 50.0, "max_leg_share": 100.0},
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification().execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.8
    assert ctx.scratch["legs"][0]["qty"] == 40.0


def test_probability_venue_remaining_uses_leg_venue_for_portfolio_lookup():
    portfolio = _RecordingPortfolio({"home": 60.0})
    ctx = EvalContext(pair_id="p", portfolio=portfolio)
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=100.0).execute(ctx))

    assert portfolio.calls == [("p", "polymarket", None)]


def test_share_limit_param_max_leg_share_overrides_strategy_default():
    ctx = EvalContext(
        pair_id="p",
        portfolio=_Portfolio(pm={"home": 60.0}),
        strategy_defaults={"share": 50.0, "max_leg_share": 100.0},
    )
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home", "price": 0.4, "share_if_wins": 50.0},
    ]

    _run(ShareLimitModification(max_leg_share=80.0).execute(ctx))

    assert ctx.scratch["share_limit_scale"] == 0.4
    assert ctx.scratch["legs"][0]["qty"] == 20.0
