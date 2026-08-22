"""head_rebate 连续实时状态场景。

用例从完整 JSON strategy spec 构建双树，共享同一 StrategyRuntimeStore，
验证跨评估轮次的 standard 及最终 ExecutionPlan。不启动 TradingNode，不进入 Risk/Execution。
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import PositionId
from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.candi_select import CandiSelectAction
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.actions.share_limit import ShareLimitModification
from src.arbitrage.strategy.actions.trend_gate import TrendGateAction
from src.arbitrage.strategy.actions.venue_replace import VenueReplaceAction
from src.arbitrage.strategy.check_action_registry import register_action
from src.arbitrage.strategy.check_action_registry import register_check
from src.arbitrage.strategy.check_action_registry import register_state_query
from src.arbitrage.strategy.checks.mean_rebate import MeanRebateCheck
from src.arbitrage.strategy.checks.mean_rebate_recovery import MeanRebateRecoveryCheck
from src.arbitrage.strategy.checks.reverse import ReverseCheck
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.json_loader import strategy_from_json
from src.arbitrage.strategy.queries.position_mode import HeadQuery
from src.arbitrage.strategy.queries.position_mode import ReverseQuery
from src.arbitrage.strategy.runtime_store import StrategyRuntimeStore
from tests.arbitrage.strategy._live_state import live_context


_PAIR_ID = "head-rebate-pair"
_STRATEGY_ID = "head_rebate"
_SHARE = 10.0
_HEAD_REBATE_SPEC = {
    "arbitrage_tree": {
        "self_hits": {"type": "head"},
        "checktion": {"type": "mean_rebate", "params": {"min_rate": -0.03}},
        "actions": [
            {"type": "venue_replace", "params": {"pm_price": True}},
            {"type": "share_limit"},
            {"type": "candi_select"},
            {"type": "trend_gate"},
            {"type": "place_bets", "params": {"limit": True}},
        ],
    },
    "compensation_tree": {
        "self_hits": {"type": "reverse"},
        "checktion": {
            "AND": [
                {"type": "reverse", "params": {"rt": 1.0, "retrieve": 0.1}},
                {"type": "mean_rebate_recovery", "params": {"force": True}},
            ],
        },
        "actions": [
            {"type": "candi_select"},
            {
                "type": "place_bets",
                "params": {"intent": "recovery", "market": True},
            },
        ],
    },
}


class _Qty:
    def __init__(self, value: float):
        self._value = float(value)

    def as_double(self) -> float:
        return self._value


class _Money:
    def __init__(self, value: float):
        self._value = float(value)

    def as_double(self) -> float:
        return self._value


class _Position:
    def __init__(self, instrument_id: str, quantity: float, avg_px_open: float):
        self.id = PositionId(f"{instrument_id}-SCENARIO")
        self.instrument_id = instrument_id
        self.quantity = _Qty(quantity)
        self.avg_px_open = avg_px_open
        self.side = PositionSide.LONG


class _Portfolio:
    def __init__(self, *, shares: dict[str, float], unrealized: dict[str, float]):
        self._shares = dict(shares)
        self._unrealized = dict(unrealized)

    def outcome_shares(self, pair_id):
        return dict(self._shares)

    def outcome_shares_for_venue(self, pair_id, venue, account_id):
        if venue == "polymarket":
            return dict(self._shares)
        return {"yes": 0.0, "no": 0.0}

    def outcome_exposures(self, pair_id, include_realized_pnl=True):
        return {
            "yes": SimpleNamespace(net_profit=0.0),
            "no": SimpleNamespace(net_profit=0.0),
        }

    def unrealized_pnl(self, instrument_id, price):
        return _Money(self._unrealized.get(str(instrument_id), 0.0))

    def realized_pnl_for_pair(self, pair_id):
        return 0.0


@dataclass
class _RoundResult:
    arb_hit: bool
    comp_hit: bool
    arb_plan: object | None
    comp_plan: object | None


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _strategy():
    register_state_query("head", HeadQuery)
    register_state_query("reverse", ReverseQuery)
    register_check("mean_rebate", MeanRebateCheck)
    register_check("reverse", ReverseCheck)
    register_check("mean_rebate_recovery", MeanRebateRecoveryCheck)
    register_action("venue_replace", VenueReplaceAction)
    register_action("share_limit", ShareLimitModification)
    register_action("candi_select", CandiSelectAction)
    register_action("trend_gate", TrendGateAction)
    register_action("place_bets", PlaceBetsAction)
    return strategy_from_json(_STRATEGY_ID, _HEAD_REBATE_SPEC, "competition:SCENARIO")


def _position(role: str) -> _Position:
    return _Position(f"{role[0].upper()}.POLYMARKET", quantity=_SHARE, avg_px_open=0.4)


def _context(*, store, shares, unrealized=None, positions=None):
    books = {
        "Y.POLYMARKET": {"bid": 0.40, "ask": 0.42},
        "N.POLYMARKET": {"bid": 0.53, "ask": 0.55},
        "Y.ORBITEXCH": {"bid": 0.38, "ask": 0.40},
        "N.ORBITEXCH": {"bid": 0.51, "ask": 0.53},
    }
    infos = {
        "Y.POLYMARKET": {"claim": "yes", "selection_role": "yes"},
        "N.POLYMARKET": {"claim": "no", "selection_role": "no"},
        "Y.ORBITEXCH": {"claim": "yes", "selection_role": "yes"},
        "N.ORBITEXCH": {"claim": "no", "selection_role": "no"},
    }
    ctx = live_context(
        pair_id=_PAIR_ID,
        books=books,
        infos=infos,
        positions=list(positions or []),
        instrument_ids=list(books),
        portfolio=_Portfolio(shares=shares, unrealized=unrealized or {}),
        strategy_defaults={"share": _SHARE, "max_leg_share": 20.0},
        strategy_id=_STRATEGY_ID,
        runtime_store=store,
    )
    price_store = PairPriceStore(ctx.cache)
    price_store.initialize(_PAIR_ID, ["yes", "no"])
    price_store.update_trend(_PAIR_ID, {"yes": 0.39, "no": 0.62})
    return ctx


def _evaluate_round(*, store, shares, unrealized=None, positions=None) -> _RoundResult:
    strategy = _strategy()
    kwargs = {
        "store": store,
        "shares": shares,
        "unrealized": unrealized,
        "positions": positions,
    }
    arb_ctx = _context(**kwargs)
    comp_ctx = _context(**kwargs)
    arb_result = evaluate_tree(strategy.arbitrage_tree, arb_ctx)
    comp_result = evaluate_tree(strategy.compensation_tree, comp_ctx)
    for action in arb_result.pending_actions:
        _run(action.execute(arb_ctx))
    for action in comp_result.pending_actions:
        _run(action.execute(comp_ctx))
    return _RoundResult(
        arb_hit=arb_result.hit,
        comp_hit=comp_result.hit,
        arb_plan=arb_ctx.scratch.get("execution_plan"),
        comp_plan=comp_ctx.scratch.get("execution_plan"),
    )


def _standard(store: StrategyRuntimeStore) -> float:
    return store.get(_STRATEGY_ID, _PAIR_ID, "standard")


def test_no_position_enters_head_and_prepares_rebate_limit_order():
    store = StrategyRuntimeStore()

    result = _evaluate_round(store=store, shares={"yes": 0.0, "no": 0.0})

    assert result.arb_hit is True
    assert result.comp_hit is False
    assert _standard(store) == pytest.approx(0.0)
    assert result.comp_plan is None
    assert len(result.arb_plan.orders) == 1
    spec = result.arb_plan.orders[0].spec
    assert (spec["instrument_id"], spec["side"], spec["price"]) == (
        "Y.POLYMARKET",
        "BUY",
        0.40,
    )
    assert "market" not in spec


def test_one_position_initializes_reverse_standard_without_stopping():
    store = StrategyRuntimeStore()

    result = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": 0.0},
        unrealized={"Y.POLYMARKET": 1.0},
        positions=[_position("yes")],
    )

    assert result.arb_hit is False
    assert result.comp_hit is False
    assert _standard(store) == pytest.approx(0.10)
    assert result.arb_plan is None
    assert result.comp_plan is None


def test_reverse_rebate_expansion_raises_standard_without_stopping():
    store = StrategyRuntimeStore()
    store.update(_STRATEGY_ID, _PAIR_ID, {"standard": 0.10})

    result = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": 0.0},
        unrealized={"Y.POLYMARKET": 2.5},
        positions=[_position("yes")],
    )

    assert result.comp_hit is False
    assert result.comp_plan is None
    assert _standard(store) == pytest.approx(0.25)


def test_reverse_rebate_drop_to_inclusive_boundary_prepares_market_hedge():
    store = StrategyRuntimeStore()
    store.update(_STRATEGY_ID, _PAIR_ID, {"standard": 0.25})

    result = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": 0.0},
        unrealized={"Y.POLYMARKET": 1.5},
        positions=[_position("yes")],
    )

    assert result.arb_hit is False
    assert result.comp_hit is True
    assert _standard(store) == pytest.approx(0.25)
    assert result.arb_plan is None
    assert len(result.comp_plan.orders) == 1
    spec = result.comp_plan.orders[0].spec
    assert spec["intent"] == "recovery"
    assert spec["market"] is True
    assert result.comp_plan.orders[0].role == "no"


def test_two_positions_return_to_head_and_reset_standard():
    store = StrategyRuntimeStore()
    store.update(_STRATEGY_ID, _PAIR_ID, {"standard": 0.25})

    result = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": _SHARE},
        unrealized={"Y.POLYMARKET": 0.5, "N.POLYMARKET": 0.3},
        positions=[_position("yes"), _position("no")],
    )

    assert result.arb_hit is True
    assert result.comp_hit is False
    assert _standard(store) == pytest.approx(0.08)
    assert result.arb_plan is not None
    assert result.comp_plan is None


def test_post_hedge_reverse_uses_reset_standard_instead_of_stale_peak():
    store = StrategyRuntimeStore()
    store.update(_STRATEGY_ID, _PAIR_ID, {"standard": 0.25})
    hedged = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": _SHARE},
        unrealized={"Y.POLYMARKET": 0.5, "N.POLYMARKET": 0.3},
        positions=[_position("yes"), _position("no")],
    )
    assert hedged.arb_hit is True
    assert _standard(store) == pytest.approx(0.08)

    reopened = _evaluate_round(
        store=store,
        shares={"yes": _SHARE, "no": 0.0},
        unrealized={"Y.POLYMARKET": 0.5},
        positions=[_position("yes")],
    )

    assert reopened.arb_hit is False
    assert reopened.comp_hit is False
    assert reopened.comp_plan is None
    assert _standard(store) == pytest.approx(0.08)
