"""head/reverse —— 按 outcome 仓位形态判态并维护动态 standard。"""

from __future__ import annotations

import logging
import math

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.objects import Price
from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.common.venues import is_dust_position
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.checks.quote_legs import VALID_OUTCOMES
from src.arbitrage.strategy.checks.quote_legs import best_ask
from src.arbitrage.strategy.checks.quote_legs import instrument_info
from src.arbitrage.strategy.checks.quote_legs import pair_positions
from src.arbitrage.strategy.checks.quote_legs import to_price
from src.arbitrage.strategy.checks.quote_legs import venue_of
from src.arbitrage.strategy.condition import EvalContext


_EPS = 1e-9
_LOG = logging.getLogger(__name__)


class HeadQuery(StateQuery):
    """无有效仓位或 yes/no 均有仓位时命中,并把 standard 更新为即时返水率。"""

    def matches(self, ctx: EvalContext) -> bool:
        if _active_outcome_count(ctx) not in {0, len(VALID_OUTCOMES)}:
            return False
        rate = instant_rebate(ctx)
        if rate is None:
            return False
        return _write_standard(ctx, rate)


class ReverseQuery(StateQuery):
    """恰有一个有效 outcome 仓位时命中,并按正向高水位规则更新 standard。"""

    def matches(self, ctx: EvalContext) -> bool:
        if _active_outcome_count(ctx) != 1:
            return False
        rate = instant_rebate(ctx)
        if rate is None:
            return False
        store = ctx.runtime_store
        strategy_id = ctx.strategy_id
        if store is None or not strategy_id:
            return False
        current = store.get(strategy_id, ctx.pair_id, "standard")
        if current is None:
            store.update(strategy_id, ctx.pair_id, {"standard": rate})
            return True
        try:
            current_rate = float(current)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(current_rate):
            return False
        if rate > 0 and rate > current_rate:
            store.update(strategy_id, ctx.pair_id, {"standard": rate})
        return True


def _active_outcome_count(ctx: EvalContext) -> int | None:
    portfolio = ctx.portfolio
    if portfolio is None:
        return None
    try:
        shares = portfolio.outcome_shares(ctx.pair_id)
    except PositionOutcomeInvariantError as exc:
        _LOG.error(f"PositionMode: pair={ctx.pair_id} portfolio invariant: {exc}")
        return None
    if set(shares) != set(VALID_OUTCOMES):
        return None
    normalized = []
    for outcome in VALID_OUTCOMES:
        try:
            share = float(shares[outcome])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(share) or share < 0:
            return None
        normalized.append(share)
    return sum(share > _EPS for share in normalized)


def instant_rebate(ctx: EvalContext) -> float | None:
    """按抗抖动盘口侧计算当前 pair 的即时返水率。"""
    portfolio = ctx.portfolio
    if portfolio is None:
        return None
    denominator = _configured_share(ctx)
    unrealized = _unrealized_pnl(ctx)
    if denominator is None or unrealized is None:
        return None
    try:
        realized = float(portfolio.realized_pnl_for_pair(ctx.pair_id))
    except (AttributeError, TypeError, ValueError):
        return None
    total = unrealized + realized
    if not math.isfinite(total):
        return None
    return total / denominator


def _configured_share(ctx: EvalContext) -> float | None:
    try:
        share = float((ctx.strategy_defaults or {}).get("share") or 0.0)
    except (TypeError, ValueError):
        return None
    return share if math.isfinite(share) and share > 0 else None


def _unrealized_pnl(ctx: EvalContext) -> float | None:
    portfolio = ctx.portfolio
    if portfolio is None:
        return None

    positions_by_instrument: dict[object, list] = {}
    for position in pair_positions(ctx):
        if is_dust_position(position) or getattr(position, "side", None) == PositionSide.FLAT:
            continue
        instrument_id = getattr(position, "instrument_id", None)
        if instrument_id is None:
            return None
        positions_by_instrument.setdefault(instrument_id, []).append(position)

    unrealized = 0.0
    for instrument_id, positions in positions_by_instrument.items():
        sides = {getattr(position, "side", None) for position in positions}
        if len(sides) != 1:
            return None
        side = next(iter(sides))
        probability = _instant_probability(ctx, instrument_id, side)
        info = instrument_info(ctx, instrument_id)
        quote_claim = str(info.get("quote_claim") or "yes").lower()
        price = to_price(venue_of(instrument_id), probability, quote_claim) if probability is not None else None
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        pnl = portfolio.unrealized_pnl(
            positions[0].instrument_id,
            Price.from_str(str(price)),
        )
        if pnl is None:
            return None
        value = pnl.as_double()
        if not math.isfinite(value):
            return None
        unrealized += value
    return unrealized


def _instant_probability(ctx: EvalContext, instrument_id: object, side) -> float | None:
    book = ctx.cache.order_book(instrument_id) if ctx.cache is not None else None
    if book is None:
        return None
    if side == PositionSide.LONG:
        return best_ask(book)
    if side == PositionSide.SHORT:
        return _best_bid(book)
    return None


def _best_bid(book) -> float | None:
    fn = getattr(book, "best_bid_price", None)
    if callable(fn):
        try:
            value = fn()
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    if isinstance(book, dict):
        value = book.get("bid")
        if value is None:
            value = book.get("best_bid")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _write_standard(ctx: EvalContext, rate: float) -> bool:
    if ctx.runtime_store is None or not ctx.strategy_id:
        return False
    ctx.runtime_store.update(ctx.strategy_id, ctx.pair_id, {"standard": rate})
    return True
