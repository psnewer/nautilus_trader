"""set_exempt：在指定当前盘拦截下单腿。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.set_exempt import SetExemptAction
from tests.arbitrage.strategy._live_state import live_context


class _SportsStore:
    def __init__(self, period):
        self._state = SimpleNamespace(period=period)

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _ctx(period):
    ctx = live_context(
        infos={},
        books={},
        instrument_ids=[],
        sports_store=_SportsStore(period),
    )
    ctx.pair_registry.register(ctx.pair_id, [], game_id=42)
    return ctx


def _run(ctx, set_number=3):
    asyncio.run(SetExemptAction(set=set_number).execute(ctx))


@pytest.mark.parametrize("period", ["S3", "TB3"])
def test_matching_regular_or_tie_break_set_clears_legs(period):
    ctx = _ctx(period)
    ctx.scratch["legs"] = [{"instrument_id": "Y.POLYMARKET"}]

    _run(ctx)

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize("period", ["S2", "", "SET3"])
def test_other_or_unknown_set_keeps_legs(period):
    ctx = _ctx(period)
    legs = [{"instrument_id": "Y.POLYMARKET"}]
    ctx.scratch["legs"] = legs

    _run(ctx)

    assert ctx.scratch["legs"] is legs


def test_selected_candidate_keeps_metadata_and_clears_legs():
    ctx = _ctx("S3")
    selected = {
        "candidate_id": "chosen",
        "rate": 0.1,
        "legs": [{"instrument_id": "Y.POLYMARKET"}],
    }
    ctx.scratch.update(selected_candidate=selected, legs=list(selected["legs"]))

    _run(ctx)

    assert ctx.scratch["selected_candidate"] == {
        "candidate_id": "chosen",
        "rate": 0.1,
        "legs": [],
    }
    assert ctx.scratch["legs"] == []


def test_candidate_pool_preserves_cancel_candidate_only():
    ctx = _ctx("S3")
    cancel = {"candidate_id": "cancel", "cancel_pair_orders": True, "legs": []}
    ctx.scratch["candidates"] = [
        {"candidate_id": "submit", "legs": [{"instrument_id": "Y.POLYMARKET"}]},
        cancel,
    ]

    _run(ctx)

    assert ctx.scratch["candidates"] == [cancel]
    assert ctx.scratch["legs"] == []


def test_cancel_only_selected_candidate_is_noop():
    ctx = _ctx("S3")
    cancel = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = cancel

    _run(ctx)

    assert ctx.scratch["selected_candidate"] is cancel


@pytest.mark.parametrize("value", [0, -1, True, 3.0, "3"])
def test_set_must_be_positive_integer(value):
    with pytest.raises(ValueError, match="set must be a positive integer"):
        SetExemptAction(set=value)
