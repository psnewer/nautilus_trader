"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check 算好,如 MeanRebateCheck),按 venue 算 size:
  - POLYMARKET: size = share(PM 单位是 shares,1 share = $1 win)
  - ORBITEXCH: size = share / price(stake = share / odds,确保 win = share)
  - 若 leg 已带 `qty`,优先使用该值(给 compensation/recovery Check 写入补缺口订单量)
  - intent 默认 `"arbitrage"`;补救树可配置 `"recovery"`,经 submitter 写入 order tags 供 Risk 判定。
"""

from __future__ import annotations

import logging

from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class PlaceBetsAction(Action):
    """通用下单 Action。"""

    def __init__(
        self,
        share: float = 22.5,
        price_overrides: dict[str, float] | None = None,
        qty_overrides: dict[str, float] | None = None,
        intent: str = "arbitrage",
    ) -> None:
        self._share = float(share)
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
            f"PlaceBets[{mode}]: pair={ctx.pair_id} legs={len(legs)} share={self._share} "
            f"mean_rebate_rate={rate}",
        )
        for leg in legs:
            venue = leg["venue"]
            price = self._price_overrides.get(venue, leg["price"])
            size = _compute_leg_size(leg, venue, self._share, price, self._qty_overrides)
            spec = {
                "instrument_id": leg["instrument_id"],
                "side": leg["side"],
                "qty": size,
                "price": price,
                "intent": self._intent,
            }
            if submitter is not None:
                # slice 10a(#50):真出单(SkipExecutionClient 在 debug.skip_execution=true 下兜底 mock 全成)
                await submitter(spec)
            else:
                # log-only fallback(无 submitter 注入;单测 / smoke)
                _LOG.info(
                    f"  would submit: instrument={leg['instrument_id']} side={leg['side']} "
                    f"role={leg['role']} venue={venue} qty={size:.4f} price={price}",
                )


def _compute_size(venue: str, share: float, price: float) -> float:
    """PM=share;OE=share/price(stake)。Mean rebate 数学:确保 win 一致。"""
    if venue == "POLYMARKET":
        return share
    if venue == "ORBITEXCH":
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
    """按优先级决定最终 qty:显式 override > leg 自带 qty > share 公式。"""
    if venue in qty_overrides:
        return qty_overrides[venue]
    if "qty" in leg:
        return float(leg["qty"])
    return _compute_size(venue, share, price)


def _normalize_venue_overrides(raw: dict[str, float] | None) -> dict[str, float]:
    """配置里的 venue key 统一转大写,便于临时 live 验证覆盖。"""
    if not raw:
        return {}
    return {str(k).upper(): float(v) for k, v in raw.items()}
