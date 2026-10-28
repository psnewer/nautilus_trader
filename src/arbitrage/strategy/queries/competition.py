"""按赛事名称包含指定子串过滤分支的 self_hits 判据。"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class CompetitionIncludeQuery(StateQuery):
    """pair 的 OE 赛事(competition)名称包含指定子串(大小写不敏感)时命中。"""

    def __init__(self, include: str) -> None:
        if not isinstance(include, str) or not include.strip():
            raise ValueError("competition: include must be a non-empty string")
        self._include = include.strip().upper()

    def matches(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        for value in sorted(ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id), key=str):
            if venue_id_from_instrument_id(value) != ORBITEXCH:
                continue
            instrument_id = (
                value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
            )
            instrument = ctx.cache.instrument(instrument_id)
            competition = str(getattr(instrument, "competition_name", "") or "").upper()
            if self._include in competition:
                return True
        return False
