"""
PlaceBetsAction —— 把树内候选转换为统一执行计划。

Action 通用 — 读 `ctx.scratch["legs"]`(由 Check/Condition 算好的完整计划腿),经 Venue Registry 按 odds model 算 size:
  - `ctx.scratch["cancel_pair_orders"]` 存在时生成 grouped cancel 计划
  - probability venue: size = share(1 share = $1 win)
  - decimal odds venue: size = share / price(stake = share / odds,确保 win = share)
  - decimal odds 合成 no 腿(`exec_instrument_id` 重定向):转为 SELL/LAY,按 lay 价重算 size
  - probability BUY 仅在互斥 LONG 的 SELL 参考价不高于当前 best bid 时，
    拆成 SELL 减仓 + BUY 剩余量；limit=true 可再把最终 SELL 改挂 best ask
  - 若 leg 已带 `qty`,优先使用该值;否则从 leg 的 `share_if_wins` 推 qty
  - intent 默认 `"arbitrage"`；补偿树可配置 `"recovery"`，胜出 candidate 的显式 intent
    优先于 Action 默认值，经 submitter 写入 order tags 供 Risk 判定。
  - enable_timeout=false 时经 opportunity metadata 通知 submit session 在 ACK 后结束追踪；
    cancel session 收到正常撤单响应即结束，不消费该参数。
  - market=true 只写入订单 metadata；计划价保持不变，由 venue adapter 在最终提交边界转换。
  - limit=true 时最终 BUY 使用 live best bid、SELL 使用 live best ask；盘口概率按 quote_claim
    还原为 venue 原生价格。limit 定价优先于 price_overrides / spread。
  - leg→side/price/qty 基础解析与 instrument constraints 读取在 `strategy/leg_plan.py`,
    与 `CandiSelectAction` 最小下注门控共用一份,防止门控与提交漂移。
"""

from __future__ import annotations

import logging
import math

from src.arbitrage.common.opportunity import new_opportunity_id
from src.arbitrage.common.venues import is_probability_odds_venue
from src.arbitrage.common.venues import order_required_balance
from src.arbitrage.common.venues import price_from_probability
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.condition import Action
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.execution_plan import ExecutionPlan
from src.arbitrage.strategy.execution_plan import PreparedOrder
from src.arbitrage.strategy.leg_plan import as_instrument_id as _as_instrument_id
from src.arbitrage.strategy.leg_plan import compute_leg_size as _compute_leg_size
from src.arbitrage.strategy.leg_plan import instrument_constraints
from src.arbitrage.strategy.leg_plan import resolve_side_and_price as _resolve_side_and_price


_LOG = logging.getLogger(__name__)


