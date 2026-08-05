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
- **#250 状态管线**(严格对标 NT 内置数据链路,"Cache 是状态、事件是通知"):
  `SportsGameStateStore`(最新状态真理源,物理存储 = NT Cache 通用对象区)+
  `SportsGameDataFilter`(二级内容过滤 seam)+
  `SportsGameDataProcessor`(固定顺序:兴趣门控 → 过滤 → 有效性 → 先写 Cache → 发布)。
- `PolymarketSportsDataClient`:`LiveMarketDataClient` 子类,`_connect` 开 WS firehose →
  processor 处理;**(game_id, channel) 即订阅键**(#322 分 phase/score 通道),消费者按 (场,通道)
  `subscribe_data(sports_data_type(game_id, channel), client_id=PMSPORTS)`,发布走该 (场,通道) topic。
  兴趣记账复用 NT 原生订阅注册表(engine 逐 (场,通道) 转发命令);某场全部通道订阅归零时
  `_unsubscribe` 回收 Store 条目。

映射:`gameId` 与 gamma `event["gameId"]` 同值(本会话实采证实:wnba 13002300 双向对上,
ATP 36 events 全有 gameId)→ 下游经 `info["game_id"]` 查 pair(#250 路由键统一 game_id)。

设计见 `refactor.md §5.9` / `docs/arbitrage/architectures/data/architecture.md §3.4.1`(#250)。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pandas as pd
import pyarrow as pa

from nautilus_trader.adapters.polymarket.arb_provider import _GAMMA_BASE
from nautilus_trader.adapters.polymarket.arb_provider import _parse_team_names
from nautilus_trader.adapters.polymarket.arb_provider import _teams_from_event
from nautilus_trader.adapters.polymarket.common.gamma_markets import fetch_gamma_events_keyset
from nautilus_trader.adapters.polymarket.common.gamma_markets import fetch_gamma_json
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.core import Data
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.core.nautilus_pyo3 import WebSocketClient
from nautilus_trader.core.nautilus_pyo3 import WebSocketConfig
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import CustomData
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import Money


SPORTS_CLIENT = "PMSPORTS"   # 不含 `-`:NT node_builder 按 `key.partition("-")[0]` 找 factory,
                            # "POLYMARKET-SPORTS" 会被前缀路由到 POLYMARKET 主 factory(#60 smoke 抓出)
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"


class PolymarketSportsDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    """`sports_ws_url` 可覆盖端点(默认 `SPORTS_WS_URL`)。"""

    sports_ws_url: str | None = None
    proxy_url: str | None = None
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
        http_client: HttpClient | None = None,
    ) -> None:
        super().__init__(config=config)
        self._target_competitions = {str(c).lower() for c in (target_competitions or [])}
        self._competition_to_sport = dict(competition_to_sport or {})
        self._competition_aliases = dict(competition_aliases or {})
        # Gamma discovery 与 PM 主链共用路由:factory 注入带 proxy_url 的 NT HttpClient
        self._http_client = http_client or HttpClient(timeout_secs=30)

    async def load_all_async(self, filters: dict | None = None) -> None:
        if not self._target_competitions:
            self._log.info("PMSPORTS discovery: no target competitions configured → load 0")
            return

        client = self._http_client
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
        try:
            events = await fetch_gamma_events_keyset(
                client,
                {
                    "series_id": series_id,
                    "closed": "false",
                    "active": "true",
                },
                base_url=_GAMMA_BASE,
            )
        except Exception as e:
            # 保持发现失败 fail-soft，下轮再试；provider 保留 last-good。
            self._log.warning(
                "PMSPORTS discovery fetch failed "
                f"{_GAMMA_BASE}/events/keyset series_id={series_id}: {e}",
            )
            return 0
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
                "selection_role": "event",
                "game_id": game_id,
                "tradable": False,
                "anchor": True,
            },
        )

    async def _fetch_json(self, client, url: str, params: dict | None = None):
        try:
            return await fetch_gamma_json(client, url, params)
        except Exception as e:
            # 保持发现失败 fail-soft,下轮再试;cache 保留 last-good。
            self._log.warning(f"PMSPORTS discovery fetch failed {url} params={params}: {e}")
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


