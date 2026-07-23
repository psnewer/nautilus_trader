"""
OneSideRebateCheck —— 定向返水候选生成。

算法:
  1. 按 outcome role 收集所有可买 leg(PM/OE/SE 都保留,不提前取最优)。
  2. 枚举每个 role 选一条 leg 的笛卡尔积。
  3. 对每个组合枚举 target role,计算 `rate = (1 - total_prob) / target_prob`。
  4. `rate >= min_rate` 时生成 candidate,写入 `ctx.scratch["candidates"]`。

后续 action 链:
  `share_limit -> candi_select -> place_bets`
"""

from __future__ import annotations

from itertools import product

from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class OneSideRebateCheck(Check):
    """枚举所有定向返水 candidate。"""

    def __init__(self, min_rate: float = 0.01, share: float | None = None) -> None:
        self._min_rate = float(min_rate)
        self._share = float(share) if share is not None else None

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False

        legs_by_role = _legs_by_role(ctx)
        roles = _roles_present(legs_by_role)
        if roles is None:
            return False
        share = self._configured_share(ctx)
        if share <= 0:
            return False

        candidates = []
        for combo in product(*(legs_by_role[role] for role in roles)):
            total_prob = sum(leg["prob"] for leg in combo)
            for target_role in roles:
                target = _leg_for_role(combo, target_role)
                if target is None or target["prob"] <= 0:
                    continue
                rate = (1.0 - total_prob) / target["prob"]
                if rate < self._min_rate:
                    continue
                candidate = self._build_candidate(
                    combo=combo,
                    roles=roles,
                    target_role=target_role,
                    share=share,
                    total_prob=total_prob,
                    rate=rate,
                )
                if candidate is not None:
                    candidates.append(candidate)

        if not candidates:
            return False

        ctx.scratch["candidates"] = candidates
        ctx.scratch["one_side_rebate"] = {
            "candidate_count": len(candidates),
            "min_rate": self._min_rate,
        }
        return True

    def _build_candidate(
        self,
        *,
        combo: tuple[dict, ...],
        roles: tuple[str, ...],
        target_role: str,
        share: float,
        total_prob: float,
        rate: float,
    ) -> dict | None:
        non_target_prob = sum(leg["prob"] for leg in combo if leg["role"] != target_role)
        target_cost = share * (1.0 - non_target_prob)
        if target_cost <= 0:
            return None

        candidate_legs = []
        for leg in combo:
            role = leg["role"]
            if role == target_role:
                cost = target_cost
                share_if_wins = cost / leg["prob"]
            else:
                share_if_wins = share
                cost = share * leg["prob"]

            qty = _qty_for_share_and_cost(
                venue=leg["venue"],
                price=leg["price"],
                share_if_wins=share_if_wins,
                cost=cost,
            )
            candidate_leg = {
                "instrument_id": leg["instrument_id"],
                "venue": leg["venue"],
                "side": "BUY",
                "price": leg["price"],
                "prob": leg["prob"],
                "role": role,
                "qty": qty,
                "share_if_wins": share_if_wins,
                "cost": cost,
            }
            # 合成 no 腿透传执行字段(place_bets SELL@lay 转换 + 重定向)
            for key in ("claim", "lay_price", "exec_instrument_id"):
                if key in leg:
                    candidate_leg[key] = leg[key]
            candidate_legs.append(candidate_leg)

        combo_key = ",".join(f"{leg['venue'].lower()}:{leg['role']}" for leg in combo)
        return {
            "candidate_id": f"one_side:{target_role}:{combo_key}",
            "strategy": "one_side_rebate",
            "target_role": target_role,
            "roles": roles,
            "rate": rate,
            "total_prob": total_prob,
            "base_share": share,
            "legs": candidate_legs,
        }

    def _configured_share(self, ctx: EvalContext) -> float:
        if self._share is not None:
            return self._share
        return float((ctx.strategy_defaults or {}).get("share") or 0.0)


def _legs_by_role(ctx) -> dict[str, list[dict]]:
    """#228:分组键 = claim 优先，fallback selection_role。"""
    return quote_legs_by_outcome(ctx)


def _roles_present(legs_by_role: dict[str, list[dict]]) -> tuple[str, ...] | None:
    """outcome 集合必须与统一的 yes/no 词汇表完全一致。"""
    present = tuple(outcome for outcome in VALID_OUTCOMES if outcome in legs_by_role)
    return present if present == VALID_OUTCOMES else None


def _leg_for_role(legs: tuple[dict, ...], role: str) -> dict | None:
    for leg in legs:
        if leg["role"] == role:
            return leg
    return None


def _qty_for_share_and_cost(
    *,
    venue: str,
    price: float,
    share_if_wins: float,
    cost: float,
) -> float:
    if price <= 0:
        return 0.0
    try:
        return qty_from_share(venue, share_if_wins, price)
    except KeyError:
        return 0.0
