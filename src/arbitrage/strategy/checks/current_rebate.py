"""CurrentRebateCheck —— 当前组合仓位的逐 outcome 返水门控。"""

from __future__ import annotations

import logging

from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


class CurrentRebateCheck(Check):
    """要求当前每个 outcome 的返水率均不低于阈值。"""

    def __init__(self, min_rate: float = 0.0) -> None:
        self._min_rate = float(min_rate)

    def passes(self, ctx: EvalContext) -> bool:
        portfolio = ctx.portfolio
        if portfolio is None:
            return False

        try:
            exposures = portfolio.outcome_exposures(ctx.pair_id)
            shares = portfolio.outcome_shares(ctx.pair_id)
        except PositionOutcomeInvariantError as exc:
            _LOG.error(f"CurrentRebate: pair={ctx.pair_id} portfolio invariant: {exc}")
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

        if any(rate < self._min_rate for rate in rates.values()):
            return False

        ctx.scratch["current_rebate"] = {
            "rates": rates,
            "min_rate": self._min_rate,
            "max_share": max_share,
        }
        return True
