"""
MeanRebateRecoveryCheck —— mean_rebate 补救检查。

目标:已有不完整持仓时,把每个 outcome 补到当前最大实际 share,并要求补齐后的最差
rebate 不低于 `min_repaired_rebate`。命中时只写缺口 legs,供 `PlaceBetsAction(intent="recovery")`
下补救单。
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.enums import PositionSide
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import is_known_venue
from src.arbitrage.common.venues import leg_economics
from src.arbitrage.common.venues import outcome_for_position
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.checks.mean_rebate import _best_ask
from src.arbitrage.strategy.checks.mean_rebate import _to_prob
from src.arbitrage.strategy.checks.mean_rebate import _venue_of
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9


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

    def __init__(self, min_repaired_rebate: float = -0.05) -> None:
        self._min_repaired_rebate = float(min_repaired_rebate)

    def passes(self, ctx: EvalContext) -> bool:
        snap = ctx.snapshot
        if snap is None:
            return False
        valid_outcomes = tuple(
            str(value).lower()
            for value in (getattr(snap, "outcomes", None) or ("home", "away"))
        )
        if len(valid_outcomes) < 2:
            return False

        existing = _existing_legs(snap, valid_outcomes)
        if not existing:
            return False
        actual_by_role = _actual_share_by_role(existing)
        target_share = max(actual_by_role.values())
        if target_share <= _EPS:
            return False

        candidates = _best_candidates_by_role(snap, valid_outcomes)
        roles_present = sorted(candidates.keys())
        if roles_present != sorted(valid_outcomes):
            return False

        recovery_specs = []
        repaired_legs = list(existing)
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
            repaired_legs.append(_CalcLeg(
                cand["venue"],
                role,
                qty,
                cand["price"],
                is_lay=bool(cand.get("exec_instrument_id")) and is_decimal_odds_venue(cand["venue"]),
            ))

        if not recovery_specs:
            return False

        repaired = _outcome_return_rates(repaired_legs, roles_present, target_share)
        if not repaired or min(repaired.values()) < self._min_repaired_rebate:
            return False

        ctx.scratch["legs"] = recovery_specs
        ctx.scratch["mean_rebate_recovery"] = {
            "target_share": target_share,
            "repaired_rebate": repaired,
            "min_repaired_rebate": min(repaired.values()),
        }
        return True


def _existing_legs(snap, outcomes: tuple[str, ...]) -> list[_CalcLeg]:
    result: list[_CalcLeg] = []
    for position in snap.positions:
        iid = getattr(position, "instrument_id", None)
        info = _info_for(snap, iid)
        selection_role = info.get("selection_role") or info.get("market_type")
        claim = info.get("claim")
        venue = _venue_of(iid)
        if not is_known_venue(venue):
            continue
        position_side = getattr(position, "side", None)
        role = outcome_for_position(
            venue,
            outcomes,
            selection_role=selection_role,
            claim=claim,
            position_side=position_side,
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


def _best_candidates_by_role(snap, outcomes: tuple[str, ...]) -> dict[str, dict]:
    candidates: dict[str, list[dict]] = {}
    for iid in snap.instrument_ids:
        info = _info_for(snap, iid)
        claim = str(info.get("claim") or "").lower()
        quote_claim = str(info.get("quote_claim") or "yes").lower()
        role = claim or str(info.get("selection_role") or info.get("market_type") or "").lower()
        if role not in outcomes:
            continue
        book = snap.order_books.get(iid)
        if book is None:
            continue
        venue = _venue_of(iid)
        price = _best_ask(book)
        if price is None or price <= 0:
            continue
        prob = _to_prob(venue, price, quote_claim)
        if prob <= 0:
            continue
        leg = {
            "instrument_id": iid,
            "venue": venue,
            "price": price,
            "prob": prob,
        }
        if claim:
            leg["claim"] = claim
        if info.get("exec_instrument_id"):
            leg["lay_price"] = price
            exec_iid = info.get("exec_instrument_id")
            if exec_iid:
                leg["exec_instrument_id"] = str(exec_iid)
        candidates.setdefault(role, []).append(leg)
    return {
        role: min(legs, key=lambda leg: (leg["prob"], venue_preference_rank(leg["venue"])))
        for role, legs in candidates.items()
    }


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


def _info_for(snap, instrument_id) -> dict:
    info = _safe_get(snap.instrument_info, instrument_id)
    if info:
        return info

    instrument_id_str = str(instrument_id)
    info = _safe_get(snap.instrument_info, instrument_id_str)
    if info:
        return info

    for key, value in getattr(snap.instrument_info, "items", lambda: [])():
        if str(key) == instrument_id_str:
            return value or {}
    return {}


def _safe_get(mapping, key):
    try:
        return mapping.get(key)
    except TypeError:
        return None