# ── #250:CustomData 状态管线(设计 data §3.4.1)─────────────────────────────

_STORE_KEY_PREFIX = "pmsports:game:"


# #322:sports 数据分通道(phase / score),各自独立 topic;见 data §3.4.2。
SPORTS_CHANNEL_PHASE = "phase"
SPORTS_CHANNEL_SCORE = "score"
_SPORTS_CHANNELS = (SPORTS_CHANNEL_PHASE, SPORTS_CHANNEL_SCORE)


def sports_phase(update: SportsGameUpdate) -> str:
    """归一化三态,**sport-agnostic** —— 仅凭统一布尔 `live`/`ended`,不解析逐 sport 的
    `status`/`period`(#322,data §3.4.2)。实盘核实 firehose 基本不推赛前帧,PRE 极少作为
    事件出现(首帧多已 IN_PLAY)。"""
    if update.ended:
        return "POST"
    if update.live:
        return "IN_PLAY"
    return "PRE"


def sports_data_type(game_id, channel) -> DataType:
    """(game_id, channel) → NT `DataType`(metadata 两键进 topic:
    `data.SportsGameUpdate.game_id=<gid>.channel=<ch>`)。

    **(game_id, channel) 即订阅键**:每场每通道一个独立 DataType/topic,消费者按 (场,通道)
    `subscribe_data`。metadata 参与 DataType 身份,engine 逐 (场,通道) 转发 subscribe/unsubscribe,
    NT client 基类原生记账(对标 OE 赔率链路 routing 表)。**键顺序固定 game_id→channel**:
    `DataType.topic` 按 metadata 插入序渲染,固定键序保证 publish/subscribe 两侧 topic 串一致。
    """
    return DataType(SportsGameUpdate, metadata={"game_id": int(game_id), "channel": str(channel)})


def game_id_of_data_type(data_type: DataType) -> int | None:
    """从订阅命令的 DataType 反解 game_id;非本模型的 DataType 返 None。"""
    if data_type.type is not SportsGameUpdate:
        return None
    gid = (data_type.metadata or {}).get("game_id")
    try:
        return int(gid)
    except (TypeError, ValueError):
        return None


def channel_of_data_type(data_type: DataType) -> str | None:
    """从 DataType 反解 channel;缺失/非本模型返 None。"""
    if data_type.type is not SportsGameUpdate:
        return None
    ch = (data_type.metadata or {}).get("channel")
    return str(ch) if ch else None


class SportsGameStateStore:
    """最新赛事状态真理源(#250)。

    物理存储 = NT Cache 通用对象区(`cache.add/get(key, bytes)`),key/codec 为 Store 私有,
    调用方不得自行拼 key 或解释 bytes。写者:PMSPORTS processor(put)+ client 归零回收(delete)。
    条目生命周期 = 订阅生命周期:该场订阅数归零时 client `_unsubscribe` 调 `delete`
    真删除(依赖 #250 新增的 NT `Cache.delete` API);`ended=True` 条目在删除前充当终态标记。
    """

    def __init__(self, cache) -> None:
        self._cache = cache

    @staticmethod
    def _key(game_id) -> str:
        return f"{_STORE_KEY_PREFIX}{int(game_id)}"

    def get(self, game_id) -> SportsGameUpdate | None:
        raw = self._cache.get(self._key(game_id))
        if not raw:
            return None
        return SportsGameUpdate.from_dict(json.loads(raw.decode("utf-8")))

    def put(self, update: SportsGameUpdate) -> None:
        raw = json.dumps(update.to_dict()).encode("utf-8")
        self._cache.add(self._key(update.game_id), raw)

    def delete(self, game_id) -> None:
        self._cache.delete(self._key(game_id))


class SportsGameDataFilter:
    """附加内容过滤 seam(二级架构占位):决定已订阅比赛的更新是否进入 Store。默认全收。

    主准入门是订阅本身(processor 的兴趣门控);具体字段级规则留后续设计。
    """

    def accepts(self, update: SportsGameUpdate) -> bool:
        return True


