"""NegRebateCheck —— 按当前 outcome 返水筛选定向返水候选。"""

from __future__ import annotations

import logging

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


class NegRebateCheck(Check):
    """只保留目标 outcome 当前返水率不高于阈值的 candidate。"""

    def __init__(self, max_rate: float = 0.0) -> None:
        self._max_rate = float(max_rate)

    def passes(self, ctx: EvalContext) -> bool:
        portfolio = ctx.portfolio
        candidates = ctx.scratch.get("candidates")
        if portfolio is None or not isinstance(candidates, list) or not candidates:
            return False

        try:
            exposures = portfolio.outcome_exposures(ctx.pair_id)
            shares = portfolio.outcome_shares(ctx.pair_id)
        except PositionOutcomeInvariantError as exc:
            _LOG.error(f"NegRebate: pair={ctx.pair_id} portfolio invariant: {exc}")
            return False

        outcomes = set(VALID_OUTCOMES)
        if set(exposures) != outcomes or set(shares) != outcomes:
            return False

        max_share = max((float(shares[outcome]) for outcome in outcomes), default=0.0)
        if max_share <= _EPS:
            rates = {outcome: 0.0 for outcome in outcomes}
        else:
            rates = {
                outcome: float(exposures[outcome].net_profit) / max_share
                for outcome in outcomes
            }

        survivors = [
            candidate
            for candidate in candidates
            if candidate.get("target_role") in outcomes
            and rates[candidate["target_role"]] <= self._max_rate
        ]
        if not survivors:
            return False

        ctx.scratch["candidates"] = survivors
        one_side = ctx.scratch.get("one_side_rebate")
        if isinstance(one_side, dict):
            one_side["candidate_count"] = len(survivors)
        ctx.scratch["neg_rebate"] = {
            "rates": rates,
            "max_rate": self._max_rate,
            "max_share": max_share,
            "candidate_count": len(survivors),
        }
        return True
