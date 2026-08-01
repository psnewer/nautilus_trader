"""
MeanRebateRecoveryCheck —— mean_rebate 补救检查。

目标:已有不完整持仓时,把每个 outcome 补到当前最大实际 share。

`min_repaired_rebate` 是**双向**门槛(#262):
- **要不要补**:当前最差 outcome 的 rebate **低于**该值才需要补救(否则仓位已达标,不该花钱);
- **补了有没有用**:补齐后的最差 rebate 必须**不低于**该值。

两者共用同一分母(当前最大实际 share),口径一致、直接可比。
命中时只写缺口 legs,供 `PlaceBetsAction(intent="recovery")` 下补救单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nautilus_trader.model.enums import PositionSide
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import is_known_venue
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import outcome_for_position
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.checks.quote_legs import quote_legs_by_outcome
from src.arbitrage.strategy.checks.quote_legs import venue_of as _venue_of
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CalcLeg:
    venue: str
    role: str
    qty: float
    price: float
    is_lay: bool = False

    def profit_if_wins(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.qty,
            is_lay=self.is_lay,
        ).profit_if_wins

    def loss_if_loses(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.qty,
            is_lay=self.is_lay,
        ).loss_if_loses

    def share_if_wins(self) -> float:
        return leg_economics(
            self.venue,
            self.price,
            self.qty,
            is_lay=self.is_lay,
        ).share_if_wins


class MeanRebateRecoveryCheck(Check):
    """补齐缺口后,若最差 outcome rebate 达标则写 recovery legs。"""

    def __init__(self, min_repaired_rebate: float = -0.05, venue_select: bool = False) -> None:
        self._min_repaired_rebate = float(min_repaired_rebate)
        # venue_select=True:补救腿只在 PM 里选(某 outcome 无 PM 报价则该 role 缺席 →
        # 后续 roles_present 校验 fail-closed,不补)。缺省/False 保持现状:各 venue 取最优赔率。
        self._venue_select = bool(venue_select)

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        valid_outcomes = VALID_OUTCOMES
        if len(valid_outcomes) < 2:
            return False

        try:
            existing = _existing_legs(ctx, valid_outcomes)
        except PositionOutcomeInvariantError as e:
            _LOG.error(f"MeanRebateRecovery: pair={ctx.pair_id} portfolio invariant: {e}")
            return False
        if not existing:
            return False
        actual_by_role = _actual_share_by_role(existing)
        target_share = max(actual_by_role.values())
        if target_share <= _EPS:
            return False

        # #262 前置门:**当前**最差 outcome 已达标 → 本就不需要补救,直接放弃。
        # 原先只判「补齐后达标」(见下方 `repaired` 判断),缺这一半 → 仓位已接近平衡时照样触发,
        # 算出极小的补单被 Risk 以 `min_notional` 拒掉,且状态不变 → 每个 OBD tick 重复一次。
        # 加上之后 `min_repaired_rebate` 才自洽地同时定义两件事:
        #   current  < 阈值 → 需要补;   repaired >= 阈值 → 补了确实有用。
        # 与 `repaired` 共用同一分母 `target_share`,两个数直接可比、同一把尺子。
        # 放在取盘口候选之前:本判据不需要行情,能早退就早退。
        current_net = _current_net_by_outcome(ctx, existing, sorted(valid_outcomes))
        current = _rates_from_net(current_net, target_share)
        if current and min(current.values()) >= self._min_repaired_rebate:
            return False

        candidates = _best_candidates_by_role(ctx, valid_outcomes, pm_only=self._venue_select)
        roles_present = sorted(candidates.keys())
        if roles_present != sorted(valid_outcomes):
            return False

        recovery_specs = []
        repair_legs = []
        for role in roles_present:
            missing = target_share - actual_by_role.get(role, 0.0)
            if missing <= _EPS:
                continue
            cand = candidates.get(role)
            if cand is None:
                return False
            qty = qty_from_share(cand["venue"], missing, cand["price"])
            spec = {
                "instrument_id": cand["instrument_id"],
                "venue": cand["venue"],
                "side": "BUY",
                "price": cand["price"],
                "prob": cand["prob"],
                "role": role,
                "qty": qty,
            }
            for key in ("claim", "lay_price", "exec_instrument_id"):
                if key in cand:
                    spec[key] = cand[key]
            recovery_specs.append(spec)
            repair_legs.append(_CalcLeg(
                cand["venue"],
                role,
                qty,
                cand["price"],
                is_lay=bool(cand.get("exec_instrument_id")) and is_decimal_odds_venue(cand["venue"]),
            ))

        if not recovery_specs:
            return False

        repaired_net = _add_legs_to_net(current_net, repair_legs, roles_present)
        repaired = _rates_from_net(repaired_net, target_share)
        if not repaired or min(repaired.values()) < self._min_repaired_rebate:
            return False

        ctx.scratch["legs"] = recovery_specs
        ctx.scratch["mean_rebate_recovery"] = {
            "target_share": target_share,
            "repaired_rebate": repaired,
            "min_repaired_rebate": min(repaired.values()),
        }
        return True


def _existing_legs(ctx, outcomes: tuple[str, ...]) -> list[_CalcLeg]:
    result: list[_CalcLeg] = []
    for position in pair_positions(ctx):
        iid = getattr(position, "instrument_id", None)
        info = instrument_info(ctx, iid)
        selection_role = info.get("selection_role") or info.get("market_type")
        claim = info.get("claim")
        venue = _venue_of(iid)
        if not is_known_venue(venue):
            continue
        position_side = getattr(position, "side", None)
        # size 让 outcome_for_position 判 dust:±dust 净仓返回 None → 这里跳过(撮合误差,非真实腿)。
        role = outcome_for_position(
            venue,
            outcomes,
            selection_role=selection_role,
            claim=claim,
            position_side=position_side,
            size=abs(position.quantity.as_double()),
        )
        if role is None:
            continue
        qty = abs(position.quantity.as_double())
        price = float(position.avg_px_open)
        if qty <= 0 or price <= 0:
            continue
        result.append(_CalcLeg(
            venue=venue,
            role=role,
            qty=qty,
            price=price,
            is_lay=position_side == PositionSide.SHORT,
        ))
    return result


def _actual_share_by_role(legs: list[_CalcLeg]) -> dict[str, float]:
    result: dict[str, float] = {}
    for leg in legs:
        result[leg.role] = result.get(leg.role, 0.0) + leg.share_if_wins()
    return result


def _best_candidates_by_role(
    ctx,
    outcomes: tuple[str, ...],
    *,
    pm_only: bool = False,
) -> dict[str, dict]:
    candidates = quote_legs_by_outcome(ctx)
    result: dict[str, dict] = {}
    for role, legs in candidates.items():
        pool = (
            [leg for leg in legs if str(leg["venue"]).upper() == POLYMARKET]
            if pm_only
            else legs
        )
        if not pool:
            continue
        result[role] = min(pool, key=lambda leg: (leg["prob"], venue_preference_rank(leg["venue"])))
    return result


def _outcome_return_rates(legs: list[_CalcLeg], roles: list[str], share: float) -> dict[str, float]:
    result: dict[str, float] = {}
    for outcome in roles:
        net = 0.0
        for leg in legs:
            if leg.role == outcome:
                net += leg.profit_if_wins()
            else:
                net -= leg.loss_if_loses()
        result[outcome] = net / share if share > 0 else 0.0
    return result


def _current_net_by_outcome(
    ctx: EvalContext,
    existing: list[_CalcLeg],
    outcomes: list[str],
) -> dict[str, float]:
    portfolio = ctx.portfolio
    if portfolio is not None:
        exposures = portfolio.outcome_exposures(ctx.pair_id)
        if exposures:
            return {
                outcome: float(exposures[outcome].net_profit)
                for outcome in outcomes
                if outcome in exposures
            }
    return {
        outcome: sum(
            leg.profit_if_wins() if leg.role == outcome else -leg.loss_if_loses()
            for leg in existing
        )
        for outcome in outcomes
    }


def _add_legs_to_net(
    baseline: dict[str, float],
    legs: list[_CalcLeg],
    outcomes: list[str],
) -> dict[str, float]:
    return {
        outcome: baseline.get(outcome, 0.0) + sum(
            leg.profit_if_wins() if leg.role == outcome else -leg.loss_if_loses()
            for leg in legs
        )
        for outcome in outcomes
    }


def _rates_from_net(net_by_outcome: dict[str, float], share: float) -> dict[str, float]:
    return {
        outcome: net / share if share > 0 else 0.0
        for outcome, net in net_by_outcome.items()
    }
