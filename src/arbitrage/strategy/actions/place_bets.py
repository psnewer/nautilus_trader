"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check/Condition 算好),按 venue 算 size:
  - POLYMARKET: size = share(PM 单位是 shares,1 share = $1 win)
  - ORBITEXCH/SHARPEXCH: size = share / price(stake = share / odds,确保 win = share)
  - 若 leg 已带 `qty`,优先使用该值;否则从 leg 的 `share_if_wins` 推 qty
  - intent 默认 `"arbitrage"`;补救树可配置 `"recovery"`,经 submitter 写入 order tags 供 Risk 判定。
"""

from __future__ import annotations

import asyncio
import logging

from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.strategy.checks.mean_rebate import _is_decimal_odds_venue
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class PlaceBetsAction(Action):
    """通用下单 Action。"""

    def __init__(
        self,
        share: float | None = None,
        price_overrides: dict[str, float] | None = None,
        qty_overrides: dict[str, float] | None = None,
        intent: str = "arbitrage",
    ) -> None:
        self._share = float(share) if share is not None else None
        self._price_overrides = _normalize_venue_overrides(price_overrides)
        self._qty_overrides = _normalize_venue_overrides(qty_overrides)
        self._intent = str(intent)

    async def execute(self, ctx: EvalContext) -> None:
        legs = ctx.scratch.get("legs", [])
        if not legs:
            _LOG.debug(f"PlaceBets: pair={ctx.pair_id} no legs (Check 未写),skip")
            return

        rate = ctx.scratch.get("mean_rebate_rate")
        submitter = ctx.submitter      # slice 10a:None → log-only fallback;否则真出单
        mode = "submit" if submitter is not None else "smoke"
        _LOG.info(
            f"PlaceBets[{mode}]: pair={ctx.pair_id} legs={len(legs)} "
            f"mean_rebate_rate={rate}",
        )
        expected_legs = tuple(_leg_key(leg, idx) for idx, leg in enumerate(legs))
        opportunity_id = new_opportunity_id()
        prepared = []
        for idx, leg in enumerate(legs):
            venue = leg["venue"]
            price = self._price_overrides.get(venue, leg["price"])
            size = _compute_leg_size(leg, venue, self._configured_share(ctx), price, self._qty_overrides)
            spec = {
                "instrument_id": leg["instrument_id"],
                "side": leg["side"],
                "qty": size,
                "price": price,
                "intent": self._intent,
                "opportunity_id": opportunity_id,
                "pair_id": ctx.pair_id,
                "leg_key": expected_legs[idx],
                "expected_legs": expected_legs,
            }
            prepared.append((leg, venue, size, price, spec))

        if submitter is not None:
            # #105:多腿**并发**提交(顺序 workaround 退役)。同页并发 placeBets 丢回执的风险由
            # OE/SE ExecClient 页锁串行碰页操作兜底;PM 与外部腿并行 → 对冲窗口更窄(synchronization §8.3)。
            # slice 10a(#50):SkipExecutionClient 在 debug.skip_execution=true 下兜底 mock 全成。
            await asyncio.gather(*(submitter(spec) for (_, _, _, _, spec) in prepared))
        else:
            # log-only fallback(无 submitter 注入;单测 / smoke)
            for leg, venue, size, price, _spec in prepared:
                _LOG.info(
                    f"  would submit: instrument={leg['instrument_id']} side={leg['side']} "
                    f"role={leg['role']} venue={venue} qty={size:.4f} price={price}",
                )

    def _configured_share(self, ctx: EvalContext) -> float:
        if self._share is not None:
            return self._share
        return float((ctx.strategy_defaults or {}).get("share") or 0.0)


def _compute_size(venue: str, share: float, price: float) -> float:
    """PM=share;OE/SE=share/price(stake)。Mean rebate 数学:确保 win 一致。"""
    if venue == "POLYMARKET":
        return share
    if _is_decimal_odds_venue(venue):
        if price <= 0:
            return 0.0
        return share / price
    return 0.0


def _compute_leg_size(
    leg: dict,
    venue: str,
    share: float,
    price: float,
    qty_overrides: dict[str, float],
) -> float:
    """按优先级决定最终 qty:显式 override > leg qty > leg share_if_wins > fallback share。"""
    if venue in qty_overrides:
        return qty_overrides[venue]
    if "qty" in leg:
        return float(leg["qty"])
    leg_share = leg.get("share_if_wins")
    if leg_share is not None:
        return _compute_size(venue, float(leg_share), price)
    return _compute_size(venue, share, price)


def _normalize_venue_overrides(raw: dict[str, float] | None) -> dict[str, float]:
    """配置里的 venue key 统一转大写,便于临时 live 验证覆盖。"""
    if not raw:
        return {}
    return {str(k).upper(): float(v) for k, v in raw.items()}


def _leg_key(leg: dict, idx: int) -> str:
    role = str(leg.get("role") or idx)
    venue = str(leg.get("venue") or "venue").lower()
    return f"{venue}:{role}:{idx}"
