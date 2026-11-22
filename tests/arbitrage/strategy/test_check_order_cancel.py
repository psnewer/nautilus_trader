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
    instrument_id: str = "Y.POLYMARKET",
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
        instrument_id=instrument_id,
        side=side,
        price=price,
        tags=tags,
    )


def _ctx(
    *,
    orders,
    positions=None,
    order_history=None,
    ask=0.40,
    bid=None,
    other_ask=None,
    other_bid=None,
    score="0-0",
    period="S1",
    first_serve="home",
):
    bid = ask - 0.01 if bid is None else bid
    other_ask = 1.0 - ask if other_ask is None else other_ask
    other_bid = 1.0 - bid if other_bid is None else other_bid
    ctx = live_context(
        books={
            "Y.POLYMARKET": {"bid": bid, "ask": ask},
            "N.POLYMARKET": {"bid": other_bid, "ask": other_ask},
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes", "selection_role": "home"},
            "N.POLYMARKET": {"claim": "no", "selection_role": "away"},
        },
        orders=orders,
        positions=positions,
        order_history=order_history,
        sports_store=SimpleNamespace(
            get=lambda game_id: SimpleNamespace(
                score=score,
                period=period,
                first_serve=first_serve,
            ),
        ),
        in_game_position_store=InGamePositionRuntimeStore(),
    )
    ctx.pair_registry.register(
        ctx.pair_id,
        ["Y.POLYMARKET", "N.POLYMARKET"],
        game_id=42,
    )
    ctx.cache.position_id = lambda order_id: None
    return ctx


def _bind_position(ctx, order, *, opening_score):
    opening_order = _order("opening", score=opening_score)
    position = SimpleNamespace(
        id="position-1",
        instrument_id=order.instrument_id,
        opening_order_id=opening_order.client_order_id,
    )
    ctx.cache._positions = [position]
    ctx.cache._order_history = [opening_order, order]
    ctx.cache.position_id = lambda order_id: (
        position.id if str(order_id) == str(order.client_order_id) else None
    )