class PlaceBetsAction(Action):
    """完成订单转换并写入 `scratch["execution_plan"]`，不直接提交或撤单。"""

    def __init__(
        self,
        price_overrides: dict[str, float] | None = None,
        qty_overrides: dict[str, float] | None = None,
        intent: str = "arbitrage",
        spread: float | None = None,
        enable_timeout: bool | None = None,
        market: bool | None = None,
        limit: bool | None = None,
    ) -> None:
        if enable_timeout is not None and not isinstance(enable_timeout, bool):
            raise ValueError("enable_timeout must be a boolean")
        if market is not None and not isinstance(market, bool):
            raise ValueError("market must be a boolean")
        if limit is not None and not isinstance(limit, bool):
            raise ValueError("limit must be a boolean")
        self._price_overrides = _normalize_venue_overrides(price_overrides)
        self._qty_overrides = _normalize_venue_overrides(qty_overrides)
        self._intent = str(intent)
        self._spread = _normalize_spread(spread)
        self._enable_timeout = enable_timeout
        self._market = market
        self._limit = bool(limit)

    async def execute(self, ctx: EvalContext) -> None:
        ctx.scratch.pop("execution_plan", None)
        if self._prepare_cancel_request(ctx):
            return

        legs = ctx.scratch.get("legs", [])
        if not legs:
            _LOG.debug(f"PlaceBets: pair={ctx.pair_id} no legs (Check 未写),skip")
            return

        # 胜出 candidate 可覆盖本树 Action 的 intent；无标记时使用 Action 配置值。
        selected = ctx.scratch.get("selected_candidate") or {}
        mean_rate = ctx.scratch.get("mean_rebate_rate")
        strategy = str(selected.get("strategy") or ("mean_rebate" if mean_rate is not None else "unknown"))
        rate = selected.get("rate", mean_rate)
        intent = str(selected.get("intent") or self._intent)
        _LOG.info(
            f"PlaceBets[prepare]: pair={ctx.pair_id} legs={len(legs)} "
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
            instrument_id = leg.get("exec_instrument_id") or leg["instrument_id"]
            if self._limit:
                price = _book_limit_price(ctx.cache, instrument_id, venue, side)
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
                "instrument_id": instrument_id,
                "side": side,
                "qty": size,
                "price": price,
                "venue": str(venue).upper(),
                "role": str(leg.get("claim") or leg.get("role") or idx),
                "source_index": idx,
            }
            spread = 0.0 if self._limit else self._spread
            drafts.extend(_expand_probability_inventory(draft, leg, ctx, spread))

        if self._limit:
            if not _apply_book_limit_to_drafts(drafts, ctx):
                return
        else:
            _apply_spread_to_drafts(drafts, self._spread, ctx)

        expected_legs = tuple(_draft_leg_key(draft) for draft in drafts)
        required_by_venue = _required_balance_by_venue(drafts)
        prepared: list[PreparedOrder] = []
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
                "positions_digest": ctx.positions_digest,
                "venue_required_balance": required_by_venue[draft["venue"]],
            }
            if draft.get("position_id") is not None:
                spec["position_id"] = draft["position_id"]
            if self._enable_timeout is not None:
                spec["enable_timeout"] = self._enable_timeout
            if self._market is not None:
                spec["market"] = self._market
            prepared.append(
                PreparedOrder(
                    spec=spec,
                    venue=draft["venue"],
                    role=draft["role"],
                ),
            )
        if not prepared:
            return
        ctx.scratch["execution_plan"] = ExecutionPlan.submit(ctx.pair_id, prepared)

    def _prepare_cancel_request(self, ctx: EvalContext) -> bool:
        selected = ctx.scratch.get("selected_candidate") or {}
        request = selected.get("cancel_pair_orders") or ctx.scratch.get("cancel_pair_orders")
        if not request:
            return False
        ctx.scratch["execution_plan"] = ExecutionPlan.cancel_pair(
            ctx.pair_id,
            request.get("reason"),
        )
        _LOG.info(
            f"PlaceBets[prepare-cancel]: pair={ctx.pair_id} "
            f"reason={request.get('reason')}",
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


def _apply_book_limit_to_drafts(drafts: list[dict], ctx: EvalContext) -> bool:
    for draft in drafts:
        price = _book_limit_price(
            ctx.cache,
            draft["instrument_id"],
            draft["venue"],
            draft["side"],
        )
        if price is None:
            _LOG.warning(
                f"PlaceBets: pair={ctx.pair_id} leg={draft['instrument_id']} "
                f"missing limit-side quote for {draft['side']}, abort opportunity",
            )
            return False
        draft["price"] = price
    return True


def _book_limit_price(cache, instrument_id, venue: str, side: str) -> float | None:
    if cache is None:
        return None
    probability = (
        _best_bid_price(cache, instrument_id)
        if str(side).upper() == "BUY"
        else _best_ask_price(cache, instrument_id)
    )
    if probability is None or not math.isfinite(probability) or not 0 < probability < 1:
        return None
    instrument = cache.instrument(_as_instrument_id(instrument_id))
    if instrument is None:
        return None
    info = getattr(instrument, "info", None) or {}
    quote_claim = str(info.get("quote_claim") or "yes").lower()
    try:
        price = price_from_probability(venue, probability, quote_claim)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _price_with_spread(draft: dict, spread: float, cache) -> float:
    side = str(draft["side"]).upper()
    price = float(draft["price"])
    venue = draft["venue"]
    minimum, maximum = _limit_price_bounds(
        cache,
        draft["instrument_id"],
        venue,
    )
    probability = probability_from_price(venue, price, "yes")
    adjusted = probability - spread if side == "BUY" else probability + spread
    probability_bounds = (
        probability_from_price(venue, minimum, "yes"),
        probability_from_price(venue, maximum, "yes"),
    )
    adjusted = min(max(adjusted, min(probability_bounds)), max(probability_bounds))
    adjusted_price = price_from_probability(venue, adjusted, "yes")
    return min(max(adjusted_price, minimum), maximum)


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


def _expand_probability_inventory(draft: dict, leg: dict, ctx, spread: float = 0.0) -> list[dict]:
    """把 probability BUY 优先转换成“卖互斥仓位 + 买剩余量”。

    当前不要求应用 spread 后的 SELL 参考价与 best bid 交叉，因此转换出的 SELL 可能只是挂单；
    limit=true 时最终 SELL 会在拆单后改挂 best ask。暂不按盘口深度限制数量。
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
    long_positions = _long_positions(ctx.cache, opposite_iid)
    available = sum(quantity for _, quantity in long_positions)
    if available <= 0:
        return [draft]

    # 暂时禁用“SELL 限价必须不高于 best bid”的即时成交门，后续可能恢复：
    # sell_limit = 1.0 - target_price
    # sell_probe = dict(draft)
    # sell_probe.update({
    #     "instrument_id": str(opposite_iid),
    #     "side": "SELL",
    #     "price": sell_limit,
    # })
    # if spread > 0:
    #     sell_limit = _price_with_spread(sell_probe, spread, ctx.cache)
    # best_bid = _best_bid_price(ctx.cache, opposite_iid)
    # if best_bid is None or sell_limit > best_bid + 1e-9:
    #     _LOG.debug(
    #         f"PlaceBets: pair={ctx.pair_id} skip PM inventory SELL "
    #         f"instrument={opposite_iid} limit={sell_limit} best_bid={best_bid}",
    #     )
    #     return [draft]

    target_constraints = instrument_constraints(ctx.cache, draft["instrument_id"])
    # PM 库存 SELL 是减仓，不应用 BUY-only 的最小 share/金额门控。
    sell_min = 0.0
    buy_min = _effective_minimum_quantity(
        target_constraints,
        price=target_price,
        side="BUY",
    )
    sell_qty = _reduction_quantity(target_qty, available, sell_min, buy_min)
    if sell_qty <= 0:
        return [draft]

    reductions = _allocate_position_reductions(
        target_qty=target_qty,
        requested_sell_qty=sell_qty,
        long_positions=long_positions,
        sell_min=sell_min,
        buy_min=buy_min,
    )
    if not reductions:
        return [draft]

    sells = []
    multiple = len(reductions) > 1
    for index, (position_id, quantity) in enumerate(reductions):
        sell = dict(draft)
        sell.update({
            "instrument_id": str(opposite_iid),
            "side": "SELL",
            "qty": quantity,
            "price": 1.0 - target_price,
            "position_id": position_id,
            "key_suffix": f"reduce:{index}" if multiple else "reduce",
        })
        sells.append(sell)

    remainder = target_qty - sum(quantity for _, quantity in reductions)
    if remainder <= 1e-9:
        return sells

    buy = dict(draft)
    buy.update({"qty": remainder, "key_suffix": "buy"})
    return [*sells, buy]


def _best_bid_price(cache, instrument_id: str) -> float | None:
    """读取互斥 instrument 当前 best bid；缺盘口或非法值时 fail-closed。"""
    book = cache.order_book(_as_instrument_id(instrument_id))
    if book is None:
        return None
    fn = getattr(book, "best_bid_price", None)
    if callable(fn):
        try:
            value = fn()
            return _numeric_value(value)
        except (TypeError, ValueError):
            return None
    if isinstance(book, dict):
        try:
            value = book.get("bid") or book.get("best_bid")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _best_ask_price(cache, instrument_id: str) -> float | None:
    """读取 instrument 当前 best ask 概率；缺盘口或非法值时 fail-closed。"""
    book = cache.order_book(_as_instrument_id(instrument_id))
    if book is None:
        return None
    fn = getattr(book, "best_ask_price", None)
    if callable(fn):
        try:
            value = fn()
            return _numeric_value(value)
        except (TypeError, ValueError):
            return None
    if isinstance(book, dict):
        try:
            value = book.get("ask") or book.get("best_ask")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


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


def _long_positions(cache, instrument_id: str) -> list[tuple[str, float]]:
    """返回可被 NT 原生 submit `position_id` 精确关闭的 LONG 仓位。"""
    result = []
    for position in cache.positions_open(instrument_id=_as_instrument_id(instrument_id)) or []:
        side = str(getattr(getattr(position, "side", None), "name", getattr(position, "side", ""))).upper()
        if side and side != "LONG":
            continue
        position_id = getattr(position, "id", None) or getattr(position, "position_id", None)
        if position_id is None:
            continue
        quantity = getattr(position, "quantity", None)
        value = quantity.as_double() if hasattr(quantity, "as_double") else quantity
        try:
            parsed = abs(float(value))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.append((str(position_id), parsed))
    return sorted(result)


def _allocate_position_reductions(
    *,
    target_qty: float,
    requested_sell_qty: float,
    long_positions: list[tuple[str, float]],
    sell_min: float,
    buy_min: float,
) -> list[tuple[str, float]]:
    """把减仓量分配到具体 Position，并保证每条 SELL 与剩余 BUY 均可下单。"""
    remaining = requested_sell_qty
    reductions: list[list] = []
    for position_id, available in long_positions:
        quantity = min(available, remaining)
        if quantity + 1e-9 < sell_min:
            continue
        reductions.append([position_id, quantity])
        remaining -= quantity
        if remaining <= 1e-9:
            break

    sold = sum(quantity for _, quantity in reductions)
    buy_remainder = target_qty - sold
    if sold <= 1e-9:
        return []
    if buy_remainder <= 1e-9 or buy_remainder + 1e-9 >= buy_min:
        return [(position_id, quantity) for position_id, quantity in reductions]

    needed = buy_min - buy_remainder
    for reduction in reversed(reductions):
        reducible = reduction[1] - sell_min
        if reducible <= 1e-9:
            continue
        adjustment = min(reducible, needed)
        reduction[1] -= adjustment
        needed -= adjustment
        if needed <= 1e-9:
            break

    while needed > 1e-9 and reductions:
        _, removed = reductions.pop()
        needed -= removed

    return [(position_id, quantity) for position_id, quantity in reductions]


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
        minimum = max(minimum, float(constraint.get("min_buy_quantity") or 0.0))
    except (TypeError, ValueError):
        pass
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
