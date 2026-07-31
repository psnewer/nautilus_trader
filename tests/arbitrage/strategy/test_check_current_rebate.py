"""CurrentRebateCheck:当前组合仓位的逐 outcome 返水门控。"""

from types import SimpleNamespace

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.strategy.checks.current_rebate import CurrentRebateCheck
from src.arbitrage.strategy.condition import EvalContext


class _Portfolio:
    def __init__(self, *, profits, shares):
        self._profits = profits
        self._shares = shares

    def outcome_exposures(self, pair_id):
        return {
            outcome: SimpleNamespace(net_profit=profit)
            for outcome, profit in self._profits.items()
        }

    def outcome_shares(self, pair_id):
        return dict(self._shares)


class _InvariantPortfolio:
    def outcome_exposures(self, pair_id):
        raise PositionOutcomeInvariantError("bad claim")

    def outcome_shares(self, pair_id):
        raise AssertionError("不应继续读取 shares")


def _ctx(*, profits, shares):
    return EvalContext(
        pair_id="pair-1",
        portfolio=_Portfolio(profits=profits, shares=shares),
    )


def test_all_outcomes_at_or_above_default_threshold_pass():
    ctx = _ctx(
        profits={"yes": 10.0, "no": 0.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert CurrentRebateCheck().passes(ctx) is True
    assert ctx.scratch["current_rebate"] == {
        "rates": {"yes": 0.1, "no": 0.0},
        "min_rate": 0.0,
        "max_share": 100.0,
    }


def test_any_outcome_below_threshold_rejects():
    ctx = _ctx(
        profits={"yes": 10.0, "no": 4.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert CurrentRebateCheck(min_rate=0.05).passes(ctx) is False
    assert "current_rebate" not in ctx.scratch


def test_no_position_is_zero_rebate_for_each_outcome():
    ctx = _ctx(
        profits={"yes": 0.0, "no": 0.0},
        shares={"yes": 0.0, "no": 0.0},
    )

    assert CurrentRebateCheck().passes(ctx) is True
    assert CurrentRebateCheck(min_rate=0.01).passes(ctx) is False


def test_incomplete_outcomes_reject():
    ctx = _ctx(
        profits={"yes": 10.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert CurrentRebateCheck().passes(ctx) is False


def test_missing_portfolio_rejects():
    assert CurrentRebateCheck().passes(EvalContext(pair_id="pair-1")) is False


def test_portfolio_invariant_error_rejects(caplog):
    ctx = EvalContext(pair_id="pair-1", portfolio=_InvariantPortfolio())

    assert CurrentRebateCheck().passes(ctx) is False
    assert "portfolio invariant" in caplog.text
