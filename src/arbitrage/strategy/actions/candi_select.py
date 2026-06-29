"""
CandiSelectAction —— 从 share_limit 调整后的 candidate 数组中选择一个下单候选。

放在 action 链 `share_limit -> candi_select -> place_bets` 中。它只负责选择并把
`ctx.scratch["legs"]` 改成被选 candidate 的 legs,不提交订单。
"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class CandiSelectAction(Action):
    """选择 candidate 内最大 leg share 最大的 candidate。"""

    async def execute(self, ctx: EvalContext) -> None:
        if "candidates" not in ctx.scratch:
            return

        candidates = ctx.scratch.get("candidates") or []
        if not candidates:
            ctx.scratch["legs"] = []
            _LOG.info(f"CandiSelect: pair={ctx.pair_id} no candidates")
            return

        selected = max(candidates, key=_candidate_score)
        ctx.scratch["selected_candidate"] = selected
        ctx.scratch["legs"] = selected.get("legs", [])

        _LOG.info(
            f"CandiSelect: pair={ctx.pair_id} candidates={len(candidates)} "
            f"selected={selected.get('candidate_id', selected.get('candidate_index'))} "
            f"max_candidate_share={_candidate_score(selected):.4f}"
        )


def _candidate_score(candidate: dict) -> float:
    shares = [
        float(leg["share_if_wins"])
        for leg in candidate.get("legs", [])
        if leg.get("share_if_wins") is not None
    ]
    return max(shares) if shares else 0.0
