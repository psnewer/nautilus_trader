"""按可选来源及盘数门输出带实时 bid 的 PM LONG 平仓 SELL 腿。"""

from __future__ import annotations

import math

from src.arbitrage.common.opportunity import order_origin
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.checks.quote_legs import venue_of
from src.arbitrage.strategy.condition import Check
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.in_game_positions import current_set_number
from src.arbitrage.strategy.leg_plan import as_instrument_id
from src.arbitrage.strategy.queries.exposure_source import POSITION_CANDIDATES


class PositionCloseCheck(Check):
    """输出 PM LONG 仓位的全量 SELL@bid 腿；可选止盈或止损盘数门。"""

    def __init__(
        self,
        origin: str | None = None,
        min_tp_set: int | None = None,
        min_sl_set: int | None = None,
    ) -> None:
        if origin is not None and origin not in {"pre_game", "in_game"}:
            raise ValueError("position_close: origin must be pre_game or in_game")
        _validate_min_set("min_tp_set", min_tp_set)
        _validate_min_set("min_sl_set", min_sl_set)
        if min_tp_set is not None and min_sl_set is not None:
            raise ValueError("position_close: min_tp_set and min_sl_set are mutually exclusive")
        self._origin = origin
        self._min_set_kind = (
            "tp" if min_tp_set is not None else "sl" if min_sl_set is not None else None
        )
        self._configured_min_set = min_tp_set if min_tp_set is not None else min_sl_set

    def passes(self, ctx: EvalContext) -> bool:
        if self._min_set_kind is not None:
            runtime_min_set = None
            if ctx.in_game_position_store is not None:
                getter = getattr(ctx.in_game_position_store, f"min_{self._min_set_kind}_set")
                runtime_min_set = getter(ctx.pair_id)
            min_set = (
                runtime_min_set
                if runtime_min_set is not None
                else self._configured_min_set
            )
            if current_set_number(
                pair_id=ctx.pair_id,
                pair_registry=ctx.pair_registry,
                sports_store=ctx.sports_store,
            ) < min_set:
                return False
        candidate_ids = ctx.scratch.get(POSITION_CANDIDATES)
        legs = _position_close_legs(
            ctx,
            origin=self._origin,
            candidate_ids=set(candidate_ids) if isinstance(candidate_ids, list) else None,
        )
        if not legs:
            return False
        ctx.scratch["legs"] = legs
        return True


def _validate_min_set(name: str, value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"position_close: {name} must be a positive integer")


def _position_close_legs(
    ctx: EvalContext,
    *,
    origin: str | None = None,
    candidate_ids: set[str] | None = None,
) -> list[dict]:
    legs = []
    for position in pair_positions(ctx):
        position_id = getattr(position, "id", None) or getattr(position, "position_id", None)
        if candidate_ids is not None and str(position_id or "") not in candidate_ids:
            continue
        if origin is not None and _position_origin(ctx, position) != origin:
            continue
        instrument_id = getattr(position, "instrument_id", None)
        raw_side = getattr(position, "side", None)
        side = str(getattr(raw_side, "name", raw_side or "")).upper()
        quantity = _number(getattr(position, "quantity", None))
        if instrument_id is None or venue_of(instrument_id) != POLYMARKET or side != "LONG":
            continue
        if quantity is None or quantity <= 0:
            continue
        info = instrument_info(ctx, instrument_id)
        outcome = str(info.get("claim") or info.get("selection_role") or "").lower()
        if outcome not in {"yes", "no", "home", "away"} or position_id is None:
            continue
        book = ctx.cache.order_book(as_instrument_id(instrument_id))
        price = _best_bid(book)
        if price is None:
            return []
        legs.append({
            "instrument_id": str(instrument_id),
            "venue": POLYMARKET,
            "side": "SELL",
            "role": outcome,
            "claim": outcome,
            "qty": quantity,
            "position_id": str(position_id),
            "price": price,
            "prob": price,
        })
    return legs


def _position_origin(ctx: EvalContext, position) -> str | None:
    if ctx.cache is None:
        return None
    opening_order_id = getattr(position, "opening_order_id", None)
    if opening_order_id is None:
        return None
    opening_order = ctx.cache.order(opening_order_id)
    return order_origin(opening_order) if opening_order is not None else None


def _number(value) -> float | None:
    if value is None:
        return None
    fn = getattr(value, "as_double", None)
    try:
        return float(fn()) if callable(fn) else float(value)
    except (TypeError, ValueError):
        return None


def _best_bid(book) -> float | None:
    if book is None:
        return None
    fn = getattr(book, "best_bid_price", None)
    if callable(fn):
        try:
            value = fn()
        except Exception:
            return None
    elif isinstance(book, dict):
        value = book.get("bid")
        if value is None:
            value = book.get("best_bid")
    else:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 < value < 1 else None
