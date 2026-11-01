"""按当前盘各 outcome 的去重触发次数筛选执行腿。"""

from __future__ import annotations

import logging

from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


_LOG = logging.getLogger(__name__)


class TriggerCountAction(Action):
    """只保留当前盘去重触发次数唯一最多的 outcome 腿。"""

    def __init__(self, history_key: str) -> None:
        if not isinstance(history_key, str) or not history_key.strip():
            raise ValueError("trigger_count: history_key must be a non-empty string")
        self._history_key = history_key.strip()

    async def execute(self, ctx: EvalContext) -> None:
        outcome, counts = _most_triggered_outcome(ctx, self._history_key)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            legs = selected.get("legs")
            if not isinstance(legs, list) or not legs:
                return
            kept = _filter_legs(ctx, legs, outcome, counts)
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
                kept = _filter_legs(ctx, legs, outcome, counts)
                if not kept:
                    continue
                filtered = dict(candidate)
                filtered["legs"] = kept
                filtered_candidates.append(filtered)
            ctx.scratch["candidates"] = filtered_candidates
            return

        legs = ctx.scratch.get("legs")
        if isinstance(legs, list) and legs:
            ctx.scratch["legs"] = _filter_legs(ctx, legs, outcome, counts)
            return
        ctx.scratch["legs"] = _current_leg(ctx, outcome)


def _most_triggered_outcome(
    ctx: EvalContext,
    history_key: str,
) -> tuple[str | None, dict[str, int]]:
    counts = {outcome: 0 for outcome in VALID_OUTCOMES}
    store = ctx.consecutive_trigger_store
    current_score = _current_score_key(ctx)
    if store is None or current_score is None:
        return None, counts

    scores = {outcome: set() for outcome in VALID_OUTCOMES}
    for observation in store.history(ctx.pair_id, history_key):
        if (
            observation.passed
            and observation.outcome in scores
            and same_set(observation.score_key, current_score)
        ):
            scores[observation.outcome].add(observation.score_key)
    counts = {outcome: len(values) for outcome, values in scores.items()}
    maximum = max(counts.values(), default=0)
    winners = [outcome for outcome, count in counts.items() if count == maximum]
    return (winners[0] if maximum > 0 and len(winners) == 1 else None), counts


def _filter_legs(
    ctx: EvalContext,
    legs: list[dict],
    target_outcome: str | None,
    counts: dict[str, int],
) -> list[dict]:
    kept = []
    for leg in legs:
        outcome = _leg_outcome(leg)
        if target_outcome is not None and outcome == target_outcome:
            kept.append(leg)
            continue
        _LOG.info(
            f"TriggerCount: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
            f"outcome={outcome} target_outcome={target_outcome} counts={counts}",
        )
    return kept


def _current_leg(ctx: EvalContext, outcome: str | None) -> list[dict]:
    if outcome is None:
        return []
    share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
    if share <= 0:
        return []
    current = quote_legs_by_outcome(ctx).get(outcome, [])
    if not current:
        return []
    leg = dict(min(
        current,
        key=lambda value: (
            float(value["prob"]),
            venue_preference_rank(value["venue"]),
            str(value["instrument_id"]),
        ),
    ))
    leg["share_if_wins"] = share
    return [leg]


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
