"""VenueReplaceAction —— 将执行腿替换为同 outcome 的 Polymarket 腿。"""

from __future__ import annotations

import logging
from copy import deepcopy

from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class VenueReplaceAction(Action):
    """把非 PM 腿替换为同 pair、同 outcome 的 PM 路由腿。

    candidate 本质就是包了元数据的 legs 数组,`ctx.scratch["legs"]` 则是裸 legs 数组;两种
    表示的承重内容都是 legs,故 `legs` / `candidates` / candi_select 之后的
    `selected_candidate` 三种输入形态都支持。
    """

    async def execute(self, ctx: EvalContext) -> None:
        if ctx.scratch.get("cancel_pair_orders"):
            return

        pm_legs = _polymarket_legs_by_outcome(ctx)

        selected = ctx.scratch.get("selected_candidate")
        if isinstance(selected, dict):
            if selected.get("cancel_pair_orders"):
                return
            replaced = _replace_candidate(selected, pm_legs, ctx.pair_id)
            if replaced is None:
                ctx.scratch["selected_candidate"] = {}
                ctx.scratch["legs"] = []
                return
            ctx.scratch["selected_candidate"] = replaced
            ctx.scratch["legs"] = replaced["legs"]
            return

        if "candidates" in ctx.scratch:
            candidates = ctx.scratch.get("candidates")
            if not isinstance(candidates, list):
                _LOG.warning(f"VenueReplace: pair={ctx.pair_id} candidates is not list, clear")
                ctx.scratch["candidates"] = []
                return
            replaced_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                replaced = _replace_candidate(candidate, pm_legs, ctx.pair_id)
                if replaced is not None:
                    replaced_candidates.append(replaced)
            ctx.scratch["candidates"] = replaced_candidates
            return

        legs = ctx.scratch.get("legs")
        if not legs:
            return
        replaced = _replace_legs(legs, pm_legs, ctx.pair_id)
        ctx.scratch["legs"] = replaced or []


def _polymarket_legs_by_outcome(ctx: EvalContext) -> dict[str, dict]:
    result = {}
    for outcome, legs in quote_legs_by_outcome(ctx).items():
        pm_legs = [leg for leg in legs if str(leg.get("venue", "")).upper() == POLYMARKET]
        if pm_legs:
            result[outcome] = min(
                pm_legs,
                key=lambda leg: (float(leg["prob"]), str(leg["instrument_id"])),
            )
    return result


def _replace_candidate(candidate: dict, pm_legs: dict[str, dict], pair_id: str) -> dict | None:
    if candidate.get("cancel_pair_orders"):
        return deepcopy(candidate)
    replaced_legs = _replace_legs(candidate.get("legs") or [], pm_legs, pair_id)
    if replaced_legs is None:
        return None
    replaced = deepcopy(candidate)
    replaced["legs"] = replaced_legs
    return replaced


def _replace_legs(
    legs: list[dict],
    pm_legs: dict[str, dict],
    pair_id: str,
) -> list[dict] | None:
    if not legs:
        return None
    result = []
    for leg in legs:
        venue = str(leg.get("venue", "")).upper()
        if venue == POLYMARKET:
            result.append(deepcopy(leg))
            continue

        outcome = str(leg.get("claim") or leg.get("role") or "").lower()
        share = _share_if_wins(leg)
        pm_leg = pm_legs.get(outcome)
        if outcome not in VALID_OUTCOMES or share is None or share <= 0 or pm_leg is None:
            _LOG.warning(
                f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
                f"cannot replace outcome={outcome!r} share={share!r}, drop group",
            )
            return None

        replacement = deepcopy(pm_leg)
        # 用当前 order 的隐含概率(两 venue 共享的 `prob`),不用 PM 实时 ask,也不重算价格。
        # PM 是 probability venue,price 即概率;qty=share(不缩放),cost=share×prob,
        # 与原 decimal 腿的 cost 口径一致。
        prob = _order_prob(leg, pm_leg, pair_id)
        qty = qty_from_share(POLYMARKET, share, prob)
        replacement["price"] = prob
        replacement["prob"] = prob
        replacement["qty"] = qty
        replacement["share_if_wins"] = share
        replacement["cost"] = leg_economics(POLYMARKET, prob, qty).loss_if_loses
        result.append(replacement)
    return result


def _share_if_wins(leg: dict) -> float | None:
    value = leg.get("share_if_wins")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _order_prob(leg: dict, pm_leg: dict, pair_id: str) -> float:
    """当前 order 的隐含概率(PM 下单价)。

    优先用原腿自身的 `prob`(与两 venue 共享的 outcome 概率);裸腿没有 committed
    价格时回退到 PM 报价腿的概率,并告警。
    """
    prob = _positive_float(leg.get("prob"))
    if prob is not None:
        return prob
    fallback = _positive_float(pm_leg.get("prob")) or _positive_float(pm_leg.get("price"))
    _LOG.warning(
        f"VenueReplace: pair={pair_id} leg={leg.get('instrument_id')} "
        f"missing prob, fallback to PM quote prob={fallback}",
    )
    return fallback if fallback is not None else 0.0
