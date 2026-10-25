"""查询最近一次未被打断的连续触发并生成执行腿。"""

from __future__ import annotations

from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


class LatestConsecutiveTriggerAction(Action):
    """只读连续触发历史尾部，按触发 outcome 从当前报价生成一条腿。"""

    def __init__(self, history_key: str) -> None:
        if not isinstance(history_key, str) or not history_key.strip():
            raise ValueError("latest_consecutive_trigger: history_key must be a non-empty string")
        self._history_key = history_key.strip()

    async def execute(self, ctx: EvalContext) -> None:
        ctx.scratch["legs"] = []
        store = ctx.consecutive_trigger_store
        if store is None:
            return
        observation = store.latest(ctx.pair_id, self._history_key)
        if observation is None or not observation.passed:
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
