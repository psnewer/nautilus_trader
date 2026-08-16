"""head/reverse 仓位判态与 standard 更新。"""

from types import SimpleNamespace

import pytest

from nautilus_trader.model.enums import PositionSide
from src.arbitrage.strategy.checks.reverse import ReverseCheck
from src.arbitrage.strategy.condition import Condition
from src.arbitrage.strategy.condition import evaluate_tree
from src.arbitrage.strategy.queries.position_mode import HeadQuery
from src.arbitrage.strategy.queries.position_mode import ReverseQuery
from src.arbitrage.strategy.runtime_store import StrategyRuntimeStore
from tests.arbitrage.strategy._live_state import live_context


class _Qty:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


class _Money:
    def __init__(self, value):
        self._value = value

    def as_double(self):
        return self._value


class _Portfolio:
    def __init__(self, *, shares, unrealized=None, realized=0.0):
        self.shares = dict(shares)
        self.unrealized = dict(unrealized or {})
        self.realized = realized
        self.prices = {}

    def outcome_shares(self, pair_id):
        return dict(self.shares)

    def unrealized_pnl(self, instrument_id, price):
        self.prices[str(instrument_id)] = float(price)
        value = self.unrealized.get(str(instrument_id))
        return None if value is None else _Money(value)

    def realized_pnl_for_pair(self, pair_id):
        return self.realized


def _position(instrument_id, *, side=PositionSide.LONG, qty=5.0):
    return SimpleNamespace(
        instrument_id=instrument_id,
        side=side,
        quantity=_Qty(qty),
    )


def _ctx(*, portfolio, books=None, infos=None, positions=None, share=10.0):
    return live_context(
        books=books,
        infos=infos or {
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        positions=positions,
        portfolio=portfolio,
        strategy_defaults={"share": share},
        strategy_id="head_rebate",
        runtime_store=StrategyRuntimeStore(),
    )


def test_head_without_position_uses_realized_pnl_as_standard():
    portfolio = _Portfolio(shares={"yes": 0.0, "no": 0.0}, realized=2.0)
    ctx = _ctx(portfolio=portfolio)

    assert HeadQuery().matches(ctx) is True
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == pytest.approx(0.2)
    assert ReverseQuery().matches(ctx) is False


def test_head_with_both_outcomes_overwrites_standard_with_current_rate():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 5.0},
        unrealized={"Y.POLYMARKET": 1.0, "N.POLYMARKET": -0.5},
        realized=0.5,
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={
            "Y.POLYMARKET": {"ask": 0.6},
            "N.POLYMARKET": {"ask": 0.4},
        },
        positions=[_position("Y.POLYMARKET"), _position("N.POLYMARKET")],
    )
    ctx.runtime_store.update("head_rebate", "p", {"standard": 0.8})

    assert HeadQuery().matches(ctx) is True
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == pytest.approx(0.1)


def test_reverse_initializes_missing_standard_even_when_rate_is_negative():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": -2.0},
        realized=0.5,
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.POLYMARKET": {"ask": 0.3}},
        positions=[_position("Y.POLYMARKET")],
    )

    assert ReverseQuery().matches(ctx) is True
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == pytest.approx(-0.15)
    assert HeadQuery().matches(ctx) is False


def test_reverse_query_updates_standard_before_reverse_check_reads_it():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": 3.0},
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.POLYMARKET": {"ask": 0.5}},
        positions=[_position("Y.POLYMARKET")],
    )
    ctx.runtime_store.update("head_rebate", "p", {"standard": 0.2})
    condition = Condition(
        self_hits=ReverseQuery(),
        checktion=ReverseCheck(rt=1.0, retrieve=0.0),
    )

    assert evaluate_tree(condition, ctx).hit is True
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("rate_pnl", "initial", "expected"),
    [
        (3.0, 0.2, 0.3),
        (1.0, 0.2, 0.2),
        (-1.0, 0.2, 0.2),
        (0.0, -0.2, -0.2),
    ],
)
def test_reverse_only_raises_standard_for_positive_higher_rate(rate_pnl, initial, expected):
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": rate_pnl},
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.POLYMARKET": {"ask": 0.5}},
        positions=[_position("Y.POLYMARKET")],
    )
    ctx.runtime_store.update("head_rebate", "p", {"standard": initial})

    assert ReverseQuery().matches(ctx) is True
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == pytest.approx(expected)


def test_reverse_with_invalid_existing_standard_fails_closed():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": 1.0},
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.POLYMARKET": {"ask": 0.5}},
        positions=[_position("Y.POLYMARKET")],
    )
    ctx.runtime_store.update("head_rebate", "p", {"standard": "invalid"})

    assert ReverseQuery().matches(ctx) is False
    assert ctx.runtime_store.get("head_rebate", "p", "standard") == "invalid"


def test_reverse_long_uses_best_ask_for_instant_rebate():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": 1.0},
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.POLYMARKET": {"bid": 0.4, "ask": 0.6}},
        positions=[_position("Y.POLYMARKET")],
    )

    assert ReverseQuery().matches(ctx) is True
    assert portfolio.prices["Y.POLYMARKET"] == pytest.approx(0.6)


def test_reverse_short_uses_best_bid_and_restores_decimal_odds():
    portfolio = _Portfolio(
        shares={"yes": 0.0, "no": 8.0},
        unrealized={"Y.ORBITEXCH": 1.0},
    )
    ctx = _ctx(
        portfolio=portfolio,
        books={"Y.ORBITEXCH": {"bid": 0.25, "ask": 0.4}},
        infos={
            "Y.ORBITEXCH": {"claim": "yes", "quote_claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        positions=[_position("Y.ORBITEXCH", side=PositionSide.SHORT)],
    )

    assert ReverseQuery().matches(ctx) is True
    assert portfolio.prices["Y.ORBITEXCH"] == pytest.approx(4.0)


def test_matching_position_shape_with_missing_valuation_quote_fails_closed():
    portfolio = _Portfolio(
        shares={"yes": 5.0, "no": 0.0},
        unrealized={"Y.POLYMARKET": 1.0},
    )
    ctx = _ctx(portfolio=portfolio, positions=[_position("Y.POLYMARKET")])

    assert ReverseQuery().matches(ctx) is False
    assert ctx.runtime_store.variables("head_rebate", "p") == {}


def test_missing_runtime_identity_fails_closed_without_updating():
    portfolio = _Portfolio(shares={"yes": 0.0, "no": 0.0})
    ctx = _ctx(portfolio=portfolio)
    ctx.strategy_id = None

    assert HeadQuery().matches(ctx) is False
    assert ctx.runtime_store.snapshot() == {}
