"""
MeanRebateRecoveryCheck —— mean_rebate 补救检查。

目标:已有不完整持仓时,把每个 outcome 补到当前最大实际 share,并要求补齐后的最差
rebate 不低于 `min_repaired_rebate`。命中时只写缺口 legs,供 `PlaceBetsAction(intent="recovery")`
下补救单。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.arbitrage.strategy.checks.mean_rebate import _VALID_ROLES
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
    fx: float

    def profit_if_wins(self) -> float:
        if self.venue == "POLYMARKET":
            return self.qty * (1.0 - self.price)
        return self.qty * (self.price - 1.0) * self.fx

    def loss_if_loses(self) -> float:
        if self.venue == "POLYMARKET":
            return self.qty * self.price
        return self.qty * self.fx

    def share_if_wins(self) -> float:
        if self.venue == "POLYMARKET":
            return self.qty
        return self.qty * self.price * self.fx


class MeanRebateRecoveryCheck(Check):
    """补齐缺口后,若最差 outcome rebate 达标则写 recovery legs。"""

    def __init__(self, min_repaired_rebate: float = -0.05, fx: float = 1.0) -> None:
        self._min_repaired_rebate = float(min_repaired_rebate)
        self._fx = float(fx)

    def passes(self, ctx: EvalContext) -> bool:
        snap = ctx.snapshot
        if snap is None:
            return False

        existing = _existing_legs(snap, self._fx)
        if not existing:
            return False
        actual_by_role = _actual_share_by_role(existing)
        target_share = max(actual_by_role.values())
        if target_share <= _EPS:
            return False

        candidates = _best_candidates_by_role(snap)
        roles_present = sorted(candidates.keys())
        if not (roles_present == ["away", "home"] or roles_present == ["away", "draw", "home"]):
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
            qty = missing if cand["venue"] == "POLYMARKET" else missing / (cand["price"] * self._fx)
            recovery_specs.append({
                "instrument_id": cand["instrument_id"],
                "venue": cand["venue"],
                "side": "BUY",
                "price": cand["price"],
                "prob": cand["prob"],
                "role": role,
                "qty": qty,
            })
            repaired_legs.append(_CalcLeg(cand["venue"], role, qty, cand["price"], self._fx))

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


def _existing_legs(snap, fx: float) -> list[_CalcLeg]:
    result: list[_CalcLeg] = []
    for position in snap.positions:
        iid = getattr(position, "instrument_id", None)
        info = _info_for(snap, iid)
        role = info.get("selection_role") or info.get("market_type")
        if role not in _VALID_ROLES:
            continue
        venue = _venue_of(iid)
        if venue not in ("POLYMARKET", "ORBITEXCH"):
            continue
        qty = abs(position.quantity.as_double())
        price = float(position.avg_px_open)
        if qty <= 0 or price <= 0:
            continue
        result.append(_CalcLeg(venue=venue, role=role, qty=qty, price=price, fx=fx))
    return result


def _actual_share_by_role(legs: list[_CalcLeg]) -> dict[str, float]:
    result: dict[str, float] = {}
    for leg in legs:
        result[leg.role] = result.get(leg.role, 0.0) + leg.share_if_wins()
    return result


def _best_candidates_by_role(snap) -> dict[str, dict]:
    candidates: dict[str, list[dict]] = {}
    for iid in snap.instrument_ids:
        info = _info_for(snap, iid)
        role = info.get("selection_role")
        if role not in _VALID_ROLES:
            continue
        book = snap.order_books.get(iid)
        if book is None:
            continue
        venue = _venue_of(iid)
        price = _best_ask(book)
        if price is None or price <= 0:
            continue
        prob = _to_prob(venue, price)
        if prob <= 0:
            continue
        candidates.setdefault(role, []).append({
            "instrument_id": iid,
            "venue": venue,
            "price": price,
            "prob": prob,
        })
    return {
        role: min(legs, key=lambda leg: (leg["prob"], 0 if leg["venue"] == "POLYMARKET" else 1))
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
