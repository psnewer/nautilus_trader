"""比赛阶段 self_hits 判据。

框架首个实用 `self_hits` 叶子(此前全是 `{}` = vacuous truth)。只读 sports 现状,
不保存跨轮状态。详细设计见 strategy §4.3 / §3.10。
"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.common.sports_phase import PHASE_POST
from src.arbitrage.common.sports_phase import PHASE_PRE
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class InGameQuery(StateQuery):
    """比赛进行中 = phase 为 ``IN_PLAY``，或已到 OE 计划开始时间。

    ``POST`` 优先表示终场，不得被计划开始时间重新判为赛中。
    """

    def matches(self, ctx: EvalContext) -> bool:
        return game_stage(ctx) == "in_game"


class PreGameQuery(StateQuery):
    """明确赛前 = phase 为 ``PRE`` 且尚未到 OE 计划开始时间。

    Store 无记录或 phase 缺失均是 UNKNOWN，不能由 ``NOT in_game`` 反推为赛前。
    """

    def matches(self, ctx: EvalContext) -> bool:
        return game_stage(ctx) == "pre_game"


def game_stage(ctx: EvalContext) -> str | None:
    """按终场 > 明确赛中 > OE 开始时间 > 明确赛前解析当前阶段。"""
    pair_registry = ctx.pair_registry
    if pair_registry is None:
        return None

    phase = None
    phase_store = ctx.phase_store
    if phase_store is not None:
        game_id = pair_registry.game_id_for_pair(ctx.pair_id)
        if game_id is not None:
            state = phase_store.get(game_id)
            phase = getattr(state, "phase", None) if state is not None else None

    if phase == PHASE_POST:
        return None
    if phase == PHASE_IN_PLAY:
        return "in_game"

    start_ns = _oe_market_start_ns(ctx)
    now_ns = ctx.ts_now_ns
    if start_ns is not None and now_ns is not None and int(now_ns) >= start_ns:
        return "in_game"
    if phase == PHASE_PRE:
        return "pre_game"
    return None


def _oe_market_start_ns(ctx: EvalContext) -> int | None:
    if ctx.cache is None or ctx.pair_registry is None:
        return None
    starts = []
    for value in ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id):
        if venue_id_from_instrument_id(value) != ORBITEXCH:
            continue
        instrument_id = (
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        )
        instrument = ctx.cache.instrument(instrument_id)
        raw = getattr(instrument, "market_start_time", None)
        try:
            start_ns = int(getattr(raw, "value", raw))
        except (TypeError, ValueError):
            continue
        if start_ns > 0:
            starts.append(start_ns)
    return min(starts) if starts else None
