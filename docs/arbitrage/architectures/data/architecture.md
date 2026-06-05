# Data 组件详细设计

> 设计理由 / 决策史(Q2/Q9 + #34/#35)见初设 `refactor.md §5.2`。
> 冲突时:有把握 → 以本文为准并回写;没把握 → 提出讨论。对应初设 Step 2。

---

## 1. 职责与边界

| 件 | 基类 | 职责 |
|---|---|---|
| PM `PolymarketDataClient` | 上游 | **零代码**(P1)。WS 订阅 → 输出 NT 标准 `OrderBookDelta` |
| `ArbPolymarketLiveDataClientFactory` | `LiveDataClientFactory` | **薄子类**,只为换用 `ArbPolymarketInstrumentProvider`(后者给 PM instrument.info 补 Q9 6-key,matching 必需;#35) |
| `OrbitExchDataClient` | 自写 `LiveMarketDataClient` | WS `multiple-market-prices` 帧 → NT 标准 `OrderBookDeltas`(snapshot CLEAR + ADDs);BACK→BUY 侧 / LAY→SELL 侧;路由 market_id+selection_id → InstrumentId |
| `OrbitExchLiveDataClientFactory` | `LiveDataClientFactory` | 构造 OE data client,共享 `PlaywrightBrowserManager`(§6.2 单例) |

**位置**(refactor.md #33):venue-coupled 全在 `nautilus_trader/adapters/{polymarket,orbitexch}/`(P9 唯一例外)。

**明确不做**:
- ❌ DataClient 不拥 BrowserManager 生命周期(共享单例由 factory 持;`_connect` 只 `get_page("data")`)
- ❌ 不输出旧自研 dict(全 NT 标准 `OrderBookDelta`)
- ❌ 订阅去重不自管(NT `DataEngine` 引用计数自动)

---

## 2. 数据流

```mermaid
flowchart LR
  V[(OE WS multiple-market-prices)] --> WSH[OrbitExchWebSocketHandler]
  WSH -->|parse_price_message| DC[OrbitExchDataClient._on_price_frame]
  DC -->|routing market_id+sel_id → InstrumentId| MAP{命中订阅?}
  MAP -->|是| CONV["oe_runner_to_book_deltas\n(CLEAR + N×BACK ADD(BUY) + M×LAY ADD(SELL))"]
  CONV --> HD["_handle_data(OrderBookDeltas)"]
  HD --> DE[NT DataEngine]
  DE --> C[(Cache.order_book)]
  DE -->|events.data| ST[Strategy 订阅]
  PMWS[(PM WS / 上游)] --> PMUP[Upstream PolymarketDataClient]
  PMUP --> HD2[_handle_data 同标准管道]
  HD2 --> DE
```

要点:
- OE WS 给的是 **snapshot of best N levels**(`bdatb`/`bdatl` 各档),`oe_runner_to_book_deltas` 转为 `OrderBookDeltas`(1×CLEAR + N×ADD)入标准管道。
- BACK ≡ 买入该方向 → BookOrder side = BUY;LAY ≡ 卖出该方向 → side = SELL。
- 未订阅市场的帧静默丢弃(routing 表查不到)。

---

## 3. 接口

### 3.1 `OrbitExchDataClient`(`nautilus_trader/adapters/orbitexch/data.py`)

```python
class OrbitExchDataClient(LiveMarketDataClient):
    async def _connect(self):
        self._page = await self._browser_manager.get_page("data")
        await self._page.goto(f"{base_url}/customer/inplay/highlights")
        self._ws_handler = OrbitExchWebSocketHandler(self._page)
        self._ws_handler.on_price_update(self._on_price_frame)
        await self._ws_handler.start()
    async def _disconnect(self):           # **不**关 browser(共享)
        if self._ws_handler: await self._ws_handler.stop()
    async def _subscribe_order_book_deltas(self, command):   # 命令路由
        self._register_instrument_routing(command.instrument_id)
    def _on_price_frame(self, message):    # WS callback
        parsed = self._parser.parse_price_message(message)
        if not parsed: return
        routing = self._market_to_instruments.get(str(parsed["market_id"]))
        if not routing: return
        ts = self._clock.timestamp_ns()
        for runner in parsed["runners"]:
            iid = routing.get(str(runner["selection_id"]))
            if iid is None: continue
            deltas = oe_runner_to_book_deltas(iid, runner, ts)
            if deltas is not None: self._handle_data(deltas)
```

**routing 表**:`dict[market_id_str, dict[selection_id_str, InstrumentId]]`,订阅时从 `cache.instrument(id)` 读 `BettingInstrument.market_id` / `.selection_id` 建。

**纯映射** `oe_runner_to_book_deltas(instrument_id, runner, ts) -> OrderBookDeltas | None`:模块级,可单测。runner 全空(back+lay 都空 / 全 size<=0)返 None,调用方不 publish 避空簿噪音。

### 3.2 `ArbPolymarketInstrumentProvider`(`adapters/polymarket/arb_provider.py`)

```python
class ArbPolymarketInstrumentProvider(PolymarketInstrumentProvider):
    def _parse_instrument(self, market_info, token_id, outcome):
        instrument = super()._parse_instrument(...)
        if isinstance(instrument.info, dict):
            instrument.info.update(enrich_pm_six_key_info(market_info, outcome))
        return instrument
```

`enrich_pm_six_key_info(market_info, outcome) -> dict` 模块函数,**best-effort seam**:
- `sport` ← `market_info.get("category")`(PM gamma 给的最接近字段)
- `competition` / `home_team` / `away_team` / `selection_role` 暂空字符串(**TODO**:实写需 PM gamma `/events/{event_id}` 调用 + ticker 拆解,参旧 `odds_client.py:255+`)
- `start_ts` 暂置 0(同 TODO)

matching `events_from_instruments` 见 4-key 任一空就跳过该 instrument → **目前 PM 侧不参与匹配**;但**结构完整**:真正 extraction 实写时,下游一行不动。

### 3.3 Factory(`adapters/polymarket/arb_factories.py` + `adapters/orbitexch/factories.py`)

- `ArbPolymarketLiveDataClientFactory.create()`:同上游 `PolymarketLiveDataClientFactory`,只把 provider 替为 `ArbPolymarketInstrumentProvider`(用上游 `PolymarketDataClient` 不变;P1 复用)
- `OrbitExchLiveDataClientFactory.create()`:构造 `PlaywrightBrowserManager` + `OrbitExchDataClient`,instrument_provider 暂用 `InstrumentProvider()` 占位
- **`PolymarketSportsLiveDataClientFactory.create()`(#60)**:构造 `PolymarketSportsDataClient`(bare `InstrumentProvider()` 占位)

### 3.4 `PolymarketSportsDataClient`(`adapters/polymarket/sports.py`,#60)—— 比分 firehose

PM **Sports WebSocket**(`wss://sports-api.polymarket.com/ws`,公开/无订阅/无鉴权/事件驱动稀疏)→ 实时赛事状态。**P11:`SportsGameUpdate` 事件的单一自然归属 = 本生产者**(消费者 matching/strategy 只读)。

- `_connect` 即开 WS firehose(无 instrument 订阅);协议层 keepalive + 兼容 app-level text `"ping"`→`"pong"`;断线重连;`_disconnect` cancel task。
- 每 `sport_result` → `parse_sport_result` → `SportsGameUpdate` → **`msgbus.publish` 裸发到 `data.SportsGameUpdate*`**(同 MatchedPair/InstrumentsRefreshed 的 publish_data 风格;消费者 `msgbus.subscribe("data.{Type}*")` 带 #58 的 `*` 通配)。**注**:`_handle_data` 走 DataEngine.process 只认内置/CustomData,裸自定义 Data 报 "unrecognized type"(#60 smoke 抓出 → 改裸 publish)。
- **映射键 `game_id`** == gamma `event["gameId"]`(`arb_provider` 抽入 `info["game_id"]`,#60 实采证实双向对上);消费者经 game_id 查 pair。
- 消费:**matching** `ended`→eviction(matching §4.4);**strategy** 经 `signal_collector` seam(strategy)。详见 refactor.md §5.9 / #60。

---

## 4. 与横切的咬合

| 横切 | 约束 |
|---|---|
| Q9 6-key | OE Provider 填全;PM 经 `ArbPolymarketInstrumentProvider._parse_instrument` 补(seam,本 slice 落地结构,extraction 待实写)|
| §4.3 OE 健康检查 | 页面 reload 机制宿主 = `OrbitExchDataClient`(它持 page);本文件留 seam,真接线见 execution §4.3 |
| 订阅去重 | NT `DataEngine` 引用计数自动,客户端不自管 |
| **Q11.A Debug 行情掉包**(#39) | `Debug{PM,OE}DataClient`(`src/arbitrage/debug/data_clients.py`)子类化 `_handle_data`,按 `DebugConfig.mock_data(ODDS)` 替换 / 注入;两 factory 读 `ArbContext.debug_config`,`enabled` → 装 Debug 子类,否则装生产。框架只提供 `_maybe_substitute(data) → data|None` 钩子(默认 passthrough),具体替换算法由 user 按 mock_data schema 子类化覆盖。详见 `_cross-cutting/debug-injection.md` |
| **#49 OE 透 inPlay 到 instrument.info**(slice 9) | `OrbitExchDataClient._on_price_frame` 解析 `marketDefinition.inPlay` 后调 module 级 `write_inplay_to_instrument_info(cache, iid, in_play)` → 写 `cache.instrument(iid).info["in_play"]`(NT cache-resident mutable dict)。Strategy `OpportunitySnapshot` 派生 in_play 从这读,**避走 SignalStore 二跳**;PM-only 事件触发评估时仍能读到 OE 最近一次写入值。Helper 防御:instrument 不在 cache → 跳过不 raise(冷启动场景)。详见 `architectures/strategy/architecture.md §3.8` |
| **#51 OE DataClient `_connect` 自管 BrowserManager**(slice 10c smoke 修) | 原设计注释"factory 层先调 `start()`",实际 factory 未接;**slice 10c 改 DataClient `_connect` 自管 `await self._browser_manager.start()`(幂等)+ `self._browser_manager.create_page("data")`**(原 bug:用 `get_page` — 只读,首次连返 None → `.goto` AttributeError)。共享 BrowserManager 仍由 OE Data/Exec/Discovery 三方使用;`start()` 幂等保护重复触发。 |

---

## 5. 落地清单(Step 2)

- [x] OE `LiveMarketDataClient` 子类(整体重写,`data.py`)+ `oe_runner_to_book_deltas` 纯映射 + routing(`tests/arbitrage/adapters/orbitexch/test_data_client_step2.py` 10 passed)
- [x] OE `OrbitExchLiveDataClientFactory`(同目录 `factories.py`)
- [x] PM `ArbPolymarketInstrumentProvider` + `ArbPolymarketLiveDataClientFactory`(seam,extraction TODO)+ enricher 单测 4 passed
- [ ] **TODO Step 2 续**:真 PM 6-key extraction(gamma `/events/{id}` 调用 + ticker 拆解);OE scraper DOM 抽 `start_ts`
- [ ] **TODO**:OE DataClient 接 `HealthCheckLoop`(execution §4.3 页面 reload 机制宿主)
- [ ] /live-test:双 venue OrderBookDelta 全链路 → strategy 收