def test_requires_explicit_selected_candidates():
    ctx = _ctx(orders=[_order("o1")], ask=0.60)

    assert OrderCancelCheck(price_drift=0.05).eval(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


def test_price_drift_only_cancels_selected_order():
    ctx = _ctx(orders=[_order("selected"), _order("other")], ask=0.46)
    ctx.scratch[CANCEL_CANDIDATES] = ["selected"]

    assert OrderCancelCheck(book_side="ask", price_drift=0.05).eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["selected"]


def test_force_cancels_only_selected_open_orders_without_other_conditions():
    ctx = _ctx(
        orders=[_order("selected"), _order("other")],
        ask=0.46,
        other_ask=0.60,
    )
    ctx.sports_store = None
    ctx.scratch[CANCEL_CANDIDATES] = ["selected", "missing"]

    assert OrderCancelCheck(force=True).eval(ctx) is True
    request = ctx.scratch["cancel_pair_orders"]
    assert request["client_order_ids"] == ["selected"]
    assert request["force"] is True


def test_force_requires_selected_active_order():
    ctx = _ctx(orders=[_order("other")])
    ctx.scratch[CANCEL_CANDIDATES] = ["missing"]

    assert OrderCancelCheck(force=True).eval(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


def test_price_drift_includes_threshold_with_float_tolerance():
    ctx = _ctx(orders=[_order("o1")], ask=0.45)
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(book_side="ask", price_drift=0.05).eval(ctx) is True


def test_serve_plus_raises_price_drift_threshold_on_opponent_serve():
    order = _order("o1", score="6-4, 1-0")
    ctx = _ctx(orders=[order], ask=0.46, other_ask=0.54, first_serve="home")
    _bind_position(ctx, order, opening_score="6-4")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is False

    ctx = _ctx(orders=[order], ask=0.47, other_ask=0.53, first_serve="home")
    _bind_position(ctx, order, opening_score="6-4")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True
    assert ctx.scratch["cancel_pair_orders"]["serve_plus"] == 0.02


def test_serve_plus_uses_order_outcome_when_identifying_opponent_serve():
    order = _order(
        "o1",
        instrument_id="N.POLYMARKET",
        price=0.60,
        score="6-4, 0-0",
    )
    ctx = _ctx(orders=[order], ask=0.33, other_ask=0.67, first_serve="home")
    _bind_position(ctx, order, opening_score="6-4")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True


def test_serve_plus_is_not_added_when_order_target_was_serving():
    ctx = _ctx(orders=[_order("o1", score="0-0")], ask=0.45, other_ask=0.55)
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True


def test_serve_plus_is_not_added_when_order_and_position_are_in_same_set():
    order = _order("o1", score="1-0")
    ctx = _ctx(orders=[order], ask=0.46, other_ask=0.54, first_serve="home")
    _bind_position(ctx, order, opening_score="0-0")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True


def test_serve_plus_is_not_added_without_position_set_evidence():
    order = _order("o1", score="1-0")
    ctx = _ctx(orders=[order], ask=0.46, other_ask=0.54, first_serve="home")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True


@pytest.mark.parametrize(
    ("order_score_value", "first_serve"),
    [
        (None, "home"),
        ("PRE", "home"),
        ("6-6", "home"),
        ("1-0", ""),
    ],
)
def test_serve_plus_falls_back_to_base_drift_without_reliable_serve_evidence(
    order_score_value,
    first_serve,
):
    ctx = _ctx(
        orders=[_order("o1", score=order_score_value)],
        ask=0.45,
        other_ask=0.55,
        first_serve=first_serve,
    )
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(price_drift=0.05, serve_plus=0.02).eval(ctx) is True


@pytest.mark.parametrize(
    ("book_side", "ask", "bid", "other_ask", "other_bid"),
    [
        ("ask", 0.46, 0.10, 0.54, 0.10),
        ("bid", 0.80, 0.46, 0.80, 0.54),
    ],
)
def test_price_drift_commission_uses_configured_book_side(
    book_side,
    ask,
    bid,
    other_ask,
    other_bid,
):
    ctx = _ctx(
        orders=[_order("o1")],
        ask=ask,
        bid=bid,
        other_ask=other_ask,
        other_bid=other_bid,
    )
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(book_side=book_side, price_drift=0.05).eval(ctx) is True


@pytest.mark.parametrize(
    ("book_side", "ask", "bid", "other_ask", "other_bid"),
    [
        ("ask", 0.46, 0.46, 0.60, 0.54),
        ("bid", 0.46, 0.46, 0.54, 0.60),
    ],
)
def test_price_drift_fails_closed_when_selected_side_commission_is_invalid(
    book_side,
    ask,
    bid,
    other_ask,
    other_bid,
):
    ctx = _ctx(
        orders=[_order("o1")],
        ask=ask,
        bid=bid,
        other_ask=other_ask,
        other_bid=other_bid,
    )
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(book_side=book_side, price_drift=0.05).eval(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


def test_invalid_commission_only_disables_price_drift_signal():
    ctx = _ctx(
        orders=[_order("o1", score="0-0")],
        ask=0.46,
        other_ask=0.60,
        score="1-0",
    )
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert (
        OrderCancelCheck(
            book_side="ask",
            price_drift=0.05,
            game_drift=1,
        ).eval(ctx)
        is True
    )


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


@pytest.mark.parametrize(
    ("score", "period"),
    [
        ("6-6", "S1"),
        ("6-6(3-4)", "S1"),
        ("", "TB1"),
        ("5-5", "tb3"),
    ],
)
def test_tie_break_cancels_selected_order(score, period):
    ctx = _ctx(orders=[_order("o1")], score=score, period=period)
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(tie_break=True).eval(ctx) is True
    request = ctx.scratch["cancel_pair_orders"]
    assert request["client_order_ids"] == ["o1"]
    assert request["tie_break"] is True


def test_tie_break_does_not_trigger_before_six_all_or_tb_period():
    ctx = _ctx(orders=[_order("o1")], score="5-5", period="S1")
    ctx.scratch[CANCEL_CANDIDATES] = ["o1"]

    assert OrderCancelCheck(tie_break=True).eval(ctx) is False
    assert "cancel_pair_orders" not in ctx.scratch


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


def test_condition_select_orders_feeds_order_cancel_for_all_open_orders():
    orders = [_order("first", price=0.40), _order("second", price=0.41)]
    ctx = _ctx(orders=orders, ask=0.42, other_ask=0.58)
    branch = AndCheckExpr(
        SelectOrdersCheck(),
        OrderCancelCheck(book_side="ask", price_drift=0.01),
    )

    assert branch.eval(ctx) is True
    assert ctx.scratch[CANCEL_CANDIDATES] == ["first", "second"]
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["first", "second"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"book_side": "mid", "price_drift": 0.1},
        {"price_drift": -0.1},
        {"price_drift": 1.0},
        {"serve_plus": 0.01, "game_drift": 1},
        {"price_drift": 0.05, "serve_plus": -0.01},
        {"price_drift": 0.05, "serve_plus": True},
        {"price_drift": 0.9, "serve_plus": 0.1},
        {"game_drift": -1},
        {"game_drift": True},
        {"min_tp_set": 0},
        {"min_sl_set": True},
        {"min_tp_set": 1, "min_sl_set": 1},
        {"tie_break": 1},
        {"force": 1},
    ],
)
def test_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        OrderCancelCheck(**kwargs)
