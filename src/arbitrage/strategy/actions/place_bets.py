"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check/Condition 算好的完整计划腿),经 Venue Registry 按 odds model 算 size:
  - probability venue: size = share(1 share = $1 win)
  - decimal odds venue: size = share / price(stake = share / odds,确保 win = share)
  - decimal odds venue 若 `claim=no`:最终下单转为 SELL/LAY,price 使用 bid/lay,size 按 lay price 重算
  - 若 leg 已带 `qty`,优先使用该值;否则从 leg 的 `share_if_wins` 推 qty
  - intent 默认 `"arbitrage"`;补救树可配置 `"recovery"`,经 submitter 写入 order tags 供 Risk 判定。
"""

from __future__ import annotations

import asyncio
import logging

from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import qty_from_share
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext


_LOG = logging.getLogger(__name__)


class PlaceBetsAction(Action):
    """通用下单 Action。"""

    def __init__(
        self,
        price_overrides: dict[str, float] | None = None,
        qty_overrides: dict[str, float] | None = None,
        intent: str = "arbitrage",
    ) -> None:
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
            if _is_non_tradable_leg(leg):
                _LOG.warning(
                    f"PlaceBets: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "is non-tradable anchor, abort opportunity",
                )
                return
            venue = leg["venue"]
            side, price = _resolve_side_and_price(leg, venue, self._price_overrides)
            if price is None:
                _LOG.warning(
                    f"PlaceBets: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "missing executable price, abort opportunity",
                )
                return
            size = _compute_leg_size(leg, venue, price, self._qty_overrides)
            if size is None:
                _LOG.warning(
                    f"PlaceBets: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "missing qty/share_if_wins, abort opportunity",
                )
                return
            spec = {
                # #228:合成 no 腿(-NO instrument)只作行情/身份载体,真单落在同 selection 的
                # yes instrument 上(SELL@lay),保证 venue 对账 LAY=SHORT 落在真 selection。
                "instrument_id": leg.get("exec_instrument_id") or leg["instrument_id"],
                "side": side,
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
            for leg, venue, size, price, spec in prepared:
                _LOG.info(
                    f"  would submit: instrument={leg['instrument_id']} side={spec['side']} "
                    f"role={leg['role']} venue={venue} qty={size:.4f} price={price}",
                )


def _compute_size(venue: str, share: float, price: float) -> float:
    """PM=share;OE/SE=share/price(stake)。Mean rebate 数学:确保 win 一致。"""
    if price <= 0:
        return 0.0
    try:
        return qty_from_share(venue, share, price)
    except KeyError:
        return 0.0


def _compute_leg_size(
    leg: dict,
    venue: str,
    price: float,
    qty_overrides: dict[str, float],
) -> float | None:
    """按优先级决定最终 qty:显式 override > leg qty > leg share_if_wins。"""
    if venue in qty_overrides:
        return qty_overrides[venue]
    if _is_decimal_no_claim(leg, venue) and leg.get("share_if_wins") is not None:
        return _compute_size(venue, float(leg["share_if_wins"]), price)
    if "qty" in leg:
        return float(leg["qty"])
    leg_share = leg.get("share_if_wins")
    if leg_share is not None:
        return _compute_size(venue, float(leg_share), price)
    return None


def _resolve_side_and_price(
    leg: dict,
    venue: str,
    price_overrides: dict[str, float],
) -> tuple[str, float | None]:
    """将语义 leg 转成最终 submit side/price。

    decimal venue 的 `claim=no` 表示 lay 该 outcome,因此最终 NT order 为 SELL @ bid/lay。
    其它情况保持现有买入路径。
    """
    venue_key = str(venue).upper()
    if venue_key in price_overrides:
        price = price_overrides[venue_key]
        return _execution_side(leg, venue), price
    if _is_decimal_no_claim(leg, venue):
        return "SELL", _leg_bid_price(leg)
    return str(leg.get("side") or "BUY").upper(), _try_float(leg.get("price"))


def _execution_side(leg: dict, venue: str) -> str:
    if _is_decimal_no_claim(leg, venue):
        return "SELL"
    return str(leg.get("side") or "BUY").upper()


def _is_decimal_no_claim(leg: dict, venue: str) -> bool:
    claim = str(leg.get("claim") or "").strip().lower()
    if claim != "no":
        return False
    try:
        return is_decimal_odds_venue(venue)
    except KeyError:
        return False


def _leg_bid_price(leg: dict) -> float | None:
    for key in ("bid", "best_bid", "lay", "lay_price"):
        value = _try_float(leg.get(key))
        if value is not None and value > 0:
            return value
    return None


def _try_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_venue_overrides(raw: dict[str, float] | None) -> dict[str, float]:
    """配置里的 venue key 统一转大写,便于临时 live 验证覆盖。"""
    if not raw:
        return {}
    return {str(k).upper(): float(v) for k, v in raw.items()}


def _leg_key(leg: dict, idx: int) -> str:
    role = str(leg.get("role") or idx)
    venue = str(leg.get("venue") or "venue").lower()
    return f"{venue}:{role}:{idx}"


def _is_non_tradable_leg(leg: dict) -> bool:
    return leg.get("tradable") is False or leg.get("anchor") is True
