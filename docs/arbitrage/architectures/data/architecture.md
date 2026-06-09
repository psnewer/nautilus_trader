# Data 组件详细设计

> 设计理由 / 决策史(Q2/Q9 + #34/#35)见初设 `refactor.md §5.2`。
> 冲突时:有把握 → 以本文为准并回写;没把握 → 提出讨论。对应初设 Step 2。

---

## 1. 职责与边界

| 件 | 基类 | 职责 |
|---|---|---|
| PM `PolymarketDataClient` | 上游 + 本项目小补丁 | WS 订阅 → 输出 NT 标准 `OrderBookDelta`;订阅启动阶段 WS connect 失败时保留订阅并自动重试 |
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
- PM CLOB market WS 由上游 `PolymarketWebSocketClient` 连接;`base_url_ws` 必须是 `.../ws/`,由 client 自行拼接 `market`。项目 dispatcher 兼容旧 `.../ws/market` 配置并归一化。`proxy_url` 由配置显式给出或 loader 从 `POLYMARKET_PROXY_URL` / 系统 proxy env 注入后透传给上游 client;NT pyo3 WS client 不自动读取系统代理,直连 PM WS 在当前网络下会超时。若启动订阅后的第一次 connect 因网络超时失败,`PolymarketDataClient._delayed_connect` 记录 warning 并按至少 5s 间隔重试,避免一次 transient timeout 后永久无 PM 盘口。PM DataClient 也记录首个 `PM OrderBookDeltas published` 低噪声锚点,用于 live smoke 区分"WS 已连"与"盘口已进入 NT 数据管道"。

---

## 3. 接口

### 3.1 `OrbitExchDataClient`(`nautilus_trader/adapters/orbitexch/data.py`)

**页面模型(#68):每 competition 一页,新开/刷新统一**。OE 赔率来自 competition 页(`/customer/sport/{sport_id}/competition/{competition_id}`)的价格 WS —— **不是**单一 `inplay/highlights` 页(那只给概览、不含完整盘口;旧"单页多市场"设计废止)。`BettingInstrument` 已带 `competition_id`(`event.competition_id`)+ `event_type_id`(=sport_id),订阅时即可定位开哪页。

并发约束:同一 `MatchedPair` 会同时订阅 home/away 两腿,因此 `_ensure_competition_page` 必须用 `_comp_pages_lock` 包住 page_key 检查 + 首次 open,避免两个协程在 `create_page` 前同时判断"未开"而双开同一 competition 页。

观测约束:价格 WS handler 的内部日志不一定进入 NT node 日志,DataClient 自身必须记录低噪声锚点:competition 页打开后的 `ws_count/ws_types`,首个 routed price frame,首个 `OrderBookDeltas` publish。live smoke 判断 OE 盘口链路时以这些 DataClient 日志为准。

```python
class OrbitExchDataClient(LiveMarketDataClient):
    async def _connect(self):
        await self._browser_manager.start()          # 幂等(共享单例)
        # 不再开 highlights 页 / 不再起单一 ws_handler;competition 页按订阅 eager 开。
        await self._instrument_provider.load_all_async()   # 发现(用 provider 自己的 scraper 浏览器)
        self._send_all_instruments_to_data_engine()
        # ... _update_instruments 周期 task(slice A,不变)

    async def _subscribe_order_book_deltas(self, command):
        iid = command.instrument_id
        inst = self._cache.instrument(iid)
        page_key = f"{inst.event_type_id}_{inst.competition_id}"   # sport_id_competition_id
        self._register_instrument_routing(iid)                     # 全局 routing(price 帧带 market_id)
        await self._ensure_competition_page(page_key)              # eager:订阅即开页(#61 要赔率流);内部加锁去重

    async def _ensure_competition_page(self, page_key):
        """每 competition 一页 + WS handler(去重);已存在则复用。"""
        if page_key in self._comp_pages:
            return
        await self._open_or_reload_competition_page(page_key, sport_id, competition_id)

    async def _open_or_reload_competition_page(self, page_key, sport_id, competition_id):
        """**新开/刷新统一入口**(对齐老 odds_client `_open_or_reload_page`;#68)。
        page 不存在 → create_page + 挂 WS 监听(#67 先挂后 goto)+ goto;
        已存在 → reload(page-level 监听跨 reload 存活,#67 实测,无需重挂)。
        健康检查 §4.3 reload 将来复用本方法的 reload 分支。"""
        url = f"{base_url}/customer/sport/{sport_id}/competition/{competition_id}"
        # #68:competition 页加载重(走代理 + 页面 JS 建价格 WS 握手),对齐老 odds_client:
        # networkidle + cfg.venues.orbitexch.page_load_timeout_sec → page_timeout,默认 120s。
        timeout_ms = self._config.page_timeout
        page = self._comp_pages.get(page_key)
        if page is None:
            page = await self._browser_manager.create_page(f"comp-{page_key}")
            handler = OrbitExchWebSocketHandler(page)
            handler.on_price_update(self._on_price_frame)
            await handler.start()                     # #67:先挂监听
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)   # 再导航(价格 WS 此时建,被抓)
            # goto 成功后才登记;失败时 stop handler + close page,避免下一腿复用未加载成功的 page。
            self._comp_pages[page_key] = page
            self._comp_handlers[page_key] = handler
        else:
            await page.reload(wait_until="networkidle", timeout=timeout_ms)      # 监听自动捕获新 WS(#67)

    def _on_price_frame(self, message):     # 所有 competition 页共用(routing 按 market_id 分流)
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

    async def _disconnect(self):            # **不**关 browser(共享);停所有 competition 页 handler
        for h in self._comp_handlers.values(): await h.stop()
```

- **routing 表**:`dict[market_id_str, dict[selection_id_str, InstrumentId]]`,订阅时从 `cache.instrument(id)` 读 `BettingInstrument.market_id` / `.selection_id` 建。**全局**(跨所有 competition 页),price 帧自带 market_id 直接分流。
- **页面注册表**:`_comp_pages: dict[page_key, Page]` + `_comp_handlers: dict[page_key, WS handler]`,每 competition 一组。
- **开页时机 = 订阅即开(eager)**:#61 的目的是"`MatchedPair`→订阅→策略拿真实赔率";赔率住 competition 页 WS,故订阅时立即开页,不推迟(老 odds_client 推迟到健康检查)。
- **关页 = 保持打开**(对齐老 odds_client;competition 数量有界,空页成本可接受)。
- **健康检查 reload(§4.3,Phase 1 ✅ 已接线 / Phase 2 ✅ live 验完成)**:本 DataClient 是健康检查宿主。`_connect` 挂 `HealthCheckLoop`;Phase 1 两维度:**时间维度** = `_on_price_frame` 写 `_comp_last_update_ns[page_key]`,`_run_health_check` 发现 `now-last_update>config.staleness_timeout_secs` → 复用 `_open_or_reload_competition_page` 的 reload 分支(赔率防冻);**连接重试维度** = `set(_market_to_page_key.values())-set(_comp_pages)`(已订阅未开)→ 本 tick 补开,失败吞掉、留下一次健康检查重试(对等 PM `_delayed_connect`)。Q19 互斥经 DataClient 订 `execution.*` 自维护 ref-count(`_is_execution_active`)。**Phase 2 ✅ 代码已接(A 方案)+✅ 真实 reload 已验(#75)**:状态维度 `leg_settled.has_any_unsettled()` → `_reload_execution_page()` 经共享 `browser_manager.get_page("execution")` reload 交易页(`leg_settled` 经 factory 注入;安全闸 `config.health_check_exec_reload_enabled` 默认 True,可配置关回)。2026-06-08 真账户零下单探针确认:已登录 execution 页 reload 不重现登录弹窗,且 `CURRENT_BETS` 如期重推。落点/数据源/A vs B 见 execution §4.3。

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
