"""one_side_rebate 策略内组合场景。

这些用例只验证策略树内部的组合语义:套利树 / 补偿树 / share_limit /
candi_select。它们不启动 TradingNode,不进入 Risk/Execution/barrier。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.model.enums import PositionSide
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.actions.candi_select import CandiSelectAction
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.bool_expr import AndExpr
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.checks.one_side_rebate import OneSideRebateCheck
from src.arbitrage.strategy.checks.neg_rebate import NegRebateCheck
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import AndCheckExpr
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.condition import evaluate_tree
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _fake_book(ask_price):
    book = MagicMock()
    book.best_ask_price = MagicMock(return_value=ask_price)
    book.best_bid_price = MagicMock(return_value=None)
    return book


class _Qty:
    def __init__(self, value: float):
        self._value = float(value)

    def as_double(self) -> float:
        return self._value


class _Position:
    def __init__(
        self,
        instrument_id: str,
        quantity: float,
        avg_px_open: float,
        side=PositionSide.LONG,
    ):
        self.instrument_id = instrument_id
        self.quantity = _Qty(quantity)
        self.avg_px_open = avg_px_open
        self.side = side


class _Portfolio:
    def __init__(self, *, pm=None, se=None, oe=None, profits=None):
        self._pm = pm or {}
        self._se = se or {}
        self._oe = oe or {}
        self._profits = profits or {"yes": 0.0, "no": 0.0}

    def outcome_exposures(self, pair_id, include_realized_pnl=True):
        return {
            outcome: SimpleNamespace(net_profit=profit)
            for outcome, profit in self._profits.items()
        }

    def outcome_shares(self, pair_id):
        outcomes = {"yes", "no"}
        return {
            outcome: sum(
                shares.get(outcome, 0.0)
                for shares in (self._pm, self._se, self._oe)
            )
            for outcome in outcomes
        }

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        if venue == "polymarket":
            return self._pm
        if venue == "sharpexch":
            return self._se
        if venue == "orbitexch":
            return self._oe
        return {}


class _MarkerAction(Action):
    def __init__(self, label: str):
        self.label = label

    async def execute(self, ctx):
        ctx.scratch["fired"] = self.label


def _ctx(*, positions=None, **kwargs) -> EvalContext:
    books = {
        "H.POLYMARKET": _fake_book(0.45),
        "A.SHARPEXCH": _fake_book(probability_from_price(SHARPEXCH, 2.0)),
    }
    infos = {
        "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "A.SHARPEXCH": {"selection_role": "away", "claim": "no"},
    }
    return live_context(
        books=books,
        infos=infos,
        positions=list(positions or []),
        instrument_ids=list(books.keys()),
        **kwargs,
    )


def _select_action(arb_res, comp_res):
    if comp_res.hit and comp_res.pending_actions:
        return comp_res.pending_actions[0]
    if arb_res.hit and arb_res.pending_actions:
        return arb_res.pending_actions[0]
    return None


def test_existing_position_with_arb_and_recovery_fires_compensation_first():
    """已有仓位且补偿可修复时,若同时出现 one_side 套利,本轮优先触发补偿树。"""
    positions = [_Position("H.POLYMARKET", quantity=100.0, avg_px_open=0.45)]
    arb_ctx = _ctx(positions=positions, strategy_defaults={"share": 100.0})
    comp_ctx = _ctx(positions=positions, strategy_defaults={"share": 100.0})
    arb_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(OneSideRebateCheck(min_rate=0.09, share=100.0)),
        actions=[_MarkerAction("arbitrage")],
    )
    comp_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(MeanRebateRecoveryCheck(min_repaired_rebate=0.04)),
        actions=[_MarkerAction("recovery")],
    )

    arb_res = evaluate_tree(arb_tree, arb_ctx)
    comp_res = evaluate_tree(comp_tree, comp_ctx)

    assert arb_res.hit is True
    assert comp_res.hit is True
    assert _select_action(arb_res, comp_res).label == "recovery"


def test_live_case_no_filled_at_033_and_yes_ask_05714_prefers_recovery():
    """复现 nohup.out 2342-2350:已有 NO 5.99@0.33 时,YES 0.5714 应走补偿。"""
    books = {
        "Y.POLYMARKET": _fake_book(0.57142857),
        "N.POLYMARKET": _fake_book(0.36),
    }
    infos = {
        "Y.POLYMARKET": {"selection_role": "yes", "claim": "yes"},
        "N.POLYMARKET": {"selection_role": "no", "claim": "no"},
    }
    positions = [_Position("N.POLYMARKET", quantity=5.99, avg_px_open=0.33)]
    portfolio = _Portfolio(profits={
        "yes": -(5.99 * 0.33),
        "no": 5.99 * (1.0 - 0.33),
    })
    arb_ctx = live_context(
        books=books,
        infos=infos,
        positions=positions,
        instrument_ids=list(books),
        portfolio=portfolio,
        strategy_defaults={"share": 5.0},
    )
    comp_ctx = live_context(
        books=books,
        infos=infos,
        positions=positions,
        instrument_ids=list(books),
        portfolio=portfolio,
        strategy_defaults={"share": 5.0},
    )
    arb_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(OneSideRebateCheck(min_rate=0.04)),
        actions=[_MarkerAction("arbitrage")],
    )
    comp_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(
            MeanRebateRecoveryCheck(min_repaired_rebate=0.0, venue_select=True),
        ),
        actions=[_MarkerAction("recovery")],
    )

    arb_res = evaluate_tree(arb_tree, arb_ctx)
    comp_res = evaluate_tree(comp_tree, comp_ctx)

    assert arb_res.hit is True
    assert comp_res.hit is True
    assert comp_ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.57142857,
        "prob": 0.57142857,
        "role": "yes",
        "qty": 5.99,
        "claim": "yes",
    }]
    assert round(comp_ctx.scratch["mean_rebate_recovery"]["min_repaired_rebate"], 8) == 0.11808857
    assert _select_action(arb_res, comp_res).label == "recovery"

    # 按实盘配置走完 Action 链，最终统一分发必须选补偿计划的 5.99，
    # 不能选 one_side_rebate 套利计划的配置 share=5.0。
    _run(CandiSelectAction().execute(arb_ctx))
    _run(PlaceBetsAction(enable_timeout=False).execute(arb_ctx))
    _run(CandiSelectAction().execute(comp_ctx))
    _run(PlaceBetsAction(enable_timeout=False, market=True).execute(comp_ctx))

    arb_plan = arb_ctx.scratch["execution_plan"]
    comp_plan = comp_ctx.scratch["execution_plan"]
    selected_plan = comp_plan or arb_plan
    assert selected_plan is comp_plan
    assert len(selected_plan.orders) == 1
    assert selected_plan.orders[0].spec["instrument_id"] == "Y.POLYMARKET"
    assert selected_plan.orders[0].spec["qty"] == 5.99
    assert selected_plan.orders[0].spec["market"] is True


def test_existing_position_with_recovery_only_fires_compensation():
    """已有仓位且套利阈值未达标时,补偿树命中则触发补偿。"""
    positions = [_Position("H.POLYMARKET", quantity=100.0, avg_px_open=0.45)]
    arb_ctx = _ctx(positions=positions, strategy_defaults={"share": 100.0})
    comp_ctx = _ctx(positions=positions, strategy_defaults={"share": 100.0})
    arb_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(OneSideRebateCheck(min_rate=0.20, share=100.0)),
        actions=[_MarkerAction("arbitrage")],
    )
    comp_tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(MeanRebateRecoveryCheck(min_repaired_rebate=0.04)),
        actions=[_MarkerAction("recovery")],
    )

    arb_res = evaluate_tree(arb_tree, arb_ctx)
    comp_res = evaluate_tree(comp_tree, comp_ctx)

    assert arb_res.hit is False
    assert comp_res.hit is True
    assert _select_action(arb_res, comp_res).label == "recovery"


def test_existing_position_share_limit_scales_candidates_before_selection():
    """已有仓位时,one_side candidates 先按 share_limit 缩放,再由 candi_select 选最大。"""
    ctx = _ctx(
        portfolio=_Portfolio(pm={"yes": 30.0, "no": 0.0}),
        strategy_defaults={"share": 30.0, "max_leg_share": 40.0},
    )

    assert OneSideRebateCheck(min_rate=0.09).passes(ctx) is True
    _run(ShareLimitModification().execute(ctx))
    _run(CandiSelectAction().execute(ctx))

    selected = ctx.scratch["selected_candidate"]
    by_role = {leg["role"]: leg for leg in ctx.scratch["legs"]}
    assert selected["target_role"] == "no"
    assert round(by_role["yes"]["share_if_wins"], 6) == 10.0
    assert round(by_role["yes"]["qty"], 6) == 10.0
    assert round(by_role["no"]["share_if_wins"], 6) == 11.0
    assert round(by_role["no"]["qty"], 6) == 5.5


def test_neg_rebate_gate_keeps_candidate_for_low_rebate_outcome():
    """one_side 生成两个方向后，只保留当前返水不高于阈值的目标方向。"""
    ctx = _ctx(
        portfolio=_Portfolio(
            pm={"yes": 100.0, "no": 80.0},
            profits={"yes": 10.0, "no": -1.0},
        ),
        strategy_defaults={"share": 100.0},
    )
    tree = Condition(
        self_hits=AndExpr(),
        checktion=AndCheckExpr(
            OneSideRebateCheck(min_rate=0.09),
            NegRebateCheck(),
        ),
        actions=[_MarkerAction("arbitrage")],
    )

    result = evaluate_tree(tree, ctx)

    assert result.hit is True
    assert {candidate["target_role"] for candidate in ctx.scratch["candidates"]} == {"no"}
    assert ctx.scratch["neg_rebate"]["rates"] == {"yes": 0.1, "no": -0.01}
