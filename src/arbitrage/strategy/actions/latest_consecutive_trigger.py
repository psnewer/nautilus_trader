"""查询最近一次未被打断的连续触发并生成执行腿。"""

from __future__ import annotations

from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.consecutive_triggers import same_set
from src.arbitrage.strategy.consecutive_triggers import score_key


class LatestConsecutiveTriggerAction(Action):
    """只读连续触发历史，按触发 outcome 从当前报价生成一条腿。"""

    def __init__(
        self,
        history_key: str,
        limit: bool = True,
        current_set: bool = False,
    ) -> None:
        if not isinstance(history_key, str) or not history_key.strip():
            raise ValueError("latest_consecutive_trigger: history_key must be a non-empty string")
        if not isinstance(limit, bool):
            raise ValueError("latest_consecutive_trigger: limit must be a boolean")
        if not isinstance(current_set, bool):
            raise ValueError("latest_consecutive_trigger: current_set must be a boolean")
        self._history_key = history_key.strip()
        self._limit = limit
        self._current_set = current_set

    async def execute(self, ctx: EvalContext) -> None:
        ctx.scratch["legs"] = []
        store = ctx.consecutive_trigger_store
        if store is None:
            return
        current_score = _current_score_key(ctx) if self._current_set else None
        if self._current_set and current_score is None:
            return
        if self._limit:
            observation = store.latest(ctx.pair_id, self._history_key)
        else:
            observation = next(
                (
                    value
                    for value in store.iter_recent(ctx.pair_id, self._history_key)
                    if value.passed
                    and (
                        not self._current_set
                        or same_set(value.score_key, current_score)
                    )
                ),
                None,
            )
        if (
            observation is None
            or not observation.passed
            or (
                self._current_set
                and not same_set(observation.score_key, current_score)
            )
        ):
            return
        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
        if share <= 0:
            return
        current = quote_legs_by_outcome(ctx).get(observation.outcome, [])
        if not current:
            return
        leg = dict(min(
            current,
            key=lambda value: (
                float(value["prob"]),
                venue_preference_rank(value["venue"]),
                str(value["instrument_id"]),
            ),
        ))
        leg["share_if_wins"] = share
        ctx.scratch["legs"] = [leg]


def _current_score_key(ctx: EvalContext):
    if ctx.sports_store is None or ctx.pair_registry is None:
        return None
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    if game_id is None:
        return None
    state = ctx.sports_store.get(game_id)
    return score_key(getattr(state, "score", "") if state is not None else "")
