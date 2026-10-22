"""
统一 cancel 判定:对候选订单按价格漂移 / 累计局数漂移 / 仓位盘数三条 opt-in 条件撤单。

合并原 position_order_cancel / in_game_cancel / in_game_order_cancel。只关注"撤单判定",
不做订单分类:候选集必须由前置 condition `exposure_source`(兼容旧 `select_orders`)写入
`scratch["cancel_candidates"]`;缺失或为空均 fail-closed。三条触发均 opt-in——对应参数未配置
就不跑该条校验:

- `price_drift`:完整 PM yes/no 在 `book_side` 对应方向的 commission 概率和位于
  `[0.98, 1.02]` 时,当前盘口与**订单自身价**的概率绝对差 `>= price_drift`。
- `game_drift`:`当前局数 − baseline >= game_drift`;baseline = 订单 `arb:order_score` 的局数,
  空/不可解析按 **0**。
- `min_tp_set` / `min_sl_set`:仅对绑定 `position_id` 且 `intent=recovery` 的平仓单，当前盘低于
  对应止盈/止损有效阈值时撤（pair 运行时覆盖优先，否则配置值）。两者互斥。

命中任一即把该订单计入定向撤单;全不命中返回 False。
"""

from __future__ import annotations

import math

from src.arbitrage.common.opportunity import order_intent
from src.arbitrage.common.opportunity import order_score
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import probability_from_price
from src.arbitrage.strategy.actions.score_selection import _SCORE_PART
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.checks.quote_legs import pair_instrument_ids
from src.arbitrage.strategy.checks.quote_legs import venue_of
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.in_game_positions import current_set_number
from src.arbitrage.strategy.queries.exposure_source import CANCEL_CANDIDATES