def _channel_changed(channel: str, previous, update: SportsGameUpdate) -> bool:
    """#322 逐通道 diff:该通道关注的字段相对旧状态是否变化。`previous is None`(首帧)一律
    视作变化(该场首次入 Store,唤醒订阅者建立初态)。"""
    if previous is None:
        return True
    if channel == SPORTS_CHANNEL_PHASE:
        return sports_phase(previous) != sports_phase(update)
    if channel == SPORTS_CHANNEL_SCORE:
        return previous.score != update.score
    return False


class SportsGameDataProcessor:
    """固定顺序:**兴趣门控(未订阅任何通道:不存不推)** → filter → 终态拒收 → 过期拒收
    → **先写 Store** → 逐通道 diff、变化的通道各自发布(#322,data §3.4.2)。

    错误边界:Store 写失败 → 不发布;publish 失败 → Cache 不回滚。**逐通道 diff**:某帧无任何
    已订阅通道发生变化 → 只刷新 Cache 时戳、不发布(§3.4.1 的"只存不发"按通道推广)。
    **终态**:ended 帧放行恰好一次(eviction 依赖 —— POST 是一次 phase 跃迁而发一次,后续
    ended 帧无跃迁且被终态拒收拦下),覆盖退订命令异步生效前的小窗。
    """

    def __init__(self, *, store, data_filter, subscribed_channels, publish, log) -> None:
        self._store = store
        self._filter = data_filter
        self._subscribed_channels = subscribed_channels  # callable(game_id) -> set[str]
        self._publish = publish                           # callable(update, channel) — client 注入
        self._log = log

    def process(self, update: SportsGameUpdate) -> None:
        channels = self._subscribed_channels(update.game_id)
        if not channels:
            return   # 未订阅任何通道:不存不推(定了就推,不定就不推)
        if not self._filter.accepts(update):
            return
        try:
            previous = self._store.get(update.game_id)
        except Exception as e:  # noqa: BLE001 — 旧值损坏不挡新状态写入
            self._log.warning(f"sports store read game {update.game_id} failed: {e!r}; treat as absent")
            previous = None
        if previous is not None and previous.ended:
            return   # 终态:ended 帧已放行过一次(eviction 依赖),后续该场所有帧准入层拒收
        if previous is not None and update.ts_event < previous.ts_event:
            return
        # 逐通道 diff:只发"已订阅 且 本通道关注字段变化"的通道(确定性顺序)。
        to_publish = [
            ch for ch in _SPORTS_CHANNELS
            if ch in channels and _channel_changed(ch, previous, update)
        ]
        try:
            self._store.put(update)
        except Exception as e:  # noqa: BLE001
            self._log.error(f"sports store write game {update.game_id} failed: {e!r}; not published")
            return
        for ch in to_publish:   # 空 → 只存不发
            try:
                self._publish(update, ch)
            except Exception as e:  # noqa: BLE001 — Cache 已是新状态,不回滚
                self._log.error(f"sports publish game {update.game_id} channel {ch} failed: {e!r}")


