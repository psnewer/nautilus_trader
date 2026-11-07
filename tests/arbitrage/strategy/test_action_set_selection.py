"""set_selection:只按已完成盘胜数筛选执行腿或撤单目标。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.set_selection import SetSelectionAction
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


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(score, *, orders=None, positions=None, order_history=None):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        sports_store=_SportsStore(score),
        orders=orders,
        positions=positions,
        order_history=order_history,
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "side": "BUY"},
        {"instrument_id": "A.POLYMARKET", "side": "BUY"},
    ]
    return ctx


def _order(order_id, instrument_id, *, side=None, tags=None):
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id=instrument_id,
        side=side,
        tags=list(tags or ()),
    )


def _position(position_id, instrument_id, opening_order_id):
    return SimpleNamespace(
        id=position_id,
        instrument_id=instrument_id,
        opening_order_id=opening_order_id,
    )


def test_completed_sets_ignore_current_set_games():
    ctx = _ctx("6-4, 2-3")

    _run(SetSelectionAction(standing="lose").execute(ctx))

    assert ctx.scratch["legs"] == [
        {"instrument_id": "A.POLYMARKET", "side": "BUY"},
    ]


def test_tied_completed_sets_are_draw_even_when_current_set_is_not_tied():
    ctx = _ctx("6-4, 2-6, 5-0")

    _run(SetSelectionAction(standing="draw").execute(ctx))

    assert len(ctx.scratch["legs"]) == 2


def test_latest_completed_set_is_counted_before_next_set_arrives():
    ctx = _ctx("6-4")

    _run(SetSelectionAction(standing="win").execute(ctx))

    assert ctx.scratch["legs"] == [
        {"instrument_id": "H.POLYMARKET", "side": "BUY"},
    ]


def test_no_completed_set_is_draw():
    ctx = _ctx("3-2")

    _run(SetSelectionAction(standing="draw").execute(ctx))

    assert len(ctx.scratch["legs"]) == 2


@pytest.mark.parametrize("score", [None, "6-4, bad"])
def test_missing_or_invalid_score_fails_closed(score):
    ctx = _ctx(score)

    _run(SetSelectionAction(standing="lose").execute(ctx))

    assert ctx.scratch["legs"] == []


def test_filters_order_cancel_request_by_order_instrument_set_standing():
    orders = [
        _order("home-order", "H.POLYMARKET"),
        _order("away-order", "A.POLYMARKET"),
    ]
    ctx = _ctx("6-4, 2-3", orders=orders)
    ctx.scratch["cancel_pair_orders"] = {
        "reason": "order_cancel",
        "client_order_ids": ["home-order", "away-order"],
    }

    _run(SetSelectionAction(standing="lose").execute(ctx))

    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["away-order"]


def test_removes_cancel_request_when_no_order_matches_standing():
    ctx = _ctx("6-4, 2-3", orders=[_order("home-order", "H.POLYMARKET")])
    ctx.scratch["cancel_pair_orders"] = {
        "reason": "order_cancel",
        "client_order_ids": ["home-order"],
    }

    _run(SetSelectionAction(standing="lose").execute(ctx))

    assert "cancel_pair_orders" not in ctx.scratch


def test_missing_standing_is_noop_for_cancel_request():
    ctx = _ctx(None)
    request = {
        "reason": "order_cancel",
        "client_order_ids": ["missing-order"],
    }
    ctx.scratch["cancel_pair_orders"] = request

    _run(SetSelectionAction().execute(ctx))

    assert ctx.scratch["cancel_pair_orders"] is request


def test_current_set_exempt_keeps_close_leg_opened_in_current_set():
    opening_order = _order(
        "open-home",
        "H.POLYMARKET",
        tags=["arb:origin=in_game", "arb:order_score=6-4, 1-1"],
    )
    ctx = _ctx(
        "6-4, 2-3",
        positions=[_position("P-home", "H.POLYMARKET", "open-home")],
        order_history=[opening_order],
    )
    ctx.scratch["legs"] = [{
        "instrument_id": "H.POLYMARKET",
        "position_id": "P-home",
        "side": "SELL",
    }]

    _run(SetSelectionAction(standing="lose", current_set_exempt=True).execute(ctx))

    assert len(ctx.scratch["legs"]) == 1


def test_current_set_exempt_defaults_to_disabled():
    opening_order = _order(
        "open-home",
        "H.POLYMARKET",
        tags=["arb:origin=in_game", "arb:order_score=6-4, 1-1"],
    )
    ctx = _ctx(
        "6-4, 2-3",
        positions=[_position("P-home", "H.POLYMARKET", "open-home")],
        order_history=[opening_order],
    )
    ctx.scratch["legs"] = [{
        "instrument_id": "H.POLYMARKET",
        "position_id": "P-home",
        "side": "SELL",
    }]

    _run(SetSelectionAction(standing="lose").execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_set_exempt_does_not_keep_close_leg_opened_in_previous_set():
    opening_order = _order(
        "open-home",
        "H.POLYMARKET",
        tags=["arb:origin=in_game", "arb:order_score=2-1"],
    )
    ctx = _ctx(
        "6-4, 2-3",
        positions=[_position("P-home", "H.POLYMARKET", "open-home")],
        order_history=[opening_order],
    )
    ctx.scratch["legs"] = [{
        "instrument_id": "H.POLYMARKET",
        "position_id": "P-home",
        "side": "SELL",
    }]

    _run(SetSelectionAction(standing="lose", current_set_exempt=True).execute(ctx))

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize(
    "tags",
    [
        ["arb:origin=pre_game", "arb:order_score=6-4, 1-1"],
        ["arb:origin=in_game", "arb:order_score=bad"],
        [],
    ],
)
def test_current_set_exempt_requires_in_game_opening_evidence(tags):
    opening_order = _order("open-home", "H.POLYMARKET", tags=tags)
    ctx = _ctx(
        "6-4, 2-3",
        positions=[_position("P-home", "H.POLYMARKET", "open-home")],
        order_history=[opening_order],
    )
    ctx.scratch["legs"] = [{
        "instrument_id": "H.POLYMARKET",
        "position_id": "P-home",
        "side": "SELL",
    }]

    _run(SetSelectionAction(standing="lose", current_set_exempt=True).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_current_set_exempt_does_not_apply_to_cancel_request():
    close_order = _order("close-home", "H.POLYMARKET", side="SELL")
    opening_order = _order(
        "open-home",
        "H.POLYMARKET",
        tags=["arb:origin=in_game", "arb:order_score=6-4, 1-1"],
    )
    ctx = _ctx(
        "6-4, 2-3",
        orders=[close_order],
        positions=[_position("P-home", "H.POLYMARKET", "open-home")],
        order_history=[close_order, opening_order],
    )
    ctx.cache.position_id = lambda client_order_id: (
        "P-home" if str(client_order_id) == "close-home" else None
    )
    ctx.scratch["cancel_pair_orders"] = {
        "reason": "order_cancel",
        "client_order_ids": ["close-home"],
    }

    _run(SetSelectionAction(standing="lose", current_set_exempt=True).execute(ctx))

    assert "cancel_pair_orders" not in ctx.scratch


@pytest.mark.parametrize("standing", ["", "win||lose", "ahead", True])
def test_invalid_standing_is_rejected(standing):
    with pytest.raises(ValueError):
        SetSelectionAction(standing=standing)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_invalid_current_set_exempt_is_rejected(value):
    with pytest.raises(ValueError, match="current_set_exempt must be a boolean"):
        SetSelectionAction(current_set_exempt=value)
