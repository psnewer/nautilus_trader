"""在指定当前盘清除下单腿。"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.in_game_positions import current_set_number


_LOG = logging.getLogger(__name__)


class SetExemptAction(Action):
    """当前盘等于配置盘号时拦截普通下单腿，保留撤单候选。"""

    def __init__(self, set: int) -> None:
        if isinstance(set, bool) or not isinstance(set, int) or set <= 0:
            raise ValueError("set_exempt: set must be a positive integer")
        self._set = set

    async def execute(self, ctx: EvalContext) -> None:
        current_set = current_set_number(
            pair_id=ctx.pair_id,
            pair_registry=ctx.pair_registry,
            sports_store=ctx.sports_store,
        )
        if current_set != self._set:
            return

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            filtered = dict(selected)
            filtered["legs"] = []
            ctx.scratch["selected_candidate"] = filtered
            ctx.scratch["legs"] = []
        elif "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                ctx.scratch["candidates"] = []
                ctx.scratch["legs"] = []
                _LOG.info(f"SetExempt: pair={ctx.pair_id} current_set={current_set} blocked")
                return
            ctx.scratch["candidates"] = [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("cancel_pair_orders")
            ]
            ctx.scratch["legs"] = []
        elif isinstance(ctx.scratch.get("legs"), list):
            ctx.scratch["legs"] = []
        _LOG.info(f"SetExempt: pair={ctx.pair_id} current_set={current_set} blocked")
