"""NegRebateCheck:按目标 outcome 的当前返水筛选 one-side candidates。"""

from types import SimpleNamespace

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.strategy.checks.neg_rebate import NegRebateCheck
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


def _candidate(target_role):
    return {"candidate_id": f"one-side:{target_role}", "target_role": target_role, "legs": []}


def _ctx(*, profits, shares, candidates=None):
    return EvalContext(
        pair_id="pair-1",
        portfolio=_Portfolio(profits=profits, shares=shares),
        scratch={
            "candidates": candidates or [_candidate("yes"), _candidate("no")],
            "one_side_rebate": {"candidate_count": 2, "min_rate": 0.04},
        },
    )


def test_default_threshold_keeps_only_non_positive_target_outcomes():
    ctx = _ctx(
        profits={"yes": 10.0, "no": -1.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert NegRebateCheck().passes(ctx) is True
    assert [candidate["target_role"] for candidate in ctx.scratch["candidates"]] == ["no"]
    assert ctx.scratch["neg_rebate"] == {
        "rates": {"yes": 0.1, "no": -0.01},
        "max_rate": 0.0,
        "max_share": 100.0,
        "candidate_count": 1,
    }
    assert ctx.scratch["one_side_rebate"]["candidate_count"] == 1


def test_rate_equal_to_configured_max_rate_passes():
    ctx = _ctx(
        profits={"yes": 5.0, "no": 6.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert NegRebateCheck(max_rate=0.05).passes(ctx) is True
    assert [candidate["target_role"] for candidate in ctx.scratch["candidates"]] == ["yes"]


def test_all_target_outcomes_above_threshold_reject():
    ctx = _ctx(
        profits={"yes": 10.0, "no": 4.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert NegRebateCheck(max_rate=0.03).passes(ctx) is False


def test_eval_rolls_back_candidate_filter_when_all_outcomes_reject():
    ctx = _ctx(
        profits={"yes": 10.0, "no": 4.0},
        shares={"yes": 100.0, "no": 80.0},
    )
    before = list(ctx.scratch["candidates"])

    assert NegRebateCheck(max_rate=0.03).eval(ctx) is False
    assert ctx.scratch["candidates"] == before
    assert "neg_rebate" not in ctx.scratch


def test_no_position_is_zero_rebate_for_each_outcome():
    ctx = _ctx(
        profits={"yes": 0.0, "no": 0.0},
        shares={"yes": 0.0, "no": 0.0},
    )

    assert NegRebateCheck().passes(ctx) is True
    assert len(ctx.scratch["candidates"]) == 2
    assert NegRebateCheck(max_rate=-0.01).passes(ctx) is False


def test_candidate_without_valid_target_outcome_is_dropped():
    ctx = _ctx(
        profits={"yes": -1.0, "no": -1.0},
        shares={"yes": 100.0, "no": 100.0},
        candidates=[_candidate("draw"), _candidate("yes")],
    )

    assert NegRebateCheck().passes(ctx) is True
    assert [candidate["target_role"] for candidate in ctx.scratch["candidates"]] == ["yes"]


def test_incomplete_outcomes_reject():
    ctx = _ctx(
        profits={"yes": -1.0},
        shares={"yes": 100.0, "no": 80.0},
    )

    assert NegRebateCheck().passes(ctx) is False


def test_missing_portfolio_or_candidates_rejects():
    assert NegRebateCheck().passes(EvalContext(pair_id="pair-1")) is False
    ctx = EvalContext(
        pair_id="pair-1",
        portfolio=_Portfolio(profits={"yes": 0.0, "no": 0.0}, shares={"yes": 0.0, "no": 0.0}),
    )
    assert NegRebateCheck().passes(ctx) is False


def test_portfolio_invariant_error_rejects(caplog):
    ctx = EvalContext(
        pair_id="pair-1",
        portfolio=_InvariantPortfolio(),
        scratch={"candidates": [_candidate("yes")]},
    )

    assert NegRebateCheck().passes(ctx) is False
    assert "portfolio invariant" in caplog.text
