"""SameSetAction 按仓位 opening order 下单盘筛选平仓腿。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.same_set import SameSetAction
from tests.arbitrage.strategy._live_state import live_context


class _SportsStore:
    def __init__(self, score):
        self._state = SimpleNamespace(score=score)

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(opening_score, current_score):
    order = SimpleNamespace(
        client_order_id="O-1",
        tags=[f"arb:order_score={opening_score}"],
    )
    position = SimpleNamespace(
        id="P-1",
        instrument_id="H.POLYMARKET",
        opening_order_id="O-1",
    )
    ctx = live_context(
        infos={"H.POLYMARKET": {"selection_role": "home", "claim": "yes"}},
        positions=[position],
        order_history=[order],
        sports_store=_SportsStore(current_score),
    )
    ctx.pair_registry.register(ctx.pair_id, ["H.POLYMARKET"], game_id=42)
    ctx.scratch["legs"] = [{
        "instrument_id": "H.POLYMARKET",
        "position_id": "P-1",
        "side": "SELL",
    }]
    return ctx


def test_keeps_position_close_leg_in_opening_set():
    ctx = _ctx("6-4, 1-1", "6-4, 1-3")

    _run(SameSetAction(limit=True).execute(ctx))

    assert len(ctx.scratch["legs"]) == 1


def test_missing_limit_enforces_same_set():
    ctx = _ctx("1-1", "6-4, 0-1")

    _run(SameSetAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_drops_position_close_leg_after_set_changes():
    ctx = _ctx("6-4, 1-3", "6-4, 3-6, 0-1")

    _run(SameSetAction(limit=True).execute(ctx))

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize(("opening_score", "current_score"), [("", "1-2"), ("1-1", "bad")])
def test_missing_or_invalid_score_fails_closed(opening_score, current_score):
    ctx = _ctx(opening_score, current_score)

    _run(SameSetAction(limit=True).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_disabled_is_noop():
    ctx = _ctx("1-1", "6-4, 0-1")
    before = ctx.scratch["legs"]

    _run(SameSetAction(limit=False).execute(ctx))

    assert ctx.scratch["legs"] is before


@pytest.mark.parametrize("value", [0, 1, "true", {}, []])
def test_invalid_limit_is_rejected(value):
    with pytest.raises(ValueError, match="limit must be a boolean"):
        SameSetAction(limit=value)
