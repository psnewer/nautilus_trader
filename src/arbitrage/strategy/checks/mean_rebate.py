"""
MeanRebateCheck —— 平均返水套利检查(slice 9 / #49)。

算法(对应 requirements §8):
  1. 按 `instrument.info["selection_role"]` 分组(home / draw / away),每方向取 PM/OE 类 venue 中
     概率最小者(即 best_ask 最便宜方)
  2. mean_rebate_rate = 1 - sum_outcomes(min_prob)
  3. >= `min_rate` 阈值 → True;同时写带 `share_if_wins` 的 `ctx.scratch["legs"]`
     供 Action 消费。`share` 可在本 Check params 中显式配置;未配置则读 Web Arbitrage 默认。

输出 legs 形态(每方向一条):
  {
    "instrument_id": InstrumentId,
    "venue": "POLYMARKET" | "ORBITEXCH" | "SHARPEXCH",
    "side": "BUY",
    "price": float (原始价 — PM 是 0-1 概率,OE 是 stake odds),
    "prob": float,
    "role": "home" | "draw" | "away",
    "share_if_wins": float,
  }

PlaceBetsAction 用 leg 自带 `share_if_wins` 推 qty:PM size=share,OE/SE size=share/price(stake)。
"""

from __future__ import annotations

from src.arbitrage.common.utils import orbitexch_odds_to_probability
from src.arbitrage.common.utils import polymarket_price_to_probability
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


_VALID_ROLES = ("home", "draw", "away")


class MeanRebateCheck(Check):
    """平均返水检查。"""

    def __init__(self, min_rate: float = 0.01, share: float | None = None) -> None:
        self._min_rate = float(min_rate)
        self._share = float(share) if share is not None else None

    def passes(self, ctx: EvalContext) -> bool:
        snap = ctx.snapshot
        if snap is None:
            return False

        # 经 snapshot.instrument_info 给每 leg 打 venue+role 标签(decouple from cache)
        legs_by_role: dict[str, list] = {}
        for iid in snap.instrument_ids:
            book = snap.order_books.get(iid)
            if book is None:
                continue
            info = snap.instrument_info.get(iid) or {}
            role = info.get("selection_role")
            if role not in _VALID_ROLES:
                continue
            venue = _venue_of(iid)
            best_ask = _best_ask(book)
            if best_ask is None or best_ask <= 0:
                continue
            prob = _to_prob(venue, best_ask)
            if prob <= 0:
                continue
            legs_by_role.setdefault(role, []).append({
                "instrument_id": iid,
                "venue": venue,
                "side": "BUY",
                "price": best_ask,
                "prob": prob,
                "role": role,
            })

        # 必须 home + away(2-way)或 home + draw + away(3-way);每方向至少 2 条可比腿。
        roles_present = sorted(legs_by_role.keys())
        if not (roles_present == ["away", "home"] or roles_present == ["away", "draw", "home"]):
            return False
        for role in roles_present:
            if len(legs_by_role[role]) < 2:
                return False  # 缺一边 → 算不了 mean_rebate

        # 每方向取 min(prob);相同 prob 时 PM 优先(arbitrary 但稳定)
        chosen_legs = []
        total_prob = 0.0
        for role in roles_present:
            cands = legs_by_role[role]
            best = min(cands, key=lambda lg: (lg["prob"], 0 if lg["venue"] == "POLYMARKET" else 1))
            chosen_legs.append(best)
            total_prob += best["prob"]

        mean_rebate_rate = 1.0 - total_prob
        if mean_rebate_rate < self._min_rate:
            return False

        share = self._configured_share(ctx)
        if share <= 0:
            return False

        for leg in chosen_legs:
            leg["share_if_wins"] = share

        ctx.scratch["legs"] = chosen_legs
        ctx.scratch["mean_rebate_rate"] = mean_rebate_rate
        return True

    def _configured_share(self, ctx: EvalContext) -> float:
        if self._share is not None:
            return self._share
        return float((ctx.strategy_defaults or {}).get("share") or 0.0)


# ─── 辅助 ──────────────────────────────────────────────────────────


def _venue_of(instrument_id) -> str:
    """从 InstrumentId 后缀提 venue 名(`X.POLYMARKET` / `Y.ORBITEXCH` / `Z.SHARPEXCH`)。"""
    s = str(instrument_id)
    if s.endswith(".POLYMARKET"):
        return "POLYMARKET"
    if s.endswith(".ORBITEXCH"):
        return "ORBITEXCH"
    if s.endswith(".SHARPEXCH"):
        return "SHARPEXCH"
    return ""


def _best_ask(book) -> float | None:
    """从 NT OrderBook / 兼容对象取 best_ask 价。"""
    # NT OrderBook 接口:best_ask_price()(返 Price 对象)/ best_ask()(返 BookOrder)
    fn = getattr(book, "best_ask_price", None)
    if callable(fn):
        try:
            v = fn()
            return float(v) if v is not None else None
        except Exception:
            return None
    # fallback:test fake 用 dict {"ask": x}
    if isinstance(book, dict):
        return book.get("ask") or book.get("best_ask")
    return None


def _to_prob(venue: str, price: float) -> float:
    if venue == "POLYMARKET":
        return polymarket_price_to_probability(price)
    if _is_decimal_odds_venue(venue):
        return orbitexch_odds_to_probability(price)
    return 0.0


def _is_decimal_odds_venue(venue: str) -> bool:
    """第一阶段 SE 接入:SHARPEXCH 先复用 OE decimal odds 语义。"""
    return str(venue).upper() in {"ORBITEXCH", "SHARPEXCH"}
