"""VenueSelectAction —— 按是否为 Polymarket 筛选执行腿。"""

from __future__ import annotations

import logging

from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class VenueSelectAction(Action):
    """pm=True 时只保留 PM 腿；pm=False 时删除 PM 腿。"""

    def __init__(self, pm: bool = True) -> None:
        if not isinstance(pm, bool):
            raise ValueError(f"venue_select: pm must be a boolean, got {pm!r}")
        self._pm = pm

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
            is_pm = str(leg.get("venue") or "").upper() == POLYMARKET
            if is_pm == self._pm:
                kept.append(leg)
                continue
            _LOG.info(
                f"VenueSelect: pair={ctx.pair_id} drop leg={leg.get('instrument_id')} "
                f"venue={leg.get('venue')} pm={self._pm}",
            )
        return kept