class OrderCancelCheck(Check):
    """按价格 / 累计局数 / 仓位盘数三条 opt-in 条件撤销候选订单。"""

    def __init__(
        self,
        book_side: str = "ask",
        price_drift: float | None = None,
        game_drift: int | None = None,
        min_tp_set: int | None = None,
        min_sl_set: int | None = None,
    ) -> None:
        self._book_side = str(book_side).lower()
        if self._book_side not in {"ask", "bid"}:
            raise ValueError("order_cancel: book_side must be ask or bid")
        if price_drift is not None:
            price_drift = float(price_drift)
            if not math.isfinite(price_drift) or not 0 <= price_drift < 1:
                raise ValueError("order_cancel: price_drift must be in [0, 1)")
        self._price_drift = price_drift
        if game_drift is not None and (
            isinstance(game_drift, bool) or not isinstance(game_drift, int) or game_drift < 0
        ):
            raise ValueError("order_cancel: game_drift must be a non-negative integer")
        self._game_drift = game_drift
        _validate_min_set("min_tp_set", min_tp_set)
        _validate_min_set("min_sl_set", min_sl_set)
        if min_tp_set is not None and min_sl_set is not None:
            raise ValueError("order_cancel: min_tp_set and min_sl_set are mutually exclusive")
        self._min_set_kind = (
            "tp" if min_tp_set is not None else "sl" if min_sl_set is not None else None
        )
        self._configured_min_set = min_tp_set if min_tp_set is not None else min_sl_set
        if price_drift is None and game_drift is None and self._min_set_kind is None:
            raise ValueError(
                "order_cancel: at least one drift or min_tp_set/min_sl_set is required",
            )

    def passes(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        candidates = ctx.scratch.get(CANCEL_CANDIDATES)
        if not isinstance(candidates, list) or not candidates:
            return False
        candidate_ids = set(candidates)

        current_games = _total_games(_current_score(ctx))
        runtime_min_set = None
        if self._min_set_kind is not None and ctx.in_game_position_store is not None:
            getter = getattr(ctx.in_game_position_store, f"min_{self._min_set_kind}_set")
            runtime_min_set = getter(ctx.pair_id)
        min_set = (
            runtime_min_set
            if runtime_min_set is not None
            else self._configured_min_set
        )
        current_set = current_set_number(
            pair_id=ctx.pair_id,
            pair_registry=ctx.pair_registry,
            sports_store=ctx.sports_store,
        )
        position_id_fn = getattr(ctx.cache, "position_id", None)
        price_drift_enabled = self._price_drift is not None and _side_satisfies_commission(
            ctx,
            self._book_side,
        )

        triggers = []
        for instrument_id in pair_instrument_ids(ctx):
            book = ctx.cache.order_book(instrument_id)
            current_probability = _book_probability(book, self._book_side)
            info = instrument_info(ctx, instrument_id)
            quote_claim = str(info.get("quote_claim") or "yes").lower()
            venue = venue_of(instrument_id)
            for order in ctx.cache.orders_open(instrument_id=instrument_id) or ():
                raw_client_order_id = getattr(order, "client_order_id", None)
                client_order_id = str(raw_client_order_id or "")
                if not client_order_id:
                    continue
                if client_order_id not in candidate_ids:
                    continue

                price_hit = False
                if price_drift_enabled and current_probability is not None:
                    order_price = _number(getattr(order, "price", None))
                    if order_price is not None:
                        try:
                            order_probability = probability_from_price(
                                venue,
                                order_price,
                                quote_claim,
                            )
                            drift = abs(current_probability - order_probability)
                            price_hit = (
                                drift > self._price_drift
                                or math.isclose(
                                    drift,
                                    self._price_drift,
                                    rel_tol=0.0,
                                    abs_tol=1e-12,
                                )
                            )
                        except (KeyError, TypeError, ValueError, ZeroDivisionError):
                            price_hit = False

                game_hit = False
                if self._game_drift is not None and current_games is not None:
                    placed = _total_games(order_score(order))
                    baseline = placed if placed is not None else 0
                    game_hit = current_games - baseline >= self._game_drift

                below_min_set = False
                if self._min_set_kind is not None and min_set is not None and current_set > 0:
                    position_id = (
                        position_id_fn(raw_client_order_id) if callable(position_id_fn) else None
                    )
                    below_min_set = (
                        position_id is not None
                        and order_intent(order) == "recovery"
                        and current_set < min_set
                    )

                if price_hit or game_hit or below_min_set:
                    triggers.append(client_order_id)

        if not triggers:
            return False
        ctx.scratch["cancel_pair_orders"] = {
            "reason": "order_cancel",
            "client_order_ids": sorted(set(triggers)),
            "book_side": self._book_side,
            "price_drift": self._price_drift,
            "game_drift": self._game_drift,
            "min_tp_set": min_set if self._min_set_kind == "tp" else None,
            "min_sl_set": min_set if self._min_set_kind == "sl" else None,
        }
        return True


def _validate_min_set(name: str, value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"order_cancel: {name} must be a positive integer")


def _current_score(ctx: EvalContext) -> str:
    if ctx.sports_store is None or ctx.pair_registry is None:
        return ""
    game_id = ctx.pair_registry.game_id_for_pair(ctx.pair_id)
    state = ctx.sports_store.get(game_id) if game_id is not None else None
    return str(getattr(state, "score", "") or "")


def _total_games(score: str) -> int | None:
    parts = [part.strip() for part in str(score or "").split(",") if part.strip()]
    matches = [_SCORE_PART.match(part) for part in parts]
    if not matches or any(match is None for match in matches):
        return None
    return sum(int(match.group(1)) + int(match.group(2)) for match in matches if match is not None)


def _book_probability(book, side: str) -> float | None:
    if book is None:
        return None
    fn = getattr(book, f"best_{side}_price", None)
    if callable(fn):
        try:
            value = fn()
        except Exception:
            return None
    elif isinstance(book, dict):
        value = book.get(side)
        if value is None:
            value = book.get(f"best_{side}")
    else:
        return None
    value = _number(value)
    return value if value is not None and math.isfinite(value) and 0 < value < 1 else None


def _side_satisfies_commission(ctx: EvalContext, side: str) -> bool:
    """完整 PM 二元盘口的指定侧概率和须位于 commission 闭区间。"""
    probabilities: dict[str, float] = {}
    for instrument_id in pair_instrument_ids(ctx):
        if venue_of(instrument_id) != POLYMARKET:
            continue
        info = instrument_info(ctx, instrument_id)
        outcome = str(info.get("claim") or info.get("selection_role") or "").lower()
        if outcome not in VALID_OUTCOMES or outcome in probabilities:
            return False
        probability = _book_probability(ctx.cache.order_book(instrument_id), side)
        if probability is None:
            return False
        probabilities[outcome] = probability
    return (
        set(probabilities) == set(VALID_OUTCOMES)
        and 0.98 <= sum(probabilities.values()) <= 1.02
    )


def _number(value) -> float | None:
    if value is None:
        return None
    fn = getattr(value, "as_double", None)
    try:
        return float(fn()) if callable(fn) else float(value)
    except (TypeError, ValueError):
        return None
