"""
ShareLimitModification —— 在 strategy action 链中执行 share limit 缩放。

两种输入:
- 单一机会:读取 `ctx.scratch["legs"]`,按配置 `share` 或 leg 自带 `share_if_wins/qty`
  计算目标 share,直接写回调整后的 `qty/share_if_wins/cost`。
- candidate 数组:读取 `ctx.scratch["candidates"]`,对每个 candidate 独立计算 scale,
  输出调整后的 candidate 数组,供后续 `CandiSelectAction` 选择。

复用 RiskEngine 的 PM/OE 公式:
  - PM: remaining = max - current[role]（单腿独立检查）
  - OE: remaining = max - merged[role]（merge后一边为0）
"""

from __future__ import annotations

import logging
from copy import deepcopy

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class ShareLimitModification(Action):
    """按 share limit 直接调整 legs 或 candidates。"""

    def __init__(self, max_leg_share: float | None = None, share: float | None = None, fx: float | None = None) -> None:
        self._max_leg_share = float(max_leg_share) if max_leg_share is not None else None
        self._share = float(share) if share is not None else None
        self._fx = float(fx) if fx is not None else None

    async def execute(self, ctx: EvalContext) -> None:
        max_leg_share = self._configured_max_leg_share(ctx)
        if max_leg_share is None:
            return
        fx = self._configured_fx(ctx)
        if self._adjust_candidates(ctx):
            return

        legs = ctx.scratch.get("legs", [])
        if not legs:
            return

        portfolio = ctx.portfolio
        if portfolio is None:
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} no portfolio, skip")
            return

        requested_by_leg = []
        remaining_values = []
        scale = 1.0
        for leg in legs:
            venue = str(leg.get("venue", "")).upper()
            role = str(leg.get("role", ""))
            if not venue or not role:
                continue
            requested_share = _leg_share_if_wins(leg, venue, fx, self._configured_share(ctx))
            if requested_share <= 0:
                continue
            if venue == "ORBITEXCH":
                remaining = self._oe_remaining(portfolio, ctx.pair_id, role, max_leg_share)
            else:
                remaining = self._pm_remaining(portfolio, ctx.pair_id, role, max_leg_share)
            if remaining <= 0:
                _LOG.info(
                    f"ShareLimitModification: pair={ctx.pair_id} remaining={remaining:.4f} "
                    f"<= 0, clear legs"
                )
                ctx.scratch["legs"] = []
                return
            scale = min(scale, remaining / requested_share)
            remaining_values.append(remaining)
            requested_by_leg.append((leg, requested_share))

        if not requested_by_leg:
            return

        ctx.scratch["legs"] = _adjust_legs(requested_by_leg, scale)
        ctx.scratch["share_limit_scale"] = scale
        ctx.scratch["adjusted_share"] = (self._share or min(s for _, s in requested_by_leg)) * scale
        _LOG.debug(
            f"ShareLimitModification: pair={ctx.pair_id} scale={scale:.4f} "
            f"adjusted_share={ctx.scratch['adjusted_share']:.4f}"
        )

    def _adjust_candidates(self, ctx: EvalContext) -> bool:
        if "candidates" not in ctx.scratch:
            return False

        max_leg_share = self._configured_max_leg_share(ctx)
        if max_leg_share is None:
            return True
        fx = self._configured_fx(ctx)
        candidates = ctx.scratch.get("candidates") or []
        if not isinstance(candidates, list):
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} candidates is not list, skip")
            return True

        portfolio = ctx.portfolio
        if portfolio is None:
            _LOG.warning(f"ShareLimitModification: pair={ctx.pair_id} no portfolio, clear candidates")
            ctx.scratch["candidates"] = []
            return True

        adjusted_candidates = []
        for idx, candidate in enumerate(candidates):
            adjusted = self._adjust_candidate(portfolio, ctx.pair_id, candidate, idx, max_leg_share, fx)
            if adjusted is not None:
                adjusted_candidates.append(adjusted)

        ctx.scratch["candidates"] = adjusted_candidates
        _LOG.debug(
            f"ShareLimitModification: pair={ctx.pair_id} candidates={len(candidates)} "
            f"adjusted={len(adjusted_candidates)}"
        )
        return True

    def _adjust_candidate(
        self,
        portfolio,
        pair_id: str,
        candidate: dict,
        idx: int,
        max_leg_share: float,
        fx: float,
    ) -> dict | None:
        legs = candidate.get("legs") or []
        if not legs:
            return None

        base_share = _candidate_base_share(candidate)
        requested_by_leg = []
        remaining_values = []
        scale = 1.0
        for leg in legs:
            venue = str(leg.get("venue", "")).upper()
            role = str(leg.get("role", ""))
            if not venue or not role:
                return None

            requested_share = _leg_share_if_wins(leg, venue, fx, base_share)
            if requested_share <= 0:
                return None

            if venue == "ORBITEXCH":
                remaining = self._oe_remaining(portfolio, pair_id, role, max_leg_share)
            else:
                remaining = self._pm_remaining(portfolio, pair_id, role, max_leg_share)
            if remaining <= 0:
                return None
            scale = min(scale, remaining / requested_share)
            remaining_values.append(remaining)
            requested_by_leg.append((leg, requested_share))

        if scale <= 0:
            return None

        adjusted = deepcopy(candidate)
        adjusted_legs = _adjust_legs(requested_by_leg, scale)

        adjusted["legs"] = adjusted_legs
        adjusted["share_limit_scale"] = scale
        adjusted["adjusted_share"] = base_share * scale if base_share > 0 else min(
            leg["share_if_wins"] for leg in adjusted_legs
        )
        adjusted.setdefault("candidate_index", idx)
        return adjusted

    def _pm_remaining(self, portfolio, pair_id: str, role: str, max_leg_share: float) -> float:
        """PM: 单腿独立检查。"""
        shares = portfolio.outcome_shares_for_venue(pair_id, "polymarket", None)
        current = shares.get(role, 0.0)
        return max_leg_share - current

    def _oe_remaining(self, portfolio, pair_id: str, role: str, max_leg_share: float) -> float:
        """OE: 按 merge 后的单腿 share 计算 remaining。

        OE 平台自动对冲，margin 按净敞口计算。remaining 基于 merge 后的 share:
        - home=60, away=40 → merge后 home=20, away=0
        - home remaining = max - 20 = 80
        - away remaining = max - 0 = 100
        """
        shares = portfolio.outcome_shares_for_venue(pair_id, "orbitexch", None)
        home = shares.get("home", 0.0)
        away = shares.get("away", 0.0)

        # merge 后的 share
        if home >= away:
            merged_home = home - away
            merged_away = 0.0
        else:
            merged_home = 0.0
            merged_away = away - home

        if role == "home":
            return max_leg_share - merged_home
        else:  # away
            return max_leg_share - merged_away

    def _configured_max_leg_share(self, ctx: EvalContext) -> float | None:
        if self._max_leg_share is not None:
            return self._max_leg_share
        value = (ctx.strategy_defaults or {}).get("max_leg_share")
        return float(value) if value is not None else None

    def _configured_share(self, ctx: EvalContext) -> float:
        if self._share is not None:
            return self._share
        return float((ctx.strategy_defaults or {}).get("share") or 0.0)

    def _configured_fx(self, ctx: EvalContext) -> float:
        if self._fx is not None:
            return self._fx
        return float((ctx.strategy_defaults or {}).get("fx") or 1.0)


