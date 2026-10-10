"""CommissionGateAction —— 按 PM 二元盘口的 commission 阈值或范围过滤执行计划。"""

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
    """按上限阈值或闭区间校验 PM 两个 outcome 的 best-ask 概率和。"""

    def __init__(
        self,
        commission: float | None = None,
        min_commission: float | None = None,
        max_commission: float | None = None,
    ) -> None:
        if commission is not None and (min_commission is not None or max_commission is not None):
            raise ValueError(
                "commission_gate: commission cannot be combined with min/max_commission",
            )
        if commission is None:
            self._threshold = None
            self._minimum = _finite_value("min_commission", min_commission)
            self._maximum = _finite_value("max_commission", max_commission)
            if self._minimum > self._maximum:
                raise ValueError("commission_gate: min_commission must be <= max_commission")
            return
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
        self._minimum = None
        self._maximum = None

    async def execute(self, ctx: EvalContext) -> None:
        if not _has_submit_plan(ctx):
            return

        commission = _pm_commission(ctx)
        if commission is not None:
            if self._threshold is not None and commission < self._threshold:
                return
            if (
                self._minimum is not None
                and self._maximum is not None
                and self._minimum <= commission <= self._maximum
            ):
                return

        _block_submit_plans(ctx)
        actual = "unavailable" if commission is None else f"{commission:.8f}"
        expected = (
            f"threshold={self._threshold:.8f}"
            if self._threshold is not None
            else f"range=[{self._minimum:.8f},{self._maximum:.8f}]"
        )
        _LOG.info(
            f"CommissionGate: pair={ctx.pair_id} blocked "
            f"pm_commission={actual} {expected}",
        )


def _finite_value(name: str, value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"commission_gate: {name} must be a finite number, got {value!r}",
        ) from exc
    if not math.isfinite(result):
        raise ValueError(
            f"commission_gate: {name} must be a finite number, got {value!r}",
        )
    return result


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
