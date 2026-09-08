"""TrendGateAction —— 按当前最优概率相对 pair 趋势基准的方向筛 leg。"""

from __future__ import annotations

import logging

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.checks.quote_legs import best_probabilities_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class TrendGateAction(Action):
    """只保留相对 `PairPriceStore.trend_price` 朝指定方向变化的 outcome 腿。

    `up` 缺失或为 True 时保留 `current_best_ask > trend_price` 的 outcome；False 时保留
    `current_best_ask < trend_price` 的 outcome。`complement=True` 时拦截二元 outcome 同为 up
    或同为 down。`enable_flat` 缺失或为 True 时，flat 可在其余 outcome 都是同一非 flat
    方向时视为该方向的反面；False 时任一 outcome 为 flat 都会拦截整组腿。基准或当前完整报价
    缺失时 fail-closed，全删腿。按 `selected_candidate`、`candidates`、legs-only 的顺序处理现有输出。
    """

    def __init__(
        self,
        up: bool = True,
        complement: bool = False,
        enable_flat: bool = True,
    ) -> None:
        if not isinstance(up, bool):
            raise ValueError(f"trend_gate: up must be a boolean, got {up!r}")
        if not isinstance(complement, bool):
            raise ValueError(
                f"trend_gate: complement must be a boolean, got {complement!r}",
            )
        if not isinstance(enable_flat, bool):
            raise ValueError(
                f"trend_gate: enable_flat must be a boolean, got {enable_flat!r}",
            )
        self._keep_up = up
        self._require_complement = complement
        self._enable_flat = enable_flat

    async def execute(self, ctx: EvalContext) -> None:
        target = "up" if self._keep_up else "down"
        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(
                ctx,
                legs,
                target,
                _directions(ctx),
                self._require_complement,
                self._enable_flat,
            )
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
            directions = _directions(ctx)
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
                kept = _filter_legs(
                    ctx,
                    legs,
                    target,
                    directions,
                    self._require_complement,
                    self._enable_flat,
                )
                if not kept:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if not isinstance(legs, list) or not legs:
            return
        ctx.scratch["legs"] = _filter_legs(
            ctx,
            legs,
            target,
            _directions(ctx),
            self._require_complement,
            self._enable_flat,
        )


def _filter_legs(
    ctx: EvalContext,
    legs: list[dict],
    target: str,
    directions: dict[str, str],
    require_complement: bool,
    enable_flat: bool,
) -> list[dict]:
    has_flat = "flat" in directions.values()
    effective_directions = (
        _directions_with_flat_opposites(directions)
        if enable_flat
        else directions
    )
    direction_values = tuple(effective_directions.values())
    same_non_flat_direction = (
        len(direction_values) == 2
        and direction_values[0] == direction_values[1]
        and direction_values[0] in {"up", "down"}
    )
    flat_matches = enable_flat or not has_flat
    complement_matches = (
        not require_complement
        or not same_non_flat_direction
    )
    kept = []
    for leg in legs:
        outcome = _outcome(leg)
        if (
            flat_matches
            and complement_matches
            and effective_directions.get(outcome) == target
        ):
            kept.append(leg)
            continue
        _LOG.info(
            f"TrendGate: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"outcome={outcome} direction={directions.get(outcome)} "
            f"effective_direction={effective_directions.get(outcome)} target={target} "
            f"complement={require_complement} complement_matches={complement_matches} "
            f"enable_flat={enable_flat} flat_matches={flat_matches}",
        )
    return kept


def _directions_with_flat_opposites(directions: dict[str, str]) -> dict[str, str]:
    """其余 outcome 趋势一致时，将 flat 推断为对手盘方向的反面。"""
    result = dict(directions)
    for outcome, direction in directions.items():
        if direction != "flat":
            continue
        opposing_directions = {
            value
            for other_outcome, value in directions.items()
            if other_outcome != outcome
        }
        if opposing_directions == {"up"}:
            result[outcome] = "down"
        elif opposing_directions == {"down"}:
            result[outcome] = "up"
    return result


def _directions(ctx: EvalContext) -> dict[str, str]:
    if ctx.cache is None or ctx.pair_registry is None:
        return {}
    state = PairPriceStore(ctx.cache).get(ctx.pair_id)
    if state is None or not state.trend_price:
        return {}
    current = best_probabilities_by_outcome(ctx.cache, ctx.pair_registry, ctx.pair_id)
    if current is None or set(current) != set(state.trend_price):
        return {}
    result = {}
    for outcome, price in current.items():
        baseline = state.trend_price[outcome]
        if price > baseline:
            result[outcome] = "up"
        elif price < baseline:
            result[outcome] = "down"
        else:
            result[outcome] = "flat"
    return result


def _outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()
