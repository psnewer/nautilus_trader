"""CommissionGateAction —— 按 PM 二元盘口的 commission 过滤执行计划。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class CommissionGateAction(Action):
    """PM 两个 outcome 的 best-ask 概率和达到阈值时拦截下单计划。"""

    def __init__(self, commission: float) -> None:
        try:
            threshold = float(commission)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"commission_gate: commission must be a finite number, got {commission!r}",
            ) from exc
        if not math.isfinite(threshold):
            raise ValueError(
                f"commission_gate: commission must be a finite number, got {commission!r}",
            )
        self._threshold = threshold

    async def execute(self, ctx: EvalContext) -> None:
        if not _has_submit_plan(ctx):
            return

        commission = _pm_commission(ctx)
        if commission is not None and commission < self._threshold:
            return

        _block_submit_plans(ctx)
        actual = "unavailable" if commission is None else f"{commission:.8f}"
        _LOG.info(
            f"CommissionGate: pair={ctx.pair_id} blocked "
            f"pm_commission={actual} threshold={self._threshold:.8f}",
        )


def _pm_commission(ctx: EvalContext) -> float | None:
    """返回 PM yes/no 当前 best-ask 概率和,缺完整二元盘口时返回 None。"""
    legs_by_outcome = quote_legs_by_outcome(ctx)
    probabilities = []
    for outcome in VALID_OUTCOMES:
        pm_leg = next(
            (
                leg
                for leg in legs_by_outcome.get(outcome, ())
                if str(leg.get("venue") or "").upper() == POLYMARKET
            ),
            None,
        )
        if pm_leg is None:
            return None
        try:
            probability = float(pm_leg.get("prob"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(probability) or probability <= 0:
            return None
        probabilities.append(probability)
    return sum(probabilities)


def _has_submit_plan(ctx: EvalContext) -> bool:
    selected = ctx.scratch.get("selected_candidate")
    if isinstance(selected, dict):
        return not selected.get("cancel_pair_orders") and bool(selected.get("legs"))

    if "candidates" in ctx.scratch:
        return any(
            isinstance(candidate, dict)
            and not candidate.get("cancel_pair_orders")
            and bool(candidate.get("legs"))
            for candidate in (ctx.scratch.get("candidates") or ())
        )

    return bool(ctx.scratch.get("legs")) and not ctx.scratch.get("cancel_pair_orders")


def _block_submit_plans(ctx: EvalContext) -> None:
    """清空下单腿,但保留撤单 candidate,避免行情门控阻断风险收尾。"""
    selected = ctx.scratch.get("selected_candidate")
    if isinstance(selected, dict):
        if selected.get("cancel_pair_orders"):
            return
        filtered = dict(selected)
        filtered["legs"] = []
        ctx.scratch["selected_candidate"] = filtered
        ctx.scratch["legs"] = []
        return

    if "candidates" in ctx.scratch:
        candidates = ctx.scratch.get("candidates") or ()
        ctx.scratch["candidates"] = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("cancel_pair_orders")
        ]
        return

    ctx.scratch["legs"] = []