def _candidate_base_share(candidate: dict) -> float:
    for key in ("base_share", "share", "target_share"):
        value = candidate.get(key)
        if value is not None:
            return float(value)
    shares = [
        float(leg["share_if_wins"])
        for leg in candidate.get("legs", [])
        if leg.get("share_if_wins") is not None
    ]
    return min(shares) if shares else 0.0


def _leg_share_if_wins(leg: dict, venue: str, fx: float, fallback_share: float) -> float:
    if leg.get("share_if_wins") is not None:
        return float(leg["share_if_wins"])
    if leg.get("qty") is not None:
        qty = float(leg["qty"])
        if venue == "ORBITEXCH":
            return qty * float(leg.get("price", 0.0)) * fx
        return qty
    return fallback_share


def _adjust_legs(requested_by_leg: list[tuple[dict, float]], scale: float) -> list[dict]:
    adjusted_legs = []
    for leg, requested_share in requested_by_leg:
        venue = str(leg.get("venue", "")).upper()
        price = float(leg.get("price", 0.0))
        new_leg = deepcopy(leg)
        new_share = requested_share * scale
        new_leg["share_if_wins"] = new_share
        if "qty" in new_leg:
            new_leg["qty"] = float(new_leg["qty"]) * scale
        elif venue == "ORBITEXCH":
            new_leg["qty"] = new_share / price if price > 0 else 0.0
        else:
            new_leg["qty"] = new_share
        if "cost" in new_leg:
            new_leg["cost"] = float(new_leg["cost"]) * scale
        return_leg = new_leg
        adjusted_legs.append(return_leg)
    return adjusted_legs
