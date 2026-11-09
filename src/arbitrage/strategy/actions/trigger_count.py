"""按当前盘各 outcome 的去重触发次数筛选执行腿。"""

from __future__ import annotations

import logging

from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


_LOG = logging.getLogger(__name__)


class TriggerCountAction(Action):
    """保留当前盘去重触发次数最多的 outcome 腿。"""

    def __init__(
        self,
        history_key: str,
        when: str | None = None,
        equal_enable: bool = False,
    ) -> None:
        if not isinstance(history_key, str) or not history_key.strip():
            raise ValueError("trigger_count: history_key must be a non-empty string")
        if when not in (None, "current_draw"):
            raise ValueError("trigger_count: when must be 'current_draw' or omitted")
        if not isinstance(equal_enable, bool):
            raise ValueError("trigger_count: equal_enable must be a boolean")
        self._history_key = history_key.strip()
        self._when = when
        self._equal_enable = equal_enable

    async def execute(self, ctx: EvalContext) -> None:  # noqa: C901 - 兼容三种 scratch 输入形态
        outcomes, counts = _most_triggered_outcomes(
            ctx,
            self._history_key,
            when=self._when,
            equal_enable=self._equal_enable,
        )

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(ctx, legs, outcomes, counts)
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
                kept = _filter_legs(ctx, legs, outcomes, counts)
                if not kept:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if isinstance(legs, list) and legs:
            ctx.scratch["legs"] = _filter_legs(ctx, legs, outcomes, counts)
            return
        ctx.scratch["legs"] = _current_legs(ctx, outcomes)


def _most_triggered_outcomes(
    ctx: EvalContext,
    history_key: str,
    *,
    when: str | None = None,
    equal_enable: bool = False,
) -> tuple[frozenset[str], dict[str, int]]:
    counts = dict.fromkeys(VALID_OUTCOMES, 0)
    store = ctx.consecutive_trigger_store
    current_score = _current_score_key(ctx)
    if store is None or current_score is None:
        return frozenset(), counts

    history = [
        observation
        for observation in store.history(ctx.pair_id, history_key)
        if same_set(observation.score_key, current_score)
    ]
    if when == "current_draw":
        draw_score = next(
            (
                observation.score_key
                for observation in reversed(history)
                if _is_draw(observation.score_key)
            ),
            None,
        )
        if draw_score is None:
            return frozenset(), counts
        cutoff = max(
            index
            for index, observation in enumerate(history)
            if observation.score_key == draw_score
        )
        while cutoff > 0 and history[cutoff - 1].score_key == draw_score:
            cutoff -= 1
        history = history[:cutoff]

    scores = {outcome: set() for outcome in VALID_OUTCOMES}
    for observation in history:
        if observation.passed and observation.outcome in scores:
            scores[observation.outcome].add(observation.score_key)
    counts = {outcome: len(values) for outcome, values in scores.items()}
    maximum = max(counts.values(), default=0)
    winners = [outcome for outcome, count in counts.items() if count == maximum]
    if maximum <= 0 or (not equal_enable and len(winners) != 1):
        return frozenset(), counts
    return frozenset(winners), counts


def _is_draw(value) -> bool:
    left, right, _, _ = value[-1]
    return left == right


def _filter_legs(
    ctx: EvalContext,
    legs: list[dict],
    target_outcomes: frozenset[str],
    counts: dict[str, int],
) -> list[dict]:
    kept = []
    for leg in legs:
        outcome = _leg_outcome(leg)
        if outcome in target_outcomes:
            kept.append(leg)
            continue
        _LOG.info(
            f"TriggerCount: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"outcome={outcome} target_outcomes={sorted(target_outcomes)} counts={counts}",
        )
    return kept


def _current_legs(ctx: EvalContext, outcomes: frozenset[str]) -> list[dict]:
    if not outcomes:
        return []
    share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
    if share <= 0:
        return []
    current_by_outcome = quote_legs_by_outcome(ctx)
    result = []
    for outcome in VALID_OUTCOMES:
        if outcome not in outcomes:
            continue
        current = current_by_outcome.get(outcome, [])
        if not current:
            continue
        leg = dict(
            min(
                current,
                key=lambda value: (
                    float(value["prob"]),
                    venue_preference_rank(value["venue"]),
                    str(value["instrument_id"]),
                ),
            )
        )
        leg["share_if_wins"] = share
        result.append(leg)
    return result


def _current_score_key(ctx: EvalContext):
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return None
    state = ctx.sports_store.get(game_id)
    return score_key(getattr(state, "score", "") if state is not None else "")


def _leg_outcome(leg: dict) -> str:
    return str(leg.get("claim") or leg.get("role") or "").strip().lower()
