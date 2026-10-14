"""select_orders：先筛选订单子集，再交给统一撤单 Check。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.checks.select_orders import CANCEL_CANDIDATES
from src.arbitrage.strategy.checks.select_orders import SelectOrdersCheck
from tests.arbitrage.strategy._live_state import live_context


def _order(order_id: str, *, side: str = "BUY", origin: str = "pre_game"):
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id="Y.POLYMARKET",
        side=side,
        tags=[f"arb:origin={origin}"],
    )


def _ctx():
    position = _order("position", side="SELL")
    ordinary = _order("ordinary")
    ctx = live_context(
        infos={"Y.POLYMARKET": {"claim": "yes"}},
        orders=[position, ordinary],
    )
    ctx.cache.position_id = lambda order_id: "P-1" if str(order_id) == "position" else None
    return ctx


def test_selects_position_orders_only():
    ctx = _ctx()

    assert SelectOrdersCheck(position_order=True).eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["position"]


def test_selects_ordinary_orders_only():
    ctx = _ctx()

    assert SelectOrdersCheck(position_order=False).eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["ordinary"]


def test_omitted_filter_selects_all_orders():
    ctx = _ctx()

    assert SelectOrdersCheck().eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["ordinary", "position"]


def test_origin_and_position_order_filters_are_both_applied_per_order():
    pre_position = _order("pre-position", side="SELL", origin="pre_game")
    pre_ordinary = _order("pre-ordinary", origin="pre_game")
    in_position = _order("in-position", side="SELL", origin="in_game")
    in_ordinary = _order("in-ordinary", origin="in_game")
    ctx = live_context(
        infos={"Y.POLYMARKET": {"claim": "yes"}},
        orders=[pre_position, pre_ordinary, in_position, in_ordinary],
    )
    ctx.cache.position_id = lambda order_id: (
        "P-1" if str(order_id) in {"pre-position", "in-position"} else None
    )

    assert SelectOrdersCheck(origin="in_game", position_order=False).eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["in-ordinary"]


def test_empty_selection_fails_without_leaking_scratch():
    ctx = live_context(infos={"Y.POLYMARKET": {"claim": "yes"}})
    ctx.scratch["existing"] = 1

    assert SelectOrdersCheck(position_order=True).eval(ctx) is False
    assert ctx.scratch == {"existing": 1}


@pytest.mark.parametrize("value", [0, 1, "true", object()])
def test_rejects_non_boolean_filter(value):
    with pytest.raises(ValueError, match="position_order must be a boolean"):
        SelectOrdersCheck(position_order=value)


@pytest.mark.parametrize("origin", ["pre", "live", "", 1, True])
def test_rejects_invalid_origin(origin):
    with pytest.raises(ValueError, match="origin must be pre_game or in_game"):
        SelectOrdersCheck(origin=origin)
