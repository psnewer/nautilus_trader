"""按 PM/OE 原始赛事名称精确过滤分支的 self_hits 判据。"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import venue_id_from_instrument_id
from src.arbitrage.strategy.bool_expr import StateQuery
from src.arbitrage.strategy.condition import EvalContext


class CompetitionIsQuery(StateQuery):
    """pair 的 PM 或 OE 原始赛事名称等于指定值之一时命中。"""

    def __init__(self, **params) -> None:
        unknown = set(params) - {"is"}
        if unknown:
            raise ValueError(f"competition: unknown params {sorted(unknown)!r}")
        values = params.get("is")
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise ValueError("competition: is must be a non-empty array of non-empty strings")
        self._values = frozenset(values)

    def matches(self, ctx: EvalContext) -> bool:
        if ctx.cache is None or ctx.pair_registry is None:
            return False
        for value in sorted(ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id), key=str):
            venue = venue_id_from_instrument_id(value)
            if venue not in {POLYMARKET, ORBITEXCH}:
                continue
            instrument_id = (
                value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
            )
            instrument = ctx.cache.instrument(instrument_id)
            if venue == POLYMARKET:
                info = getattr(instrument, "info", None)
                competition = str(
                    info.get("competition_raw", "") if isinstance(info, dict) else "",
                )
            else:
                competition = str(getattr(instrument, "competition_name", "") or "")
            if competition in self._values:
                return True
        return False
