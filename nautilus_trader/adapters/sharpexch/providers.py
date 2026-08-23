"""SharpExch InstrumentProvider。

第一阶段复用 OE 型结构:发现事件 → 多条 `BettingInstrument`,并填 matching 必需字段。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterable

import pandas as pd

from nautilus_trader.common.providers import InstrumentProvider

_log = logging.getLogger(__name__)
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchDiscoveryClient
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchMarketEvent
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchRunner

class SharpExchInstrumentProvider(InstrumentProvider):
    """SE 自写 Provider。`discovery` 由 factory 注入。"""

    def __init__(
        self,
        discovery: SharpExchDiscoveryClient,
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
        events = await self._discovery.discover_events(self._sport_configs or None)
        for event in events:
            for instrument in self._build_legs(event):
                self.add(instrument)
        _log.info(f"SE InstrumentProvider: loaded {self.count} instruments")

    def _build_legs(self, event: SharpExchMarketEvent) -> Iterable[BettingInstrument]:
        """#228:3-way(runners 含 draw)每 selection 产 yes + 合成 no 两条腿(no 是同
        selection 的 lay 投影,下单经 `exec_instrument_id` 重定向回 yes instrument);2-way 不变。"""
        info_base = {
            "sport": self._sport_aliases.get(event.sport, event.sport),
            "competition": self._competition_aliases.get(event.competition, event.competition),
            "home_team": event.home_team,
            "away_team": event.away_team,
        }
        is_three_way = any(runner.role == "draw" for runner in event.runners)
        for runner in event.runners:
            binary_market_id = (
                f"{event.market_id}:{runner.selection_id}"
                if is_three_way
                else str(event.market_id)
            )
            info = dict(
                info_base,
                selection_role=runner.role,
                binary_market_id=binary_market_id,
            )
            if not is_three_way:
                info["claim"] = "yes" if runner.role == "home" else "no"
                yield self._betting_instrument(event, runner, info, self._fx)
                continue
            yes = self._betting_instrument(event, runner, dict(info, claim="yes"), self._fx)
            yield yes
            yield self._betting_instrument(
                event,
                runner,
                dict(
                    info,
                    claim="no",
                    quote_claim="no",
                    exec_instrument_id=str(yes.id),
                    venue_selection_id=int(runner.selection_id),
                ),
                self._fx,
                no_leg=True,
            )

    @staticmethod
    def _betting_instrument(
        event: SharpExchMarketEvent,
        runner: SharpExchRunner,
        info: dict,
        fx: float,
        *,
        no_leg: bool = False,
    ) -> BettingInstrument:
        min_stake_usd = Decimal("12")
        market_start_time = pd.Timestamp(event.start_ts, unit="ns", tz="UTC")
        return BettingInstrument(
            venue_name="SHARPEXCH",
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
            market_name=f"{runner.role}-NO" if no_leg else runner.role,
            market_start_time=market_start_time,
            market_type="MATCH_ODDS",
            # 合成 no 只作缓存身份/行情载体。负 selection 不含小数点，避免 NT 将 Symbol
            # 误判为 composite；真实 selection 保存在 info，执行仍重定向到 yes instrument。
            selection_handicap=null_handicap(),
            selection_id=-(int(runner.selection_id) + 1) if no_leg else int(runner.selection_id),
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
