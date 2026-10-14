"""PM 实时报价产腿 Action。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.current_set_game_selection import CurrentSetGameSelectionAction
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.pm_quote_legs import PmQuoteLegsAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


def _ctx(*, books=None, infos=None, share=10.0):
    return live_context(
        infos=infos or _INFOS,
        books=books or {
            "H.POLYMARKET": {"bid": 0.41, "ask": 0.43},
            "A.POLYMARKET": {"bid": 0.55, "ask": 0.57},
        },
        strategy_defaults={"share": share},
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _SportsStore:
    def get(self, game_id):
        assert game_id == 42
        return SimpleNamespace(score="2-3")


def test_generates_complete_pm_quote_legs_without_selecting_direction():
    ctx = _ctx()

    _run(PmQuoteLegsAction().execute(ctx))

    assert [leg["claim"] for leg in ctx.scratch["legs"]] == ["yes", "no"]
    assert [leg["price"] for leg in ctx.scratch["legs"]] == [0.43, 0.57]
    assert [leg["qty"] for leg in ctx.scratch["legs"]] == [10.0, 10.0]


def test_actions_select_loser_within_max_and_limit_buy_to_bid():
    ctx = _ctx()
    ctx.sports_store = _SportsStore()
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)

    _run(PmQuoteLegsAction().execute(ctx))
    _run(CurrentSetGameSelectionAction(standing="lose", max=1).execute(ctx))
    _run(PlaceBetsAction(origin="in_game", limit=True).execute(ctx))

    order = ctx.scratch["execution_plan"].orders[0]
    assert order.spec["instrument_id"] == "H.POLYMARKET"
    assert order.spec["price"] == pytest.approx(0.41)
    assert "time_in_force" not in order.spec


def test_incomplete_quotes_or_invalid_share_clear_legs():
    for ctx in (
        _ctx(books={"H.POLYMARKET": {"ask": 0.43}}),
        _ctx(share=0.0),
    ):
        ctx.scratch["legs"] = [{"stale": True}]
        _run(PmQuoteLegsAction().execute(ctx))
        assert ctx.scratch["legs"] == []


def test_duplicate_pm_instrument_for_same_outcome_clears_legs():
    infos = {
        **_INFOS,
        "H2.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    }
    books = {
        "H.POLYMARKET": {"ask": 0.43},
        "H2.POLYMARKET": {"ask": 0.44},
        "A.POLYMARKET": {"ask": 0.57},
    }
    ctx = _ctx(books=books, infos=infos)

    _run(PmQuoteLegsAction().execute(ctx))

    assert ctx.scratch["legs"] == []
