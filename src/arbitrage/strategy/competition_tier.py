"""Strategy 共用的赛事级别判定。"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.condition import EvalContext


LOWER_TIER_COMPETITION_MARKERS = ("CHALLENGER", "WTA", "UTR", "ITF")


def lower_tier_oe_competition(ctx: EvalContext) -> str | None:
    """从 pair 的 OE BettingInstrument 读取低级别赛事原始名称。"""
    if ctx.cache is None or ctx.pair_registry is None:
        return None
    for value in sorted(ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id), key=str):
        if venue_id_from_instrument_id(value) != ORBITEXCH:
            continue
        instrument_id = (
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
        )
        instrument = ctx.cache.instrument(instrument_id)
        competition = str(getattr(instrument, "competition_name", "") or "").strip()
        normalized = competition.upper()
        if any(marker in normalized for marker in LOWER_TIER_COMPETITION_MARKERS):
            return competition
    return None
