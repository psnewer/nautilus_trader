"""order_cancel:只对 condition/兼容 selector 产出的候选执行 opt-in 撤单判定。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.checks.order_cancel import OrderCancelCheck
from src.arbitrage.strategy.checks.select_orders import CANCEL_CANDIDATES
from src.arbitrage.strategy.checks.select_orders import SelectOrdersCheck
from src.arbitrage.strategy.condition import AndCheckExpr
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from src.arbitrage.strategy.queries.exposure_source import ExposureSourceQuery
from tests.arbitrage.strategy._live_state import live_context


def _order(
    order_id: str,
    *,
    price: float = 0.40,
    score: str | None = "0-0",
    intent: str = "arbitrage",
    side: str = "BUY",
):
    tags = [f"arb:intent={intent}"]
    tags.append("arb:origin=in_game")
    if score is not None:
        tags.append(f"arb:order_score={score}")
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id="Y.POLYMARKET",
        side=side,
        price=price,
        tags=tags,
    )


def _ctx(*, orders, ask=0.40, score="0-0", period="S1"):
    ctx = live_context(
        books={"Y.POLYMARKET": {"bid": ask - 0.01, "ask": ask}},
        infos={"Y.POLYMARKET": {"claim": "yes"}},
        orders=orders,
        sports_store=SimpleNamespace(
            get=lambda game_id: SimpleNamespace(score=score, period=period),
        ),
        in_game_position_store=InGamePositionRuntimeStore(),
    )
    ctx.pair_registry.register(ctx.pair_id, ["Y.POLYMARKET"], game_id=42)
    ctx.cache.position_id = lambda order_id: None
    return ctx


def test_requires_explicit_selected_candidates():
    ctx = _ctx(orders=[_order("o1")], ask=0.60)

    assert OrderCancelCheck(price_drift=0.05).eval(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


def test_price_drift_only_cancels_selected_order():
    ctx = _ctx(orders=[_order("selected"), _order("other")], ask=0.46)
    ctx.scratch[CANCEL_CANDIDATES] = ["selected"]

    assert OrderCancelCheck(book_side="ask", price_drift=0.05).eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["selected"]


def test_price_drift_includes_threshold_with_float_tolerance():
    ctx = _ctx(orders=[_order("o1")], ask=0.45)
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(book_side="ask", price_drift=0.05).eval(ctx) is True


def test_missing_order_score_uses_zero_game_baseline():
    ctx = _ctx(orders=[_order("o1", score=None)], score="1-0")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(game_drift=0).eval(ctx) is True


def test_game_drift_includes_threshold():
    order = _order("o1", score="1-1")
    ctx = _ctx(orders=[order], score="2-1")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]
    assert OrderCancelCheck(game_drift=1).eval(ctx) is True

    ctx.sports_store = SimpleNamespace(
        get=lambda game_id: SimpleNamespace(score="1-1", period="S1"),
    )
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]
    assert OrderCancelCheck(game_drift=1).eval(ctx) is False


def test_min_tp_set_only_applies_to_recovery_order_bound_to_position():
    recovery = _order("recovery", intent="recovery")
    ordinary = _order("ordinary", intent="arbitrage")
    ctx = _ctx(orders=[recovery, ordinary], period="S1")
    ctx.cache.position_id = lambda order_id: "P-1" if str(order_id) == "recovery" else None
    ctx.scratch[CANCEL_CANDIDATES] = ["recovery", "ordinary"]

    assert OrderCancelCheck(min_tp_set=2).eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["recovery"]


def test_runtime_tp_and_sl_thresholds_are_independent():
    recovery = _order("recovery", intent="recovery")
    ctx = _ctx(orders=[recovery], period="S2")
    ctx.cache.position_id = lambda order_id: "P-1"
    ctx.in_game_position_store.set_min_tp_set(ctx.pair_id, 3)
    ctx.in_game_position_store.set_min_sl_set(ctx.pair_id, 2)

    ctx.scratch[CANCEL_CANDIDATES] = ["recovery"]
    assert OrderCancelCheck(min_tp_set=1).eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["min_tp_set"] == 3

    ctx.scratch.pop("cancel_pair_orders")
    ctx.scratch[CANCEL_CANDIDATES] = ["recovery"]
    assert OrderCancelCheck(min_sl_set=1).eval(ctx) is False


def test_and_chain_keeps_position_and_ordinary_orders_separate():
    position = _order("position", side="SELL", intent="recovery")
    ordinary = _order("ordinary")
    ctx = _ctx(orders=[position, ordinary], ask=0.46)
    ctx.cache.position_id = lambda order_id: "P-1" if str(order_id) == "position" else None

    position_branch = AndCheckExpr(
        SelectOrdersCheck(position_order=True),
        OrderCancelCheck(price_drift=0.10),
    )
    assert position_branch.eval(ctx) is False
    assert CANCEL_CANDIDATES not in ctx.scratch

    ordinary_branch = AndCheckExpr(
        SelectOrdersCheck(position_order=False),
        OrderCancelCheck(price_drift=0.05),
    )
    assert ordinary_branch.eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["ordinary"]


def test_condition_exposure_source_feeds_order_cancel_without_select_check():
    position = _order("position", side="SELL", intent="recovery")
    ordinary = _order("ordinary")
    ctx = _ctx(orders=[position, ordinary], ask=0.46)
    ctx.cache.position_id = lambda order_id: "P-1" if str(order_id) == "position" else None
    branch = Condition(
        self_hits=ExposureSourceQuery("in_game_order", position_order=False),
        checktion=OrderCancelCheck(price_drift=0.05),
    )

    assert evaluate_tree(branch, ctx).hit is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["ordinary"]
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["ordinary"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"book_side": "mid", "price_drift": 0.1},
        {"price_drift": -0.1},
        {"price_drift": 1.0},
        {"game_drift": -1},
        {"game_drift": True},
        {"min_tp_set": 0},
        {"min_sl_set": True},
        {"min_tp_set": 1, "min_sl_set": 1},
    ],
)
def test_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        OrderCancelCheck(**kwargs)
