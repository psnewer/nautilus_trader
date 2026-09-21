"""PriceGateAction —— 按执行腿的统一隐含概率筛选下单腿。"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class PriceGateAction(Action):
    """默认删除概率低于阈值的腿；below=True 时删除高于阈值的腿。"""

    def __init__(self, price: float, below: bool = False) -> None:
        try:
            threshold = float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"price_gate: price must be finite, got {price!r}") from exc
        if not math.isfinite(threshold):
            raise ValueError(f"price_gate: price must be finite, got {price!r}")
        if not isinstance(below, bool):
            raise ValueError(f"price_gate: below must be a boolean, got {below!r}")
        self._price = threshold
        self._below = below

    async def execute(self, ctx: EvalContext) -> None:
        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = self._filter_legs(ctx, legs)
            filtered = dict(selected)
            filtered["legs"] = kept
            ctx.scratch["selected_candidate"] = filtered
            ctx.scratch["legs"] = kept
            return

        if "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                ctx.scratch["candidates"] = []
                return
            filtered_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("cancel_pair_orders"):
                    filtered_candidates.append(candidate)
                    continue
                legs = candidate.get("legs")
                if not isinstance(legs, list) or not legs:
                    continue
                kept = self._filter_legs(ctx, legs)
                if kept:
                    filtered = dict(candidate)
                    filtered["legs"] = kept
                    filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if isinstance(legs, list) and legs:
            ctx.scratch["legs"] = self._filter_legs(ctx, legs)

    def _filter_legs(self, ctx: EvalContext, legs: list[dict]) -> list[dict]:
        kept = []
        for leg in legs:
            probability = _leg_probability(leg)
            if probability is not None and (
                probability >= self._price
                if not self._below
                else probability <= self._price
            ):
                kept.append(leg)
                continue
            _LOG.info(
                f"PriceGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"prob={probability} raw_prob={leg.get('prob')} price={leg.get('price')} "
                f"venue={leg.get('venue')} threshold={self._price} below={self._below}",
            )
        return kept


def _leg_probability(leg: dict) -> float | None:
    raw_probability = leg.get("prob")
    if raw_probability is not None:
        return _valid_probability(raw_probability)

    venue = str(leg.get("venue") or "").upper()
    claim = str(leg.get("claim") or leg.get("role") or "yes").lower()
    try:
        price = float(leg.get("price"))
        probability = probability_from_price(venue, price, claim)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return _valid_probability(probability)


def _valid_probability(value) -> float | None:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return probability
