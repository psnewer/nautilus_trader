"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check/Condition 算好的完整计划腿),经 Venue Registry 按 odds model 算 size:
  - probability venue: size = share(1 share = $1 win)
  - decimal odds venue: size = share / price(stake = share / odds,确保 win = share)
  - decimal odds 合成 no 腿(`exec_instrument_id` 重定向):转为 SELL/LAY,按 lay 价重算 size
  - probability BUY 可用同 pair 互斥 LONG 仓位拆成 SELL 减仓 + BUY 剩余量
  - 若 leg 已带 `qty`,优先使用该值;否则从 leg 的 `share_if_wins` 推 qty
  - intent 默认 `"arbitrage"`;补救树可配置 `"recovery"`,经 submitter 写入 order tags 供 Risk 判定。
    #277 起优先读 `ctx.scratch["selected_candidate"]["intent"]`(recovery 候选经 arb 链胜出时不丢豁免)。
  - leg→side/price/qty 基础解析与 instrument constraints 读取在 `strategy/leg_plan.py`,
    与 `CandiSelectAction` 最小下注门控共用一份,防止门控与提交漂移。
"""

from __future__ import annotations

import asyncio
import logging
import math

from src.arbitrage.bootstrap import get_arb_context
from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.common.venues import is_decimal_odds_venue
from src.arbitrage.common.venues import is_probability_odds_venue
from src.arbitrage.common.venues import order_required_balance
from src.arbitrage.strategy.checks.quote_legs import to_price
from src.arbitrage.strategy.checks.quote_legs import worst_ask
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.leg_plan import as_instrument_id as _as_instrument_id
from src.arbitrage.strategy.leg_plan import compute_leg_size as _compute_leg_size
from src.arbitrage.strategy.leg_plan import instrument_constraints
from src.arbitrage.strategy.leg_plan import resolve_side_and_price as _resolve_side_and_price


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
        # #277:胜出 candidate 自带 intent(recovery 候选经 arb 链胜出时必须以 recovery 提交,
        # 保住 Risk 的 profit-gates 豁免);无标记时用本 Action 配置值。
        selected = ctx.scratch.get("selected_candidate") or {}
        intent = str(selected.get("intent") or self._intent)
        submitter = ctx.submitter      # slice 10a:None → log-only fallback;否则真出单
        mode = "submit" if submitter is not None else "smoke"
        _LOG.info(
            f"PlaceBets[{mode}]: pair={ctx.pair_id} legs={len(legs)} "
            f"mean_rebate_rate={rate}",
        )
        opportunity_id = new_opportunity_id()
        drafts = []
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
            price = _apply_market_order_override(leg, venue, price, ctx, self._price_overrides)
            size = _compute_leg_size(leg, venue, price, self._qty_overrides)
            if size is None:
                _LOG.warning(
                    f"PlaceBets: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "missing qty/share_if_wins, abort opportunity",
                )
                return
            draft = {
                "instrument_id": leg.get("exec_instrument_id") or leg["instrument_id"],
                "side": side,
                "qty": size,
                "price": price,
                "venue": str(venue).upper(),
                "role": str(leg.get("claim") or leg.get("role") or idx),
                "source_index": idx,
            }
            drafts.extend(_expand_probability_inventory(draft, leg, ctx))

        expected_legs = tuple(_draft_leg_key(draft) for draft in drafts)
        required_by_venue = _required_balance_by_venue(drafts)
        prepared = []
        for draft, leg_key in zip(drafts, expected_legs, strict=True):
            spec = {
                "instrument_id": draft["instrument_id"],
                "side": draft["side"],
                "qty": draft["qty"],
                "price": draft["price"],
                "intent": intent,
                "opportunity_id": opportunity_id,
                "pair_id": ctx.pair_id,
                "leg_key": leg_key,
                "expected_legs": expected_legs,
                "open_orders_digest": ctx.open_orders_digest,
                "venue_required_balance": required_by_venue[draft["venue"]],
            }
            prepared.append((draft, spec))

        if submitter is not None:
            # #105:多腿**并发**提交(顺序 workaround 退役)。同页并发 placeBets 丢回执的风险由
            # OE/SE ExecClient 页锁串行碰页操作兜底;PM 与外部腿并行 → 对冲窗口更窄(synchronization §8.3)。
            # slice 10a(#50):SkipExecutionClient 在 debug.skip_execution=true 下兜底 mock 全成。
            await asyncio.gather(*(submitter(spec) for (_, spec) in prepared))
        else:
            # log-only fallback(无 submitter 注入;单测 / smoke)
            for draft, spec in prepared:
                _LOG.info(
                    f"  would submit: instrument={spec['instrument_id']} side={spec['side']} "
                    f"role={draft['role']} venue={draft['venue']} qty={spec['qty']:.4f} "
                    f"price={spec['price']}",
                )


def _apply_market_order_override(
    leg: dict,
    venue: str,
    price: float,
    ctx,
    price_overrides: dict[str, float],
) -> float:
    """decimal venue(OE/SE)市价单开关(#256 续):打开时用书内最差价替代限价,保证成交
    而非最优价。显式 `price_overrides`(调试/临时覆盖)优先于市价单,不被此处替换;
    缺深度(查不到 worst price)时退回原限价,不报错、不阻断下单。

    读取 `leg["instrument_id"]`(quote_legs 定价所用的原始 instrument,3-way 合成 no 腿的
    `exec_instrument_id` 重定向只影响提交目标,不影响这里的定价来源)对应的 book,取
    `worst_ask` 的隐含概率,用与 `quote_legs_by_outcome` 相同的 `quote_claim` 换算回真实价格。
    """
    venue_key = str(venue).upper()
    if venue_key in price_overrides:
        return price
    try:
        if not is_decimal_odds_venue(venue):
            return price
    except KeyError:
        return price
    if not get_arb_context().market_order_enabled:
        return price
    if ctx.cache is None:
        return price
    instrument_id = leg.get("instrument_id")
    iid = _as_instrument_id(instrument_id)
    book = ctx.cache.order_book(iid)
    if book is None:
        return price
    worst_probability = worst_ask(book)
    if worst_probability is None or worst_probability <= 0:
        return price
    instrument = ctx.cache.instrument(iid)
    info = getattr(instrument, "info", None) or {}
    quote_claim = str(info.get("quote_claim") or "yes").lower()
    worst_price = to_price(venue, worst_probability, quote_claim)
    if worst_price is None or worst_price <= 0:
        return price
    return worst_price


def _normalize_venue_overrides(raw: dict[str, float] | None) -> dict[str, float]:
    """配置里的 venue key 统一转大写,便于临时 live 验证覆盖。"""
    if not raw:
        return {}
    return {str(k).upper(): float(v) for k, v in raw.items()}


def _draft_leg_key(draft: dict) -> str:
    base = f"{draft['venue'].lower()}:{draft['role']}:{draft['source_index']}"
    suffix = draft.get("key_suffix")
    return f"{base}:{suffix}" if suffix else base


def _is_non_tradable_leg(leg: dict) -> bool:
    return leg.get("tradable") is False or leg.get("anchor") is True


def _expand_probability_inventory(draft: dict, leg: dict, ctx) -> list[dict]:
    """把 probability BUY 优先转换成“卖互斥仓位 + 买剩余量”。

    在 Action 执行时读取 live Cache；之后挂单变化由 Execution barrier 基线比较收口。
    """
    if ctx.cache is None or ctx.pair_registry is None or draft["side"] != "BUY":
        return [draft]
    try:
        if not is_probability_odds_venue(draft["venue"]):
            return [draft]
    except KeyError:
        return [draft]

    target_qty = float(draft["qty"])
    target_price = float(draft["price"])
    if target_qty <= 0 or not 0 < target_price < 1:
        return [draft]

    opposite_iid = _opposite_real_instrument(ctx, leg, draft)
    if opposite_iid is None:
        return [draft]
    available = _long_position_quantity(ctx.cache, opposite_iid)
    if available <= 0:
        return [draft]

    opposite_constraints = instrument_constraints(ctx.cache, opposite_iid)
    target_constraints = instrument_constraints(ctx.cache, draft["instrument_id"])
    sell_min = _effective_minimum_quantity(
        opposite_constraints,
        price=1.0 - target_price,
        side="SELL",
    )
    buy_min = _effective_minimum_quantity(
        target_constraints,
        price=target_price,
        side="BUY",
    )
    sell_qty = _reduction_quantity(target_qty, available, sell_min, buy_min)
    if sell_qty <= 0:
        return [draft]

    sell = dict(draft)
    sell.update({
        "instrument_id": str(opposite_iid),
        "side": "SELL",
        "qty": sell_qty,
        "price": 1.0 - target_price,
        "key_suffix": "reduce",
    })
    remainder = target_qty - sell_qty
    if remainder <= 1e-9:
        return [sell]

    buy = dict(draft)
    buy.update({"qty": remainder, "key_suffix": "buy"})
    return [sell, buy]


def _opposite_real_instrument(ctx, leg: dict, draft: dict) -> str | None:
    source = ctx.cache.instrument(_as_instrument_id(leg.get("instrument_id")))
    source_info = getattr(source, "info", None) or {}
    outcome = str(
        leg.get("claim")
        or source_info.get("claim")
        or leg.get("role")
        or source_info.get("selection_role")
        or ""
    ).lower()
    outcomes = ("yes", "no")
    if len(outcomes) != 2 or outcome not in outcomes:
        return None
    opposite = next(value for value in outcomes if value != outcome)
    venue = draft["venue"]
    for iid in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id):
        iid_text = str(iid)
        if not iid_text.upper().endswith(f".{venue}"):
            continue
        instrument = ctx.cache.instrument(_as_instrument_id(iid_text))
        info = getattr(instrument, "info", None) or {}
        candidate = str(info.get("claim") or info.get("selection_role") or "").lower()
        if candidate != opposite or info.get("exec_instrument_id"):
            continue
        return iid_text
    return None


def _long_position_quantity(cache, instrument_id: str) -> float:
    total = 0.0
    for position in cache.positions_open(instrument_id=_as_instrument_id(instrument_id)) or []:
        side = str(getattr(getattr(position, "side", None), "name", getattr(position, "side", ""))).upper()
        if side and side != "LONG":
            continue
        quantity = getattr(position, "quantity", None)
        value = quantity.as_double() if hasattr(quantity, "as_double") else quantity
        try:
            total += abs(float(value))
        except (TypeError, ValueError):
            continue
    return total


def _minimum_quantity(constraint: dict) -> float:
    try:
        return max(0.0, float(constraint.get("min_quantity") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _effective_minimum_quantity(constraint: dict, *, price: float, side: str) -> float:
    """返回实际子单的最小 quantity；PM BUY 额外满足 quote notional 下限。"""
    minimum = _minimum_quantity(constraint)
    if side != "BUY" or price <= 0:
        return minimum
    try:
        min_buy_notional = max(0.0, float(constraint.get("min_buy_notional") or 0.0))
    except (TypeError, ValueError):
        min_buy_notional = 0.0
    if min_buy_notional <= 0:
        return minimum
    raw = min_buy_notional / price
    try:
        increment = max(0.0, float(constraint.get("size_increment") or 0.0))
    except (TypeError, ValueError):
        increment = 0.0
    notional_minimum = math.ceil((raw / increment) - 1e-12) * increment if increment > 0 else raw
    return max(minimum, notional_minimum)


def _reduction_quantity(target: float, available: float, sell_min: float, buy_min: float) -> float:
    """在两个子单都合法的前提下最大化减仓量；无法合法拆分时返回 0。"""
    max_sell = min(target, available)
    if max_sell + 1e-9 < sell_min:
        return 0.0
    if max_sell + 1e-9 >= target:
        return target
    if target - max_sell + 1e-9 >= buy_min:
        return max_sell
    adjusted = target - buy_min
    if adjusted + 1e-9 >= sell_min and adjusted <= available + 1e-9:
        return adjusted
    return 0.0


def _required_balance_by_venue(drafts: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for draft in drafts:
        venue = draft["venue"]
        qty = float(draft["qty"])
        price = float(draft["price"])
        required = order_required_balance(venue, qty, price, draft["side"])
        totals[venue] = totals.get(venue, 0.0) + required
    return totals
