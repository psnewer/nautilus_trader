"""OrbitExchInstrumentProvider —— 包 `OrbitExchDiscoveryClient`,每场赛事产出多条 `BettingInstrument`
(home / draw 可选 / away),每条腿 `info` 填 Q9 六统一 key。

设计见 `docs/arbitrage/architectures/discovery/architecture.md §3.2 / §4.1`;
refactor.md §5.1 表(line 130 / 184)规定本类落 `nautilus_trader/adapters/orbitexch/providers.py`
(P9 唯一例外:OE 适配器子树住 NT adapter 目录;#33 校准位置)。

2026-07-03: 迁移到 `sport/details` API,与 SE 对齐,替代原有 DOM 抓取方式。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

import pandas as pd

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchDiscoveryClient
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchMarketEvent
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchRunner


class OrbitExchInstrumentProvider(InstrumentProvider):
    """OE 自写 Provider。`discovery` 由 factory 注入。

    slice 7A(#46):`sport_aliases` / `competition_aliases` 在写 info 时查表,
    实现 normalizer 假设的"Provider 填 info 时已 alias"(如 `"atp"` → `"ATP"`)。
    """

    def __init__(
        self,
        discovery: OrbitExchDiscoveryClient,
        config: InstrumentProviderConfig | None = None,
        *,
        sport_aliases: dict[str, str] | None = None,
        competition_aliases: dict[str, str] | None = None,
        sport_configs: Iterable | None = None,
        fx: float = 1.0,
    ) -> None:
        super().__init__(config=config)
        self._discovery = discovery
        self._sport_aliases = sport_aliases or {}
        self._competition_aliases = competition_aliases or {}
        self._sport_configs = list(sport_configs or [])
        self._fx = float(fx) if fx > 0 else 1.0

    async def load_all_async(self, filters: dict | None = None) -> None:
        """一轮发现:discovery → MarketEvent[] → 每场 ≥1 条 BettingInstrument 入基类。"""
        events = await self._discovery.discover_events(self._sport_configs or None)
        for event in events:
            for instrument in self._build_legs(event):
                self.add(instrument)

    def _build_legs(self, event: OrbitExchMarketEvent) -> Iterable[BettingInstrument]:
        """每方向(有 selection_id 才出腿)一条 `BettingInstrument`,info 填 6-key
        (sport / competition 走 aliases 规范化)。"""
        info_base = {
            "sport": self._sport_aliases.get(event.sport, event.sport),
            "competition": self._competition_aliases.get(event.competition, event.competition),
            "home_team": event.home_team,
            "away_team": event.away_team,
            "start_ts": event.start_ts,
        }
        for runner in event.runners:
            info = dict(info_base, selection_role=runner.role)
            yield self._betting_instrument(event, runner, info, self._fx)

    @staticmethod
    def _betting_instrument(
        event: OrbitExchMarketEvent,
        runner: OrbitExchRunner,
        info: dict,
        fx: float,
    ) -> BettingInstrument:
        min_stake_usd = Decimal("7") * Decimal(str(fx))
        market_start_time = pd.Timestamp(event.start_ts, unit="ns", tz="UTC")
        return BettingInstrument(
            venue_name="ORBITEXCH",
            betting_type="ODDS",
            competition_id=_to_int_or_zero(event.competition_id),
            competition_name=event.competition,
            event_country_code="",
            event_id=_to_int_or_zero(event.market_id),
            event_name=f"{event.home_team} v {event.away_team}",
            event_open_date=market_start_time,
            event_type_id=_to_int_or_zero(event.sport_id),
            event_type_name=event.sport,
            market_id=str(event.market_id),
            market_name=runner.role,
            market_start_time=market_start_time,
            market_type="MATCH_ODDS",
            selection_handicap=null_handicap(),
            selection_id=int(runner.selection_id),
            selection_name=runner.role,
            currency="USD",
            price_precision=2,
            size_precision=2,
            min_notional=Money(min_stake_usd, USD),
            ts_event=0,
            ts_init=0,
            info=info,
        )


def _to_int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