class PolymarketSportsDataClient(LiveMarketDataClient):
    """PM Sports WS firehose → #250/#322 状态管线(兴趣门控 → 先写 Store → 逐通道变化发布)。

    无 instrument 订阅:`_connect` 即开 WS 流式收;消费者按 (场,通道) 经
    `subscribe_data(sports_data_type(game_id, channel), client_id=ClientId(SPORTS_CLIENT))`。
    服务端协议层 ping 由 NT WebSocketClient 自动回 pong;另兼容偶发 app-level text
    `"ping"`(回 `"pong"`)。
    """

    def __init__(
        self,
        loop,
        msgbus,
        cache,
        clock,
        instrument_provider,
        config,
        *,
        data_filter: SportsGameDataFilter | None = None,
    ) -> None:
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
        self._proxy_url = getattr(config, "proxy_url", None)
        self._ws_client: WebSocketClient | None = None
        self._ws_task: asyncio.Task | None = None
        self._update_instruments_task: asyncio.Task | None = None
        # #250:兴趣注册表直接复用 NT 原生订阅记账(client 基类 `_add_subscription` 同步维护,
        # engine 首订转发/归零退订;归零时序依赖本轮 engine 归零判断修复)。
        # 定了就推(先写 Store 再发布),不定不存不推。
        self._sports_store = SportsGameStateStore(cache)
        self._processor = SportsGameDataProcessor(
            store=self._sports_store,
            data_filter=data_filter or SportsGameDataFilter(),
            subscribed_channels=self._subscribed_channels,
            publish=self._publish_update,
            log=self._log,
        )
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
        if self._ws_client is not None:
            await self._ws_client.disconnect()
            self._ws_client = None

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
        while True:  # 初连失败重试;连接后的断线重连由 NT WebSocketClient 负责
            try:
                config = WebSocketConfig(
                    url=self._ws_url,
                    headers=[],
                    heartbeat=None,
                    reconnect_timeout_ms=30_000,
                    proxy_url=self._proxy_url,
                )
                self._ws_client = await WebSocketClient.connect(
                    loop_=self._loop,
                    config=config,
                    handler=self._on_ws_message,
                    post_reconnection=lambda: self._log.info("Sports WS reconnected"),
                )
                self._log.info("Sports WS connected")
                return
            except asyncio.CancelledError:
                self._log.debug("Sports WS task canceled")
                return
            except Exception as e:  # noqa: BLE001 — 重连,不让单次异常杀任务
                self._log.warning(f"Sports WS error: {e!r}; reconnecting in 5s")
                await asyncio.sleep(5.0)

    def _on_ws_message(self, raw: bytes) -> None:
        s = raw.decode("utf-8", "replace").strip()
        if s == "ping":
            if self._ws_client is not None:
                self.create_task(self._ws_client.send_text(b"pong"))
            return
        self._on_frame(s)

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
                self._processor.process(update)

    def _publish_update(self, update: SportsGameUpdate, channel: str) -> None:
        # CustomData 经 DataEngine 路由到 (场,通道) topic
        # (`data.SportsGameUpdate.game_id=<gid>.channel=<ch>`)。
        self._handle_data(CustomData(sports_data_type(update.game_id, channel), update))

    # ── NT per-(game,channel) 订阅(#250/#322)────────────────────────
    # 上游是无订阅 firehose,"断某场某通道的 feed" = 订阅注册表移除(对标 OE 路由表)。
    # 兴趣记账由 NT client 基类原生维护(`subscribed_custom_data()`,engine 首订转发/
    # 归零退订时同步更新);`_subscribe/_unsubscribe` 只补日志与归零回收。
    def _subscribed_channels(self, game_id) -> set:
        """#322:该 game 当前被订阅的通道集合(扫 NT 原生订阅注册表按 game_id 过滤)。"""
        gid = int(game_id)
        out: set = set()
        for dt in self.subscribed_custom_data():
            if game_id_of_data_type(dt) != gid:
                continue
            ch = channel_of_data_type(dt)
            if ch:
                out.add(ch)
        return out

    def _is_game_subscribed(self, game_id) -> bool:
        return bool(self._subscribed_channels(game_id))

    async def _subscribe(self, command) -> None:
        gid = game_id_of_data_type(command.data_type)
        if gid is None:
            self._log.warning(f"PMSPORTS ignoring subscribe for unsupported data type: {command.data_type}")
            return
        self._log.info(f"PMSPORTS subscribed: game {gid} channel {channel_of_data_type(command.data_type)}")

    async def _unsubscribe(self, command) -> None:
        gid = game_id_of_data_type(command.data_type)
        if gid is None:
            return
        ch = channel_of_data_type(command.data_type)
        # 基类 unsubscribe 先 `_remove_subscription` 再调本钩子(live/data_client.py:262)→
        # `subscribed_custom_data()` 已不含本通道。该 game 仍有别的通道订阅 → 不回收 Store
        # (别的消费者还需要);全部通道归零才回收。
        if self._subscribed_channels(gid):
            self._log.info(f"PMSPORTS channel {ch} unsubscribed for game {gid}; other channels remain")
            return
        try:
            self._sports_store.delete(gid)
        except Exception as e:  # noqa: BLE001 — 回收失败不影响退订本体
            self._log.warning(f"PMSPORTS store reclaim for game {gid} failed: {e!r}")
        self._log.info(f"PMSPORTS game unsubscribed (all channels zero), store reclaimed: {gid}")


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
