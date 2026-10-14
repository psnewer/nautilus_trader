"""赛前当前低 ask 偏移挂单及方向反转撤单。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.checks.pre_game_order import PreGameOrderCheck
from src.arbitrage.strategy.checks.pre_game_reverse import PreGameReverseCheck
from src.arbitrage.strategy.checks.select_orders import CANCEL_CANDIDATES
from tests.arbitrage.strategy._live_state import live_context


def _ctx(*, books=None, orders=None):
    return live_context(
        books=books,
        orders=orders,
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        strategy_defaults={"share": 10.0, "max_leg_share": 10.0},
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _select(ctx, *order_ids):
    ctx.scratch[CANCEL_CANDIDATES] = list(order_ids)
    return ctx


def test_pre_game_order_selects_lower_current_ask_and_applies_offset():
    ctx = _ctx(books={
        "Y.POLYMARKET": {"ask": 0.43},
        "N.POLYMARKET": {"ask": 0.57},
    })

    assert PreGameOrderCheck(offset=0.2).passes(ctx) is True
    assert ctx.scratch["legs"][0]["claim"] == "yes"
    assert ctx.scratch["legs"][0]["price"] == pytest.approx(0.344)

    _run(PlaceBetsAction(origin="pre_game", limit=False).execute(ctx))
    order = ctx.scratch["execution_plan"].orders[0]
    assert order.spec["price"] == pytest.approx(0.344)
    assert order.spec["intent"] == "arbitrage"
    assert order.spec["origin"] == "pre_game"
    assert "post_only" not in order.spec


@pytest.mark.parametrize(
    "books",
    [
        {"Y.POLYMARKET": {"ask": 0.5}, "N.POLYMARKET": {"ask": 0.5}},
        {"Y.POLYMARKET": {"ask": 0.43}},
    ],
)
def test_pre_game_order_requires_complete_unequal_current_asks(books):
    assert PreGameOrderCheck(offset=0.2).passes(_ctx(books=books)) is False


def test_reverse_cancels_when_order_direction_ask_is_not_lowest():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=1,
        side="BUY",
        tags=["arb:intent=arbitrage", "arb:origin=pre_game"],
    )
    ctx = _ctx(
        books={"Y.POLYMARKET": {"ask": 0.55}, "N.POLYMARKET": {"ask": 0.45}},
        orders=[order],
    )

    assert PreGameReverseCheck().passes(_select(ctx, "O-1")) is True
    assert ctx.scratch["cancel_pair_orders"] == {
        "reason": "pre_game_reverse",
        "client_order_ids": ["O-1"],
    }


def test_reverse_does_not_cancel_when_order_direction_is_lowest_or_equal():
    order = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="O-1",
        ts_submitted=1,
        side="BUY",
        tags=["arb:intent=arbitrage", "arb:origin=pre_game"],
    )
    for books in (
        {"Y.POLYMARKET": {"ask": 0.45}, "N.POLYMARKET": {"ask": 0.55}},
        {"Y.POLYMARKET": {"ask": 0.5}, "N.POLYMARKET": {"ask": 0.5}},
    ):
        ctx = _select(_ctx(books=books, orders=[order]), "O-1")
        assert PreGameReverseCheck().passes(ctx) is False


def test_reverse_restores_economic_outcome_for_inventory_sell_orders():
    orders = [
        SimpleNamespace(
            instrument_id="N.POLYMARKET",
            client_order_id=client_order_id,
            ts_submitted=1,
            side="SELL",
            tags=["arb:intent=arbitrage", "arb:origin=pre_game"],
        )
        for client_order_id in ("O-1", "O-2")
    ]
    ctx = _ctx(
        books={"Y.POLYMARKET": {"ask": 0.55}, "N.POLYMARKET": {"ask": 0.45}},
        orders=orders,
    )

    assert PreGameReverseCheck().passes(_select(ctx, "O-1", "O-2")) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-1", "O-2"]


def test_reverse_requires_selector_and_ignores_unselected_orders():
    ordinary = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="ordinary",
        ts_submitted=1,
        side="BUY",
        tags=["arb:intent=arbitrage", "arb:origin=pre_game"],
    )
    position = SimpleNamespace(
        instrument_id="Y.POLYMARKET",
        client_order_id="position",
        ts_submitted=1,
        side="SELL",
        tags=["arb:intent=recovery", "arb:origin=pre_game"],
    )
    ctx = _ctx(
        books={"Y.POLYMARKET": {"ask": 0.55}, "N.POLYMARKET": {"ask": 0.45}},
        orders=[ordinary, position],
    )

    assert PreGameReverseCheck().passes(ctx) is False
    assert PreGameReverseCheck().passes(_select(ctx, "ordinary")) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["ordinary"]


@pytest.mark.parametrize("offset", [0, 1, -0.1, float("nan"), True])
def test_pre_game_order_rejects_invalid_offset(offset):
    with pytest.raises(ValueError, match="offset must be a finite number"):
        PreGameOrderCheck(offset=offset)
