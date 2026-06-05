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
from typing import Any

import pyarrow as pa

from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.core import Data
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


SPORTS_CLIENT = "PMSPORTS"   # 不含 `-`:NT node_builder 按 `key.partition("-")[0]` 找 factory,
                            # "POLYMARKET-SPORTS" 会被前缀路由到 POLYMARKET 主 factory(#60 smoke 抓出)
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"


class PolymarketSportsDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    """`sports_ws_url` 可覆盖端点(默认 `SPORTS_WS_URL`)。"""

    sports_ws_url: str | None = None


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
    WS 协议层 keepalive;另兼容偶发 app-level text `"ping"`(回 `"pong"`)。
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
        self._ws_url = getattr(config, "sports_ws_url", None) or SPORTS_WS_URL
        self._ws_task: asyncio.Task | None = None
        # publish_data 风格裸 publish:`_handle_data` 走 DataEngine.process 只认内置/CustomData,
        # 裸自定义 Data 被拒("unrecognized type",#60 smoke 抓出)。消费者用 `msgbus.subscribe`
        # 订 `data.{Type}*`,故生产者也裸 publish 到同 topic(同 MatchedPair/InstrumentsRefreshed)。
        self._sports_topic = "data." + DataType(SportsGameUpdate).topic   # = "data.SportsGameUpdate*"
        self._first_frame_logged = False  # 一次性"feed 活着"确认日志(稀疏 firehose 运维可观测）

    async def _connect(self) -> None:
        self._ws_task = self.create_task(self._run_ws())
        self._log.info(f"PolymarketSportsDataClient connecting WS firehose: {self._ws_url}")

    async def _disconnect(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None

    async def _run_ws(self) -> None:
        import websockets  # 局部 import:仅 sports client 用

        while True:  # 断线重连(外层 cancel 退出)
            try:
                async with websockets.connect(self._ws_url, ping_interval=20, max_size=None) as ws:
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
