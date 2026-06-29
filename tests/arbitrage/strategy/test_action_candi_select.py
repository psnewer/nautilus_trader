"""CandiSelectAction:从 candidates 选择最大 leg share 最大的 candidate。"""

import asyncio

from src.arbitrage.strategy.actions.candi_select import CandiSelectAction
from src.arbitrage.strategy.condition import EvalContext


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_selects_candidate_whose_largest_leg_share_is_largest():
    ctx = EvalContext(pair_id="p")
    ctx.scratch["candidates"] = [
        {
            "candidate_id": "base_big_but_leg_small",
            "adjusted_share": 100.0,
            "legs": [{"role": "home", "share_if_wins": 40.0}],
        },
        {
            "candidate_id": "leg_big",
            "adjusted_share": 50.0,
            "legs": [
                {"role": "home", "share_if_wins": 45.0},
                {"role": "away", "share_if_wins": 80.0},
            ],
        },
    ]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["selected_candidate"]["candidate_id"] == "leg_big"
    assert ctx.scratch["legs"] == [
        {"role": "home", "share_if_wins": 45.0},
        {"role": "away", "share_if_wins": 80.0},
    ]


def test_no_candidates_clears_legs():
    ctx = EvalContext(pair_id="p")
    ctx.scratch["candidates"] = []
    ctx.scratch["legs"] = [{"role": "old"}]

    _run(CandiSelectAction().execute(ctx))

    assert ctx.scratch["legs"] == []
