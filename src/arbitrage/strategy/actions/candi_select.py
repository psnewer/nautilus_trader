"""
CandiSelectAction —— 门控 + 树间取舍 + 组内选择(#277)。

#277 起所有链(arb + comp)都在 `place_bets` 之前插入本 Action,内部三步:
1. **最小下注门控**:候选池 = 本树 `candidates`(缺失时把 `legs` 包成单 candidate)
   + evaluator 注入的 `recovery_candidates`;逐腿按与 place_bets 共用的 `leg_plan`
   解析 side/price/qty,对实际提交 instrument(`exec_instrument_id` 优先)检查
   `min_quantity` / `min_notional`(经 `notional_value`,与 NT 原生口径一致)/
   BUY `min_buy_notional`;任一腿不过整 candidate 淘汰。
2. **套利优先分组**:本树组有幸存者就只在本组选,全灭才落 recovery 组;
   recovery 永不参与本树组的 share 比较。
3. **组内选择(逻辑不变)**:legs 内最大 `share_if_wins` 最高者胜出,写
   `ctx.scratch["selected_candidate"]`(含 intent 标记)和 `ctx.scratch["legs"]`。

Risk 侧限额检查保留为兜底:market-order 最差价覆盖与 PM 减仓拆单发生在门控之后。
"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.leg_plan import as_instrument_id
from src.arbitrage.strategy.leg_plan import compute_leg_size
from src.arbitrage.strategy.leg_plan import object_value
from src.arbitrage.strategy.leg_plan import resolve_side_and_price


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


class CandiSelectAction(Action):
    """最小下注门控 → 套利优先分组 → 组内 max leg share 选择。"""

    async def execute(self, ctx: EvalContext) -> None:
        primary = _primary_pool(ctx)
        recovery = list(ctx.scratch.get("recovery_candidates") or [])
        if primary is None and not recovery:
            return

        survivors = _gate_pool(ctx, primary or [], group="primary")
        group = "primary"
        if not survivors and recovery:
            survivors = _gate_pool(ctx, recovery, group="recovery")
            group = "recovery"
            if survivors:
                _LOG.info(
                    f"CandiSelect: pair={ctx.pair_id} primary group exhausted, "
                    f"fallback to recovery ({len(survivors)} candidate)"
                )

        if not survivors:
            ctx.scratch["legs"] = []
            _LOG.info(f"CandiSelect: pair={ctx.pair_id} no candidates")
            return

        selected = max(survivors, key=_candidate_score)
        ctx.scratch["selected_candidate"] = selected
        ctx.scratch["legs"] = selected.get("legs", [])

        _LOG.info(
            f"CandiSelect: pair={ctx.pair_id} group={group} candidates={len(survivors)} "
            f"selected={selected.get('candidate_id', selected.get('candidate_index'))} "
            f"max_candidate_share={_candidate_score(selected):.4f}"
        )


def _primary_pool(ctx: EvalContext) -> list[dict] | None:
    """本树候选:candidates 优先;legs-only 的 Check(mean_rebate / recovery)包成单 candidate。"""
    if "candidates" in ctx.scratch:
        return list(ctx.scratch.get("candidates") or [])
    legs = ctx.scratch.get("legs")
    if legs:
        return [{"candidate_id": "legs", "legs": legs}]
    return None


def _gate_pool(ctx: EvalContext, pool: list[dict], group: str) -> list[dict]:
    survivors = []
    for idx, candidate in enumerate(pool):
        reason = _min_bet_violation(ctx, candidate)
        if reason is None:
            survivors.append(candidate)
            continue
        cid = candidate.get("candidate_id", candidate.get("candidate_index", idx))
        # 低于限额是常态(share_limit 缩量后每个 OBD tick 可能重复命中)→ DEBUG 留痕;
        # 字段/instrument 解析异常在 _min_bet_violation 内已打 WARNING。
        _LOG.debug(
            f"CandiSelect[min-bet]: pair={ctx.pair_id} group={group} "
            f"candidate={cid} dropped: {reason}"
        )
    return survivors


def _min_bet_violation(ctx: EvalContext, candidate: dict) -> str | None:
    """返回不通过原因;None = 全腿达标。任一腿不过整 candidate 淘汰(半个套利不是套利)。"""
    legs = candidate.get("legs") or []
    if not legs:
        return "no legs"
    for leg in legs:
        venue = str(leg.get("venue", "")).upper()
        if not venue:
            _LOG.warning(f"CandiSelect[min-bet]: pair={ctx.pair_id} leg missing venue, drop")
            return "leg missing venue"
        side, price = resolve_side_and_price(leg, venue, {})
        if price is None or price <= 0:
            _LOG.warning(
                f"CandiSelect[min-bet]: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                "missing executable price, drop"
            )
            return "missing executable price"
        qty = compute_leg_size(leg, venue, price, {})
        if qty is None or qty <= 0:
            _LOG.warning(
                f"CandiSelect[min-bet]: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                "missing qty/share_if_wins, drop"
            )
            return "missing qty/share_if_wins"

        if ctx.cache is None:
            continue  # 无 cache(单测 harness)跳过 instrument 门,交给 Risk 兜底
        target_iid = leg.get("exec_instrument_id") or leg.get("instrument_id")
        instrument = ctx.cache.instrument(as_instrument_id(target_iid))
        if instrument is None:
            _LOG.warning(
                f"CandiSelect[min-bet]: pair={ctx.pair_id} instrument={target_iid} "
                "not in cache, drop"
            )
            return "instrument not in cache"

        reason = _instrument_min_violation(instrument, side, qty, price)
        if reason is not None:
            return f"leg={target_iid} {reason}"
    return None


def _instrument_min_violation(instrument, side: str, qty: float, price: float) -> str | None:
    """与 NT/Risk 同口径:qty 经 make_qty 规整后比 min_quantity;notional 经 notional_value
    (OE/SE stake 口径由 instrument 自己定义);BUY 另比 info["min_buy_notional"]
    (qty*price,同 Risk `_check_min_buy_notional`)。真 NT instrument 走原生方法;
    缺方法的对象(单测 mock)退回 float 口径。"""
    quantity = qty
    make_qty = getattr(instrument, "make_qty", None)
    if callable(make_qty):
        try:
            quantity = make_qty(qty).as_double()
        except (ValueError, TypeError) as e:
            return f"make_qty failed: {e}"

    min_quantity = object_value(getattr(instrument, "min_quantity", None))
    if min_quantity is not None and quantity + _EPS < min_quantity:
        return f"qty={quantity:.4f} < min_quantity={min_quantity:.4f}"

    min_notional = object_value(getattr(instrument, "min_notional", None))
    if min_notional is not None and min_notional > 0:
        notional = _notional(instrument, quantity, price)
        if notional + _EPS < min_notional:
            return f"notional={notional:.4f} < min_notional={min_notional:.4f}"

    if side == "BUY":
        info = getattr(instrument, "info", None) or {}
        try:
            min_buy = float(info.get("min_buy_notional") or 0.0)
        except (TypeError, ValueError):
            min_buy = 0.0
        if min_buy > 0:
            buy_notional = quantity * price
            if buy_notional + _EPS < min_buy:
                return f"buy_notional={buy_notional:.4f} < min_buy_notional={min_buy:.4f}"
    return None


def _notional(instrument, qty: float, price: float) -> float:
    """NT 原生 `notional_value`(BettingInstrument = qty×multiplier 的 stake 口径)优先;
    mock 退回 qty×price。"""
    fn = getattr(instrument, "notional_value", None)
    make_qty = getattr(instrument, "make_qty", None)
    make_price = getattr(instrument, "make_price", None)
    if callable(fn) and callable(make_qty) and callable(make_price):
        try:
            return float(fn(make_qty(qty), make_price(price)).as_double())
        except (ValueError, TypeError):
            pass
    return qty * price


def _candidate_score(candidate: dict) -> float:
    shares = [
        float(leg["share_if_wins"])
        for leg in candidate.get("legs", [])
        if leg.get("share_if_wins") is not None
    ]
    return max(shares) if shares else 0.0
