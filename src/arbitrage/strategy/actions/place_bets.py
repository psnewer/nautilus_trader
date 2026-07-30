"""
PlaceBetsAction —— 通用下单(slice 9 / #49,Q-D1=A log-only smoke)。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check/Condition 算好的完整计划腿),经 Venue Registry 按 odds model 算 size:
  - `ctx.scratch["cancel_pair_orders"]` 存在时不构造订单，调用 EvalContext 的 NT pair 撤单回调后返回
  - probability venue: size = share(1 share = $1 win)
  - decimal odds venue: size = share / price(stake = share / odds,确保 win = share)
  - decimal odds 合成 no 腿(`exec_instrument_id` 重定向):转为 SELL/LAY,按 lay 价重算 size
  - probability BUY 可用同 pair 互斥 LONG 仓位拆成 SELL 减仓 + BUY 剩余量
  - 若 leg 已带 `qty`,优先使用该值;否则从 leg 的 `share_if_wins` 推 qty
  - intent 默认 `"arbitrage"`;补救树可配置 `"recovery"`,经 submitter 写入 order tags 供 Risk 判定。
    #277 起优先读 `ctx.scratch["selected_candidate"]["intent"]`(recovery 候选经 arb 链胜出时不丢豁免)。
  - enable_timeout=false 时经 opportunity metadata 通知 Execution session 在 ACK 后结束追踪。
  - leg→side/price/qty 基础解析与 instrument constraints 读取在 `strategy/leg_plan.py`,
    与 `CandiSelectAction` 最小下注门控共用一份,防止门控与提交漂移。
"""

from __future__ import annotations

import asyncio
import logging
import math

from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.common.venues import is_probability_odds_venue
from src.arbitrage.common.venues import order_required_balance
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
        spread: float | None = None,
        enable_timeout: bool | None = None,
    ) -> None:
        if enable_timeout is not None and not isinstance(enable_timeout, bool):
            raise ValueError("enable_timeout must be a boolean")
        self._price_overrides = _normalize_venue_overrides(price_overrides)
        self._qty_overrides = _normalize_venue_overrides(qty_overrides)
        self._intent = str(intent)
        self._spread = _normalize_spread(spread)
        self._enable_timeout = enable_timeout

    async def execute(self, ctx: EvalContext) -> None:
        if _execute_cancel_request(ctx):
            return

        legs = ctx.scratch.get("legs", [])
        if not legs:
            _LOG.debug(f"PlaceBets: pair={ctx.pair_id} no legs (Check 未写),skip")
            return

        # #277:胜出 candidate 自带 intent(recovery 候选经 arb 链胜出时必须以 recovery 提交,
        # 保住 Risk 的 profit-gates 豁免);无标记时用本 Action 配置值。
        selected = ctx.scratch.get("selected_candidate") or {}
        mean_rate = ctx.scratch.get("mean_rebate_rate")
        strategy = str(selected.get("strategy") or ("mean_rebate" if mean_rate is not None else "unknown"))
        rate = selected.get("rate", mean_rate)
        intent = str(selected.get("intent") or self._intent)
        submitter = ctx.submitter      # slice 10a:None → log-only fallback;否则真出单
        mode = "submit" if submitter is not None else "smoke"
        _LOG.info(
            f"PlaceBets[{mode}]: pair={ctx.pair_id} legs={len(legs)} "
            f"strategy={strategy} rate={rate}",
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
            # qty 按候选**最优价** `price` 计算(口径一致:share/payout 按最优赔率)。
            size = _compute_leg_size(leg, venue, price, self._qty_overrides)
            if size is None:
                _LOG.warning(
                    f"PlaceBets: pair={ctx.pair_id} leg={leg.get('instrument_id')} "
                    "missing qty/share_if_wins, abort opportunity",
                )
                return
            _LOG.info(
                f"PlaceBets leg: pair={ctx.pair_id} venue={str(venue).upper()} "
                f"role={leg.get('claim') or leg.get('role')} "
                f"price={price} qty={size:.4f}",
            )
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

        _apply_spread_to_drafts(drafts, self._spread, ctx)

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
                "positions_digest": ctx.positions_digest,
                "venue_required_balance": required_by_venue[draft["venue"]],
            }
            if self._enable_timeout is not None:
                spec["enable_timeout"] = self._enable_timeout
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


def _execute_cancel_request(ctx: EvalContext) -> bool:
    selected = ctx.scratch.get("selected_candidate") or {}
    request = selected.get("cancel_pair_orders") or ctx.scratch.get("cancel_pair_orders")
    if not request:
        return False
    canceler = ctx.pair_order_canceler
    if canceler is None:
        _LOG.info(
            f"PlaceBets[cancel-smoke]: pair={ctx.pair_id} "
            f"reason={request.get('reason')} orders=0",
        )
        return True
    count = canceler(ctx.pair_id)
    _LOG.info(
        f"PlaceBets[cancel]: pair={ctx.pair_id} "
        f"reason={request.get('reason')} orders={count}",
    )
    return True


def _normalize_venue_overrides(raw: dict[str, float] | None) -> dict[str, float]:
    """配置里的 venue key 统一转大写,便于临时 live 验证覆盖。"""
    if not raw:
        return {}
    return {str(k).upper(): float(v) for k, v in raw.items()}


def _normalize_spread(value: float | None) -> float:
    if value is None:
        return 0.0
    spread = float(value)
    if not math.isfinite(spread) or not 0 <= spread < 1:
        raise ValueError(f"spread must be a finite probability in [0, 1), got {value!r}")
    return spread


def _apply_spread_to_drafts(drafts: list[dict], spread: float, ctx: EvalContext) -> None:
    if spread <= 0:
        return
    for draft in drafts:
        original = float(draft["price"])
        draft["price"] = _price_with_spread(draft, spread, ctx.cache)
        _LOG.info(
            f"PlaceBets spread: pair={ctx.pair_id} venue={draft['venue']} "
            f"side={draft['side']} original_price={original} "
            f"limit_price={draft['price']} spread={spread}",
        )


def _price_with_spread(draft: dict, spread: float, cache) -> float:
    side = str(draft["side"]).upper()
    price = float(draft["price"])
    adjusted = price - spread if side == "BUY" else price + spread
    minimum, maximum = _limit_price_bounds(
        cache,
        draft["instrument_id"],
        draft["venue"],
    )
    return min(max(adjusted, minimum), maximum)


def _limit_price_bounds(cache, instrument_id, venue: str) -> tuple[float, float]:
    instrument = cache.instrument(_as_instrument_id(instrument_id)) if cache is not None else None
    minimum = _numeric_value(getattr(instrument, "min_price", None))
    maximum = _numeric_value(getattr(instrument, "max_price", None))

    if is_probability_odds_venue(venue):
        increment = _numeric_value(getattr(instrument, "price_increment", None)) or 0.01
        lower = minimum if minimum is not None else increment
        upper = maximum if maximum is not None else 1.0 - increment
        return lower, upper

    return minimum if minimum is not None else 1.01, maximum if maximum is not None else 1000.0


def _numeric_value(value) -> float | None:
    if value is None:
        return None
    for method in ("as_double", "as_decimal"):
        converter = getattr(value, method, None)
        if callable(converter):
            return float(converter())
    return float(value)


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
