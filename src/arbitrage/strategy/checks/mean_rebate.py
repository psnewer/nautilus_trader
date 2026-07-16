"""
MeanRebateCheck —— 平均返水套利检查(slice 9 / #49;#228 outcome 化)。

算法(对应 requirements §8):
  1. 按 outcome 标签分组(#228:`info.get("claim") or info.get("selection_role")`,
     合法集合 = `snapshot.outcomes` —— 所有 binary pair 均为 `[yes,no]`),
     每方向取所有 venue 中概率最小者(即 best_ask 最便宜方;decimal claim=no 腿的
     概率经 `probability_from_price(venue, price, claim)` 取补集)
  2. mean_rebate_rate = 1 - sum_outcomes(min_prob)
  3. >= `min_rate` 阈值 → True;同时写带 `share_if_wins` 的 `ctx.scratch["legs"]`
     供 Action 消费。`share` 可在本 Check params 中显式配置;未配置则读 Web Arbitrage 默认。

输出 legs 形态(每方向一条):
  {
    "instrument_id": InstrumentId,
    "venue": str,
    "side": "BUY",
    "price": float (原始价 — PM 是 0-1 概率,OE 是 stake odds;no 腿 = lay 原值),
    "prob": float,
    "role": canonical outcome 标签 yes/no,
    "share_if_wins": float,
    # 合成 no 腿另带(place_bets SELL@lay 转换 + 执行重定向):
    "claim": "no", "lay_price": float, "exec_instrument_id": str,
  }

PlaceBetsAction 用 leg 自带 `share_if_wins` 经 Venue Registry 推 qty。
"""

from __future__ import annotations

from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.common.venues import venue_preference_rank
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext


class MeanRebateCheck(Check):
    """平均返水检查。"""

    def __init__(self, min_rate: float = 0.01, share: float | None = None) -> None:
        self._min_rate = float(min_rate)
        self._share = float(share) if share is not None else None

    def passes(self, ctx: EvalContext) -> bool:
        snap = ctx.snapshot
        if snap is None:
            return False

        # 经 snapshot.instrument_info 给每 leg 打 venue+outcome 标签(decouple from cache)
        # #228:分组键 = claim 优先(3-way 腿),fallback selection_role(2-way);合法集 = snap.outcomes。
        valid_outcomes = tuple(getattr(snap, "outcomes", None) or ("home", "away"))
        legs_by_outcome: dict[str, list] = {}
        for iid in snap.instrument_ids:
            book = snap.order_books.get(iid)
            if book is None:
                continue
            info = snap.instrument_info.get(iid) or {}
            claim = str(info.get("claim") or "").lower()
            quote_claim = str(info.get("quote_claim") or "yes").lower()
            outcome = claim or str(info.get("selection_role") or "").lower()
            if outcome not in valid_outcomes:
                continue
            venue = _venue_of(iid)
            best_ask = _best_ask(book)
            if best_ask is None or best_ask <= 0:
                continue
            prob = _to_prob(venue, best_ask, quote_claim)
            if prob <= 0:
                continue
            leg = {
                "instrument_id": iid,
                "venue": venue,
                "side": "BUY",
                "price": best_ask,
                "prob": prob,
                "role": outcome,
            }
            if claim:
                leg["claim"] = claim
            if info.get("exec_instrument_id"):
                # #228:no 腿 price 即 lay 原值;place_bets 经 lay_price 转 SELL,
                # 经 exec_instrument_id 重定向到同 selection 的 yes instrument(如有)。
                leg["lay_price"] = best_ask
                exec_iid = info.get("exec_instrument_id")
                if exec_iid:
                    leg["exec_instrument_id"] = str(exec_iid)
            legs_by_outcome.setdefault(outcome, []).append(leg)

        # 必须 outcome 集合齐(#228:snap.outcomes 声明);每方向至少 2 条可比腿。
        if sorted(legs_by_outcome.keys()) != sorted(valid_outcomes):
            return False
        for outcome in valid_outcomes:
            if len(legs_by_outcome[outcome]) < 2:
                return False  # 缺一边 → 算不了 mean_rebate

        # 每方向取 min(prob);相同 prob 时按 venue capability 稳定排序。
        chosen_legs = []
        total_prob = 0.0
        for outcome in sorted(valid_outcomes):
            cands = legs_by_outcome[outcome]
            best = min(cands, key=lambda lg: (lg["prob"], venue_preference_rank(lg["venue"])))
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
    """从 NT `InstrumentId` 或兼容字符串提真实 venue 名。"""
    return venue_id_from_instrument_id(instrument_id)


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


def _to_prob(venue: str, price: float, claim: str = "yes") -> float:
    try:
        return probability_from_price(venue, price, claim or "yes")
    except KeyError:
        return 0.0
