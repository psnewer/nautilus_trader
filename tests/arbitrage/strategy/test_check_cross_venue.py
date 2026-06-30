"""RequireCrossVenueCheck:过滤单 venue 覆盖全部套利腿的机会。"""

from src.arbitrage.strategy.checks.cross_venue import RequireCrossVenueCheck
from src.arbitrage.strategy.condition import EvalContext


def _ctx():
    return EvalContext(pair_id="p")


def test_legs_all_same_venue_rejected_and_cleared():
    ctx = _ctx()
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home"},
        {"venue": "POLYMARKET", "role": "away"},
    ]

    assert RequireCrossVenueCheck().passes(ctx) is False
    assert "legs" not in ctx.scratch


def test_legs_cross_venue_pass():
    ctx = _ctx()
    ctx.scratch["legs"] = [
        {"venue": "POLYMARKET", "role": "home"},
        {"venue": "ORBITEXCH", "role": "away"},
    ]

    assert RequireCrossVenueCheck().passes(ctx) is True
    assert len(ctx.scratch["legs"]) == 2


def test_candidates_filter_same_venue_candidates():
    ctx = _ctx()
    same = {
        "candidate_id": "same",
        "legs": [
            {"venue": "POLYMARKET", "role": "home"},
            {"venue": "POLYMARKET", "role": "away"},
        ],
    }
    cross = {
        "candidate_id": "cross",
        "legs": [
            {"venue": "POLYMARKET", "role": "home"},
            {"venue": "ORBITEXCH", "role": "away"},
        ],
    }
    ctx.scratch["candidates"] = [same, cross]

    assert RequireCrossVenueCheck().passes(ctx) is True
    assert ctx.scratch["candidates"] == [cross]
    assert ctx.scratch["cross_venue_filter"] == {"before": 2, "after": 1}


def test_candidates_all_same_venue_rejected_and_cleared():
    ctx = _ctx()
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "same",
            "legs": [
                {"venue": "ORBITEXCH", "role": "home"},
                {"venue": "ORBITEXCH", "role": "away"},
            ],
        },
    ]

    assert RequireCrossVenueCheck().passes(ctx) is False
    assert "candidates" not in ctx.scratch
