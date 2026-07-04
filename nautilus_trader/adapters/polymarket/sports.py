# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""
Polymarket Sports 实时赛事信号(#60 slice)。

PM 提供独立的 **Sports WebSocket**(`wss://sports-api.polymarket.com/ws`):无订阅、无鉴权、
firehose 推所有活跃赛事状态(事件驱动、稀疏)。NT 原生 PM 适配器只连 CLOB market/user WS,
对此零支持(本会话全库 grep 确认)。

本模块:
- `SportsGameUpdate`:NT `@customdataclass` Data 事件(matching eviction + strategy 订阅)。
- `parse_sport_result`:原始 `sport_result` dict → `SportsGameUpdate`(纯映射,可单测)。
- `PolymarketSportsDataClient`:`LiveMarketDataClient` 子类,`_connect` 开 WS firehose →
  `msgbus.publish` 裸发 `SportsGameUpdate` 到 `data.SportsGameUpdate*`(同 MatchedPair/
  InstrumentsRefreshed 的 publish_data 风格)。**注**:`_handle_data` 走 DataEngine.process 只认
  内置/CustomData,裸自定义 Data 报 "unrecognized type"(#60 smoke 抓);消费者用
  `msgbus.subscribe("data.{Type}*")`(带 #58 的 `*` 通配)。

映射:`gameId` 与 gamma `event["gameId"]` 同值(本会话实采证实:wnba 13002300 双向对上,
ATP 36 events 全有 gameId)→ 下游经 `info["game_id"]` 查 pair。

设计见 `refactor.md §5.9`。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd
import pyarrow as pa

from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core import Data
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import DataType
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.polymarket.arb_provider import _parse_team_names
from nautilus_trader.adapters.polymarket.arb_provider import _teams_from_event
from nautilus_trader.adapters.polymarket.arb_provider import _EVENTS_PAGE_LIMIT
from nautilus_trader.adapters.polymarket.arb_provider import _GAMMA_BASE


SPORTS_CLIENT = "PMSPORTS"   # 不含 `-`:NT node_builder 按 `key.partition("-")[0]` 找 factory,
                            # "POLYMARKET-SPORTS" 会被前缀路由到 POLYMARKET 主 factory(#60 smoke 抓出)
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"


class PolymarketSportsDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    """`sports_ws_url` 可覆盖端点(默认 `SPORTS_WS_URL`)。"""

    sports_ws_url: str | None = None
    update_instruments_interval_mins: int | None = 60


class PolymarketSportsInstrumentProvider(InstrumentProvider):
    """PMSPORTS synthetic event anchor provider(#127)。

    从公开 Gamma sports discovery 读取赛事,每场只产出一条 non-tradable `.PMSPORTS`
    `BettingInstrument`。它只给 matching 当 event anchor,不表达可交易腿、不进入 Strategy/Risk/Execution。
    """

    def __init__(
        self,
        config: InstrumentProviderConfig | None = None,
        *,
        target_competitions: list | tuple | None = None,
        competition_to_sport: dict | None = None,
        competition_aliases: dict | None = None,
    ) -> None:
        super().__init__(config=config)
        self._target_competitions = {str(c).lower() for c in (target_competitions or [])}
        self._competition_to_sport = dict(competition_to_sport or {})
        self._competition_aliases = dict(competition_aliases or {})

    async def load_all_async(self, filters: dict | None = None) -> None:
        if not self._target_competitions:
            self._log.info("PMSPORTS discovery: no target competitions configured → load 0")
            return

        async with httpx.AsyncClient(timeout=30.0) as client:
            sports = await self._fetch_json(client, f"{_GAMMA_BASE}/sports")
            count = 0
            for comp_info in sports or []:
                comp_raw = str(comp_info.get("sport", ""))
                series_id = comp_info.get("series")
                if comp_raw.lower() not in self._target_competitions or not series_id:
                    continue
                sport = self._competition_to_sport.get(comp_raw.lower(), comp_raw)
                competition = self._competition_aliases.get(
                    comp_raw,
                    self._competition_aliases.get(comp_raw.lower(), comp_raw),
                )
                count += await self._load_series(client, str(series_id), competition, sport)
        self._log.info(f"PMSPORTS discovery: loaded {count} anchor instrument(s)")

    async def _load_series(self, client, series_id: str, comp_name: str, sport: str) -> int:
        events = await self._fetch_json(
            client,
            f"{_GAMMA_BASE}/events",
            params={
                "series_id": series_id,
                "closed": "false",
                "active": "true",
                "limit": _EVENTS_PAGE_LIMIT,
            },
        )
        count = 0
        for event in events or []:
            if event.get("closed") or not event.get("active", True):
                continue
            count += self._process_event(event, comp_name, sport)
        return count

    def _process_event(self, event: dict, comp_name: str, sport: str) -> int:
        game_id = event.get("gameId")
        if game_id is None:
            return 0

        teams_info = _teams_from_event(event)
        if teams_info is not None:
            home_team, away_team, _, _ = teams_info
        else:
            parsed = _parse_team_names(event.get("title", ""))
            if parsed is None:
                return 0
            home_team, away_team = parsed

        anchor = self._anchor_instrument(
            game_id=int(game_id),
            event=event,
            comp_name=comp_name,
            sport=sport,
            home_team=home_team,
            away_team=away_team,
        )
        self.add(anchor)
        return 1

    @staticmethod
    def _anchor_instrument(
        *,
        game_id: int,
        event: dict,
        comp_name: str,
        sport: str,
        home_team: str,
        away_team: str,
    ) -> BettingInstrument:
        start_ts = _parse_start_ts(event.get("startDate") or "")
        event_id = _to_int_or_zero(event.get("id"))
        selection_id = game_id if game_id > 0 else event_id
        open_date = pd.Timestamp(start_ts, unit="ns", tz="UTC") if start_ts else pd.Timestamp(0, unit="ns", tz="UTC")
        return BettingInstrument(
            venue_name=SPORTS_CLIENT,
            betting_type="EVENT",
            competition_id=0,
            competition_name=comp_name,
            event_country_code="",
            event_id=event_id,
            event_name=f"{home_team} v {away_team}",
            event_open_date=open_date,
            event_type_id=0,
            event_type_name=sport,
            market_id=str(game_id),
            market_name="event_anchor",
            market_start_time=open_date,
            market_type="EVENT_ANCHOR",
            selection_handicap=null_handicap(),
            selection_id=selection_id,
            selection_name="event",
            currency="USD",
            price_precision=2,
            size_precision=2,
            min_notional=Money(Decimal("0"), USD),
            ts_event=0,
            ts_init=0,
            info={
                "sport": sport,
                "competition": comp_name,
                "home_team": home_team,
                "away_team": away_team,
                "start_ts": start_ts,
                "selection_role": "event",
                "game_id": game_id,
                "tradable": False,
                "anchor": True,
            },
        )

    @staticmethod
    async def _fetch_json(client, url: str, params: dict | None = None):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            # 保持发现失败 fail-soft,下轮再试;cache 保留 last-good。
            import logging
            logging.getLogger(__name__).warning(f"PMSPORTS discovery fetch failed {url} params={params}: {e}")
            return None


@customdataclass
class SportsGameUpdate(Data):
    """PM Sports WS 一条 `sport_result` 的归一视图。

    Fields(`@customdataclass` 自动注入 `ts_event` / `ts_init`):
    - `game_id`      PM gameId(== gamma `event["gameId"]`,**映射键**)
    - `league`       `leagueAbbreviation`(nfl/wnba/nhl/mlb/fif…)
    - `home_team` / `away_team`  队名(格式逐 league 异:wnba 三字码 / fif 全名)
    - `status`       如 "InProgress"
    - `score` / `period` / `elapsed`  比分 / 阶段 / 钟
    - `live`         进行中
    - `ended`        已结束(eviction 触发,D4)
    - `finished_ts`  ISO 结束时刻(仅 ended 时非空)
    """

    game_id: int
    league: str
    home_team: str
    away_team: str
    status: str
    score: str
    period: str
    elapsed: str
    live: bool
    ended: bool
    finished_ts: str

    _schema = pa.schema(
        {
            "game_id": pa.int64(),
            "league": pa.string(),
            "home_team": pa.string(),
            "away_team": pa.string(),
            "status": pa.string(),
            "score": pa.string(),
            "period": pa.string(),
            "elapsed": pa.string(),
            "live": pa.bool_(),
            "ended": pa.bool_(),
            "finished_ts": pa.string(),
            "ts_event": pa.int64(),
            "ts_init": pa.int64(),
        },
        metadata={"type": "SportsGameUpdate"},
    )

    def to_dict(self, to_arrow: bool = False) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "league": self.league,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "status": self.status,
            "score": self.score,
            "period": self.period,
            "elapsed": self.elapsed,
            "live": self.live,
            "ended": self.ended,
            "finished_ts": self.finished_ts,
            "ts_event": self.ts_event,
            "ts_init": self.ts_init,
        }

    @staticmethod
    def from_dict(values: dict[str, Any]) -> "SportsGameUpdate":
        return SportsGameUpdate(
            ts_event=values["ts_event"],
            ts_init=values["ts_init"],
            game_id=int(values["game_id"]),
            league=values["league"],
            home_team=values["home_team"],
            away_team=values["away_team"],
            status=values["status"],
            score=values["score"],
            period=values["period"],
            elapsed=values["elapsed"],
            live=bool(values["live"]),
            ended=bool(values["ended"]),
            finished_ts=values["finished_ts"],
        )


def parse_sport_result(d: dict, *, ts: int) -> SportsGameUpdate | None:
    """原始 `sport_result` dict → `SportsGameUpdate`;缺 `gameId` 返 None(防御)。

    字段取自顶层(实采:顶层与嵌套 `eventState` 同值);`finished_timestamp` 仅 ended 时有。
    """
    gid = d.get("gameId")
    if gid is None:
        return None
    return SportsGameUpdate(
        ts_event=ts,
        ts_init=ts,
        game_id=int(gid),
        league=str(d.get("leagueAbbreviation") or d.get("league") or ""),
        home_team=str(d.get("homeTeam") or ""),
        away_team=str(d.get("awayTeam") or ""),
        status=str(d.get("status") or ""),
        score=str(d.get("score") or ""),
        period=str(d.get("period") or ""),
        elapsed=str(d.get("elapsed") or ""),
        live=bool(d.get("live")),
        ended=bool(d.get("ended")),
        finished_ts=str(d.get("finished_timestamp") or ""),
    )


class PolymarketSportsDataClient(LiveMarketDataClient):
    """PM Sports WS firehose → publish `SportsGameUpdate`(#60)。

    无 instrument 订阅:`_connect` 即开 WS 流式收;消费者经 `subscribe_data(DataType(SportsGameUpdate))`。
    服务端协议层 ping 由 websockets 自动回 pong;另兼容偶发 app-level text `"ping"`(回 `"pong"`)。
    """

    def __init__(self, loop, msgbus, cache, clock, instrument_provider, config) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SPORTS_CLIENT),
            venue=Venue(SPORTS_CLIENT),  # 合成 venue:不接收 instrument 订阅,只发自定义 data
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._sports_config = config  # Keep config object ref (base class stores json_primitives dict)
        self._ws_url = getattr(config, "sports_ws_url", None) or SPORTS_WS_URL
        self._ws_task: asyncio.Task | None = None
        self._update_instruments_task: asyncio.Task | None = None
        # publish_data 风格裸 publish:`_handle_data` 走 DataEngine.process 只认内置/CustomData,
        # 裸自定义 Data 被拒("unrecognized type",#60 smoke 抓出)。消费者用 `msgbus.subscribe`
        # 订 `data.{Type}*`,故生产者也裸 publish 到同 topic(同 MatchedPair/InstrumentsRefreshed)。
        self._sports_topic = "data." + DataType(SportsGameUpdate).topic   # = "data.SportsGameUpdate*"
        self._first_frame_logged = False  # 一次性"feed 活着"确认日志(稀疏 firehose 运维可观测）

    async def _connect(self) -> None:
        await self._instrument_provider.load_all_async()
        self._send_all_instruments_to_data_engine()
        update_interval = getattr(self._sports_config, "update_instruments_interval_mins", None)
        if update_interval:
            self._update_instruments_task = self.create_task(self._update_instruments(update_interval))
        self._ws_task = self.create_task(self._run_ws())
        self._log.info(f"PolymarketSportsDataClient connecting WS firehose: {self._ws_url}")

    async def _disconnect(self) -> None:
        if self._update_instruments_task is not None:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    async def _update_instruments(self, interval_mins: int) -> None:
        try:
            while True:
                await asyncio.sleep(interval_mins * 60)
                try:
                    await self._instrument_provider.load_all_async()
                    self._send_all_instruments_to_data_engine()
                except Exception as e:
                    self._log.warning(f"PMSPORTS update_instruments failed: {e!r}; retrying next cycle")
        except asyncio.CancelledError:
            self._log.debug("Canceled task 'pmsports_update_instruments'")

    async def _run_ws(self) -> None:
        import websockets  # 局部 import:仅 sports client 用

        while True:  # 断线重连(外层 cancel 退出)
            try:
                async with websockets.connect(self._ws_url, ping_interval=None, max_size=None) as ws:
                    self._log.info("Sports WS connected")
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        s = raw.strip()
                        if s == "ping":
                            await ws.send("pong")
                            continue
                        self._on_frame(s)
            except asyncio.CancelledError:
                self._log.debug("Sports WS task canceled")
                return
            except Exception as e:  # noqa: BLE001 — 重连,不让单次异常杀任务
                self._log.warning(f"Sports WS error: {e!r}; reconnecting in 5s")
                await asyncio.sleep(5.0)

    def _on_frame(self, s: str) -> None:
        try:
            j = json.loads(s)
        except (ValueError, TypeError):
            return
        now = self._clock.timestamp_ns()
        for it in (j if isinstance(j, list) else [j]):
            if not isinstance(it, dict):
                continue
            update = parse_sport_result(it, ts=now)
            if update is not None:
                if not self._first_frame_logged:
                    self._first_frame_logged = True
                    self._log.info(
                        f"Sports feed live: first update {update.league}/{update.game_id} "
                        f"({update.home_team} vs {update.away_team}, live={update.live} ended={update.ended})",
                    )
                self._msgbus.publish(topic=self._sports_topic, msg=update)


def _parse_start_ts(date_str: str) -> int:
    if not date_str:
        return 0
    try:
        return int(pd.Timestamp(date_str).value)
    except (ValueError, TypeError):
        return 0


def _to_int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
