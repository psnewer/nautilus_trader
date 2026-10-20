"""
pre_rebate 全配置场景:通过正式配置 loader/dispatcher 加载真实 arb_config.json。

按赛事等级(lower / NOT lower)x 阶段(pre_game / in_game)x 来源(order / position)x
是否针对仓位,逐分支构造上下文,断言配置实际路由到预期分支并产出预期计划。

验证策略树路由、Check 产出及命中分支的完整 Action 链(最终 execution_plan、撤单计划、
TP/SL 盘数门槛运行时写入)。不启动 TradingNode,不进入 Risk / Execution / barrier。赛中 one_side
场景使用真实配置连续评估两次,覆盖趋势、连续触发、比分、venue_replace、share/commission
门控及最终 place_bets 参数。

arb_config.json 被 .gitignore 忽略;缺失时跳过(本地有配置才跑真配置回归)。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.common.sports_phase import PHASE_PRE
from src.arbitrage.config import load_arb_config
from src.arbitrage.config.dispatcher import to_strategy_registry
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.condition import evaluate_tree_to_plan
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from tests.arbitrage.strategy._live_state import live_context


_CONFIG = Path(__file__).resolve().parents[5] / "arb_config.json"
_PAIR = "p"
_GAME = 42
_INFOS = {
    "Y.POLYMARKET": {"claim": "yes", "selection_role": "home"},
    "N.POLYMARKET": {"claim": "no", "selection_role": "away"},
    "A.ORBITEXCH": {"competition": "Tennis"},
}
_CROSS_VENUE_INFOS = {
    "Y.POLYMARKET": {"claim": "yes", "selection_role": "home"},
    "N.POLYMARKET": {"claim": "no", "selection_role": "away"},
    "N.ORBITEXCH": {"claim": "no", "selection_role": "away"},
}
_CROSS_VENUE_BOOKS = {
    "Y.POLYMARKET": {"ask": 0.45, "bid": 0.43},
    "N.POLYMARKET": {"ask": 0.55, "bid": 0.53},
    "N.ORBITEXCH": {"ask": 0.45, "bid": 0.44},
}


class _EmptyPortfolio:
    """真实 action 链需要 Portfolio;该场景表示 pair 当前没有任何仓位。"""

    def outcome_shares(self, pair_id, account_id=None):
        return {"yes": 0.0, "no": 0.0}

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        return {"yes": 0.0, "no": 0.0}


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fire(result, ctx):
    async def execute_all():
        for action in result.pending_actions:
            await action.execute(ctx)

    _run(execute_all())


@pytest.fixture(scope="module")
def trees():
    if not _CONFIG.exists():
        pytest.skip("arb_config.json not present")
    from launchers import arb_node

    arb_node.register_builtin_checks_and_actions()
    cfg = load_arb_config(_CONFIG)
    strategy = to_strategy_registry(cfg).get_for(None, None, "Tennis")
    assert strategy is not None
    assert strategy.metadata["id"] == "pre_rebate"
    return strategy.arbitrage_tree, strategy.compensation_tree


def _order(
    coid,
    *,
    instrument_id="Y.POLYMARKET",
    side="BUY",
    price=0.40,
    origin="pre_game",
    intent="arbitrage",
    magic=None,
    score="0-0",
    ts=1,
):
    tags = [f"arb:origin={origin}", f"arb:intent={intent}"]
    if magic is not None:
        tags.append(f"arb:magic={magic}")
    if score is not None:
        tags.append(f"arb:order_score={score}")
    return SimpleNamespace(
        client_order_id=coid,
        instrument_id=instrument_id,
        side=side,
        price=price,
        tags=tags,
        ts_submitted=ts,
    )


def _position(
    *,
    instrument_id="Y.POLYMARKET",
    position_id="P-1",
    opening_order_id="O-open",
    quantity=5.0,
    side="LONG",
):
    return SimpleNamespace(
        id=position_id,
        instrument_id=instrument_id,
        side=side,
        quantity=quantity,
        opening_order_id=opening_order_id,
    )


def _ctx(
    *,
    tier="lower",
    phase="in",
    score="0-0",
    period="S1",
    books=None,
    orders=(),
    positions=(),
    order_history=None,
    position_ids=None,
    min_set_store=None,
    consecutive_trigger_store=None,
    event_name=None,
    trend_price=None,
    infos=None,
    portfolio=None,
    tree_kind=None,
    share=10.0,
):
    merged_books = {
        "Y.POLYMARKET": {"ask": 0.45, "bid": 0.43},
        "N.POLYMARKET": {"ask": 0.57, "bid": 0.55},
    }
    if books:
        merged_books.update(books)
    phase_value = PHASE_PRE if phase == "pre" else PHASE_IN_PLAY
    store = min_set_store if min_set_store is not None else InGamePositionRuntimeStore()
    resolved_infos = dict(infos or _INFOS)
    ctx = live_context(
        pair_id=_PAIR,
        books=merged_books,
        infos=resolved_infos,
        instrument_ids=list(resolved_infos),
        orders=list(orders),
        order_history=order_history,
        positions=list(positions),
        sports_store=SimpleNamespace(
            get=lambda game_id: SimpleNamespace(score=score, period=period),
        ),
        phase_store=SimpleNamespace(
            get=lambda game_id: SimpleNamespace(phase=phase_value),
        ),
        in_game_position_store=store,
        consecutive_trigger_store=consecutive_trigger_store,
        event_name=event_name,
        portfolio=portfolio,
        tree_kind=tree_kind,
        strategy_defaults={"share": share, "max_leg_share": share},
    )
    ctx.pair_registry.register(_PAIR, list(resolved_infos), game_id=_GAME)
    competition = "ATP Challenger Lisbon" if tier == "lower" else "ATP Masters 1000 Shanghai"
    for instrument_id in resolved_infos:
        if instrument_id.endswith(".ORBITEXCH"):
            ctx.cache.instrument(instrument_id).competition_name = competition
    if trend_price is not None:
        price_store = PairPriceStore(ctx.cache)
        price_store.initialize(_PAIR, ["yes", "no"])
        price_store.update_trend(_PAIR, trend_price)
    resolved_ids = dict(position_ids or {})
    ctx.cache.position_id = lambda coid: resolved_ids.get(str(coid))
    return ctx


# ───────────────────────── 套利树 ─────────────────────────


def test_lower_pre_game_places_pre_game_order(trees):
    arb_tree, _ = trees
    ctx = _ctx(
        tier="lower",
        phase="pre",
        tree_kind="arbitrage",
        books={"Y.POLYMARKET": {"ask": 0.43, "bid": 0.41}, "N.POLYMARKET": {"ask": 0.57, "bid": 0.55}},
    )

    result = evaluate_tree(arb_tree, ctx)

    assert result.hit is True
    legs = ctx.scratch["legs"]
    assert len(legs) == 1
    assert legs[0]["claim"] == "yes"
    assert legs[0]["side"] == "BUY"
    assert legs[0]["price"] == pytest.approx(0.43 * 0.8)
    _fire(result, ctx)
    plan = ctx.scratch["execution_plan"]
    assert plan.kind == "submit"
    assert len(plan.orders) == 1
    assert plan.orders[0].spec["price"] == pytest.approx(0.43 * 0.8)
    assert plan.orders[0].spec["origin"] == "pre_game"


def test_regular_pre_game_has_no_arbitrage_branch(trees):
    arb_tree, _ = trees
    ctx = _ctx(tier="regular", phase="pre")

    # 非低级别套利树只有 in_game 分支;赛前不下单。
    assert evaluate_tree(arb_tree, ctx).hit is False


def test_lower_in_game_no_arbitrage_without_one_side_opportunity(trees):
    arb_tree, _ = trees
    # yes/no ask 之和 ~1,无跨 venue one_side 机会 → one_side_rebate 不命中。
    ctx = _ctx(tier="lower", phase="in")

    assert evaluate_tree(arb_tree, ctx).hit is False


def _in_game_opportunity_ctx(
    *,
    tier,
    score,
    trigger_store,
    books=None,
    period="S1",
    orders=(),
):
    return _ctx(
        tier=tier,
        phase="in",
        score=score,
        period=period,
        books=books or _CROSS_VENUE_BOOKS,
        orders=orders,
        infos=_CROSS_VENUE_INFOS,
        portfolio=_EmptyPortfolio(),
        tree_kind="arbitrage",
        event_name="MarketOrderBookDeltas",
        consecutive_trigger_store=trigger_store,
        trend_price={"yes": 0.40, "no": 0.50},
    )


@pytest.mark.parametrize(
    ("tier", "expected_price"),
    [
        ("lower", 0.46),  # PM NO ask 0.55 → limit bid 0.53 → spread 0.07
        ("regular", 0.55),  # limit=false,直接使用 venue_replace 后 PM NO ask
    ],
)
def test_in_game_full_action_chain_reaches_configured_execution_plan(
    trees,
    tier,
    expected_price,
):
    """覆盖真实配置中此前完全未执行的完整 one_side action 链。"""
    arb_tree, _ = trees
    trigger_store = ConsecutiveTriggerStore()

    first = _in_game_opportunity_ctx(tier=tier, score="0-0", trigger_store=trigger_store)
    first_result = evaluate_tree(arb_tree, first)
    assert first_result.hit is True
    _fire(first_result, first)
    assert "execution_plan" not in first.scratch

    second = _in_game_opportunity_ctx(tier=tier, score="1-1", trigger_store=trigger_store)
    second_result = evaluate_tree(arb_tree, second)
    assert second_result.hit is True
    _fire(second_result, second)

    history = trigger_store.history(_PAIR, "pre_rebate_original_leg")
    assert [item.score_raw for item in history] == ["0-0", "1-1"]
    assert [item.passed for item in history] == [False, True]
    plan = second.scratch["execution_plan"]
    assert plan.kind == "submit"
    assert len(plan.orders) == 1
    assert plan.orders[0].spec["instrument_id"] == "N.POLYMARKET"
    assert plan.orders[0].spec["price"] == pytest.approx(expected_price)
    assert plan.orders[0].spec["origin"] == "in_game"
    assert plan.orders[0].spec["post_only"] is False
    assert plan.orders[0].spec["enable_timeout"] is False


def test_in_game_full_chain_is_blocked_by_configured_commission_range(trees):
    arb_tree, _ = trees
    trigger_store = ConsecutiveTriggerStore()
    books = dict(_CROSS_VENUE_BOOKS)
    books["N.POLYMARKET"] = {"ask": 0.60, "bid": 0.58}  # PM commission=1.05

    first = _in_game_opportunity_ctx(
        tier="regular",
        score="0-0",
        trigger_store=trigger_store,
        books=books,
    )
    first_result = evaluate_tree(arb_tree, first)
    _fire(first_result, first)

    second = _in_game_opportunity_ctx(
        tier="regular",
        score="1-1",
        trigger_store=trigger_store,
        books=books,
    )
    second_result = evaluate_tree(arb_tree, second)
    _fire(second_result, second)

    assert second_result.hit is True
    assert second.scratch["legs"] == []
    assert "execution_plan" not in second.scratch


@pytest.mark.parametrize(
    ("tier", "expect_plan"),
    [("lower", True), ("regular", False)],
)
def test_in_game_start_game_order_uses_tier_specific_current_order_gate(
    trees,
    tier,
    expect_plan,
):
    """低级别配置忽略 start_game 挂单,非低级别配置仍将它视为阻拦订单。"""
    arb_tree, _ = trees
    trigger_store = ConsecutiveTriggerStore()
    start_game_order = _order("O-start", intent="start_game", price=0.20)

    for score in ("0-0", "1-1"):
        ctx = _in_game_opportunity_ctx(
            tier=tier,
            score=score,
            trigger_store=trigger_store,
            orders=[start_game_order],
        )
        result = evaluate_tree(arb_tree, ctx)
        _fire(result, ctx)

    assert ("execution_plan" in ctx.scratch) is expect_plan


def test_in_game_set_three_skips_configured_convert(trees):
    """set_exempt=3 只豁免 convert;同方向 PM 腿仍可生成订单。"""
    arb_tree, _ = trees
    trigger_store = ConsecutiveTriggerStore()

    for score in ("0-0", "1-1"):
        ctx = _in_game_opportunity_ctx(
            tier="regular",
            score=score,
            period="S3",
            trigger_store=trigger_store,
        )
        result = evaluate_tree(arb_tree, ctx)
        _fire(result, ctx)

    plan = ctx.scratch["execution_plan"]
    assert len(plan.orders) == 1
    assert plan.orders[0].spec["instrument_id"] == "Y.POLYMARKET"
    assert plan.orders[0].spec["price"] == pytest.approx(0.45)


# ───────────────────────── 补偿树:撤单 ─────────────────────────


def test_lower_pre_game_position_hit_order_cancel_on_price_drift(trees):
    _, comp_tree = trees
    order = _order(
        "O-posp",
        side="SELL",
        origin="in_game",
        intent="recovery",
        magic="in_game_close_pre_game_position_hit",
        price=0.40,
    )
    ctx = _ctx(
        tier="lower",
        phase="in",
        orders=[order],
        position_ids={"O-posp": "P-1"},
        books={"Y.POLYMARKET": {"ask": 0.46, "bid": 0.44}},  # drift 0.06 >= 0.05
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["reason"] == "order_cancel"
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-posp"]
    _fire(result, ctx)
    assert ctx.scratch["execution_plan"].kind == "cancel_pair"


def test_lower_pre_game_nonposition_order_cancel_on_game_drift(trees):
    _, comp_tree = trees
    order = _order("O-prebuy", side="BUY", origin="pre_game", score="0-0")
    ctx = _ctx(
        tier="lower",
        phase="in",
        score="1-0",  # 推进 1 局 >= game_drift 1
        orders=[order],
        books={"Y.POLYMARKET": {"ask": 0.40, "bid": 0.38}},  # 无价格漂移
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["reason"] == "order_cancel"
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-prebuy"]
    _fire(result, ctx)
    assert ctx.scratch["execution_plan"].cancel_order_ids == ("O-prebuy",)


def test_lower_in_game_position_order_cancel_bumps_min_sl_set(trees):
    _, comp_tree = trees
    store = InGamePositionRuntimeStore()
    order = _order(
        "O-ingpos",
        side="SELL",
        origin="in_game",
        intent="recovery",
        magic="in_game_close_in_game_position_sl",
        price=0.40,
    )
    ctx = _ctx(
        tier="lower",
        phase="in",
        period="S2",
        orders=[order],
        position_ids={"O-ingpos": "P-9"},
        min_set_store=store,
        books={"Y.POLYMARKET": {"ask": 0.46, "bid": 0.44}},  # drift 0.06 >= 0.05
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-ingpos"]
    # 跑该分支的 pending actions(place_bets 生成撤单计划 + advance_min_sl_set 写盘数)。
    for action in result.pending_actions:
        _run(action.execute(ctx))
    assert store.min_sl_set(_PAIR) == 3  # current_set S2 + offset 1
    assert store.min_tp_set(_PAIR) is None

    # 同一盘再出现 SL 恢复挂单，即使价格/比分都未漂移，也会被运行时 min_sl_set=3 撤销。
    follow_up = _order(
        "O-ingpos-next",
        side="SELL",
        origin="in_game",
        intent="recovery",
        magic="in_game_close_in_game_position_sl",
        price=0.46,
    )
    follow_up_ctx = _ctx(
        tier="lower",
        phase="in",
        period="S2",
        orders=[follow_up],
        position_ids={"O-ingpos-next": "P-9"},
        min_set_store=store,
        books={"Y.POLYMARKET": {"ask": 0.46, "bid": 0.44}},
    )
    follow_up_result = evaluate_tree(comp_tree, follow_up_ctx)
    assert follow_up_result.hit is True
    assert follow_up_ctx.scratch["cancel_pair_orders"]["min_sl_set"] == 3
    assert follow_up_ctx.scratch["cancel_pair_orders"]["client_order_ids"] == [
        "O-ingpos-next",
    ]


@pytest.mark.parametrize(
    ("score", "expected_hit"),
    [
        ("3-6, 2-1", True),
        ("6-3, 0-1", False),
    ],
)
def test_lower_in_game_tp_order_cancel_filters_by_completed_set_standing(
    trees,
    score,
    expected_hit,
):
    _, comp_tree = trees
    store = InGamePositionRuntimeStore()
    order = _order(
        "O-ingpos-tp",
        side="SELL",
        origin="in_game",
        intent="recovery",
        magic="in_game_close_in_game_position_tp",
    )
    ctx = _ctx(
        tier="lower",
        phase="in",
        score=score,
        period="S2",
        orders=[order],
        position_ids={"O-ingpos-tp": "P-9"},
        min_set_store=store,
    )

    result = _run(evaluate_tree_to_plan(comp_tree, ctx))

    assert result.hit is expected_hit
    if expected_hit:
        assert ctx.scratch["execution_plan"].cancel_order_ids == ("O-ingpos-tp",)
    else:
        assert "execution_plan" not in ctx.scratch
    assert store.min_tp_set(_PAIR) is None


def test_lower_in_game_nonposition_order_cancel_on_price_drift(trees):
    _, comp_tree = trees
    order = _order("O-ingbuy", side="BUY", origin="in_game", price=0.40)
    ctx = _ctx(
        tier="lower",
        phase="in",
        orders=[order],
        books={"Y.POLYMARKET": {"ask": 0.55, "bid": 0.53}},  # drift 0.15 >= 0.14
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-ingbuy"]
    _fire(result, ctx)
    assert ctx.scratch["execution_plan"].kind == "cancel_pair"


def test_regular_in_game_nonposition_order_cancel(trees):
    _, comp_tree = trees
    order = _order("O-reg", side="BUY", origin="in_game", price=0.40)
    ctx = _ctx(
        tier="regular",
        phase="in",
        orders=[order],
        books={"Y.POLYMARKET": {"ask": 0.42, "bid": 0.40}},  # drift 0.02 >= 0.01
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-reg"]
    _fire(result, ctx)
    assert ctx.scratch["execution_plan"].kind == "cancel_pair"


@pytest.mark.parametrize(
    ("tier", "origin", "position_order", "ask", "expected_hit"),
    [
        ("lower", "pre_game", True, 0.4500, True),
        ("lower", "pre_game", True, 0.4499, False),
        ("lower", "in_game", False, 0.5400, True),
        ("lower", "in_game", False, 0.5399, False),
        ("regular", "in_game", False, 0.4100, True),
        ("regular", "in_game", False, 0.4099, False),
    ],
)
def test_order_cancel_uses_exact_configured_price_drift_boundaries(
    trees,
    tier,
    origin,
    position_order,
    ask,
    expected_hit,
):
    _, comp_tree = trees
    order = _order(
        "O-boundary",
        side="SELL" if position_order else "BUY",
        origin=origin,
        price=0.40,
    )
    ctx = _ctx(
        tier=tier,
        phase="in",
        orders=[order],
        position_ids={"O-boundary": "P-1"} if position_order else {},
        books={"Y.POLYMARKET": {"ask": ask, "bid": ask - 0.02}},
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is expected_hit


def test_lower_pre_game_reverse_on_direction_flip(trees):
    _, comp_tree = trees
    order = _order("O-rev", side="BUY", origin="pre_game")
    ctx = _ctx(
        tier="lower",
        phase="pre",
        orders=[order],
        # 订单方向 yes 的 ask 已高于对手 no → 不再最低 → 反转撤单
        books={"Y.POLYMARKET": {"ask": 0.55, "bid": 0.53}, "N.POLYMARKET": {"ask": 0.45, "bid": 0.43}},
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    assert ctx.scratch["cancel_pair_orders"]["reason"] == "pre_game_reverse"
    assert ctx.scratch["cancel_pair_orders"]["client_order_ids"] == ["O-rev"]
    _fire(result, ctx)
    assert ctx.scratch["execution_plan"].kind == "cancel_pair"


# ───────────────────────── 补偿树:仓位平仓 ─────────────────────────


def test_lower_pre_game_position_close_emits_sell_at_bid(trees):
    _, comp_tree = trees
    opening = _order("O-open", side="BUY", origin="pre_game")
    ctx = _ctx(
        tier="lower",
        phase="pre",
        tree_kind="compensation",
        orders=[],  # 无 open order → exposure 走 position 侧
        order_history=[opening],
        positions=[_position(opening_order_id="O-open")],
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    legs = ctx.scratch["legs"]
    assert legs[0]["side"] == "SELL"
    assert legs[0]["price"] == pytest.approx(0.43)  # best bid
    assert legs[0]["position_id"] == "P-1"
    _fire(result, ctx)
    order = ctx.scratch["execution_plan"].orders[0].spec
    # 配置 limit=true 后 SELL 先取 ask=0.45,再应用 spread=0.03。
    assert order["price"] == pytest.approx(0.48)
    assert order["intent"] == "recovery"
    assert order["magic"] == "pre_game_close_pre_game_position"


def test_lower_pre_game_position_close_skips_duplicate_initialized_magic_order(trees):
    _, comp_tree = trees
    opening = _order("O-open", side="BUY", origin="pre_game")
    duplicate = _order("O-close-initialized", side="SELL", origin="pre_game", ts=0)
    duplicate.tags.append("arb:magic=pre_game_close_pre_game_position")
    ctx = _ctx(
        tier="lower",
        phase="pre",
        tree_kind="compensation",
        orders=[],  # INITIALIZED 尚不在 orders_open，但已存在于完整订单历史。
        order_history=[opening, duplicate],
        positions=[_position(opening_order_id="O-open")],
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    _fire(result, ctx)
    assert "execution_plan" not in ctx.scratch


def test_lower_in_game_position_close_routes_to_in_game_position_branch(trees):
    _, comp_tree = trees
    opening = _order("O-open-in", side="BUY", origin="in_game")
    ctx = _ctx(
        tier="lower",
        phase="in",
        tree_kind="compensation",
        score="1-1",
        period="S1",
        orders=[],
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-in")],
    )

    result = evaluate_tree(comp_tree, ctx)

    assert result.hit is True
    legs = ctx.scratch["legs"]
    assert legs[0]["side"] == "SELL"
    assert legs[0]["price"] == pytest.approx(0.43)  # best bid
    assert legs[0]["position_id"] == "P-1"
    _fire(result, ctx)
    order = ctx.scratch["execution_plan"].orders[0].spec
    # in_game_position 分支为 limit=true + spread=0.07。
    assert order["price"] == pytest.approx(0.52)


def test_lower_in_game_position_stop_loss_requires_two_games_and_opening_set(trees):
    _, comp_tree = trees
    opening = _order(
        "O-open-sl",
        side="BUY",
        origin="in_game",
        score="0-0",
    )
    ctx = _ctx(
        tier="lower",
        phase="in",
        tree_kind="compensation",
        score="1-3",
        period="S1",
        orders=[],
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-sl")],
    )

    result = _run(evaluate_tree_to_plan(comp_tree, ctx))

    assert result.hit is True
    order = ctx.scratch["execution_plan"].orders[0].spec
    assert order["magic"] == "in_game_close_in_game_position_sl"
    assert order["side"] == "SELL"
    assert order["price"] == pytest.approx(0.45)

    ctx = _ctx(
        tier="lower",
        phase="in",
        tree_kind="compensation",
        score="3-6, 0-2",
        period="S2",
        orders=[],
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-sl")],
    )

    result = _run(evaluate_tree_to_plan(comp_tree, ctx))

    assert result.hit is False
    assert "execution_plan" not in ctx.scratch


def test_lower_pre_game_position_closes_in_game_only_when_score_allows(trees):
    _, comp_tree = trees
    opening = _order("O-open-pre", side="BUY", origin="pre_game")
    ctx = _ctx(
        tier="lower",
        phase="in",
        tree_kind="compensation",
        score="1-1",
        orders=[],
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-pre")],
    )

    result = _run(evaluate_tree_to_plan(comp_tree, ctx))
    assert result.hit is True

    order = ctx.scratch["execution_plan"].orders[0].spec
    assert order["side"] == "SELL"
    assert order["magic"] == "in_game_close_pre_game_position_hit"
    assert order["price"] == pytest.approx(0.43)  # limit=false,保留 position_close 的 bid
    assert order["intent"] == "recovery"


@pytest.mark.parametrize(("origin", "expect_default"), [("pre_game", True), ("in_game", False)])
def test_lower_in_game_position_close_while_position_is_losing(
    trees,
    origin,
    expect_default,
):
    _, comp_tree = trees
    opening = _order("O-open-lose", side="BUY", origin=origin)
    ctx = _ctx(
        tier="lower",
        phase="in",
        tree_kind="compensation",
        score="0-1",
        orders=[],
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-lose")],
    )

    result = _run(evaluate_tree_to_plan(comp_tree, ctx))

    assert result.hit is expect_default
    if expect_default:
        order = ctx.scratch["execution_plan"].orders[0].spec
        assert order["magic"] == "in_game_close_pre_game_position_default"
        assert order["price"] == pytest.approx(0.59)
    else:
        assert "execution_plan" not in ctx.scratch


def test_regular_position_has_no_compensation_branch(trees):
    _, comp_tree = trees
    opening = _order("O-open-reg", side="BUY", origin="in_game")
    ctx = _ctx(
        tier="regular",
        phase="in",
        order_history=[opening],
        positions=[_position(opening_order_id="O-open-reg")],
    )

    assert evaluate_tree(comp_tree, ctx).hit is False
