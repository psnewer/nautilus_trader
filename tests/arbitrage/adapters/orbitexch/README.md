# OE 适配器测试

OE **没有上游适配器,全部自写**。本目录覆盖:
- `OrbitExchInstrumentProvider`(Step 1)
- `PlaywrightBrowserManager` 共享(Q2,Step 1)
- `OrbitExchDataClient(LiveMarketDataClient)`(Step 2)
- `OrbitExchExecutionClient(LiveExecutionClient)`(Step 5)

对应章节: `refactor.md §5.1.2, §5.2.1, §5.5, §6.2`

## 锁定决定

- Q1: InstrumentId = `{market_id}-{selection_id}.ORBITEXCH`
- Q2: 沿用现有 `PlaywrightBrowserManager`,所有权抽到 NT factory 层(共享单例);三方按 page name `"discovery"` / `"data"` / `"execution"` 拿专属 page
- Q9: instrument.info 必含 6 个统一 key
- Q13(2026-05-19): OE adapter 内部承担健康检查,**吸收原 OE 网页监控**;两个刷新触发并存(时间维度 + `leg_settled=false`);**刷新后的持仓/挂单数据走 NT 标准 report 通路**(`generate_position_status_report` / `generate_order_status_report`),与 PM 对齐;execution session 单一职责(cancel-only 或 submit+track,都 track 到 terminal);移除 recovery loop。详见 `refactor.md §6.8`。⚠️ **2026-06-15 修正**:`leg_settled` 退役,状态维度改由 `VenueExecutionLiveness` 的 order/position alive + Risk gate 承担。
- Q17(2026-05-19): **健康检查不碰余额** —— OE 余额走 WS 被动推(reactive,**已含挂单占用**),健康检查只保 WS/页面活;OE 无 REST 不拉余额。可用余额 `_check_balance` 直接信 WS 上报值(不再减),与 PM 自扣非对称

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_browser_manager_sharing.py` | Q2 验收: 三方共享 manager + page 命名 + 生命周期 |
| `test_provider.py` | OE Provider 的具体行为(Q9 字段 / 抓取失败 / page 命名 / 最小 stake 元数据) |
| `test_data_client.py` | OE DataClient(Step 2 占位,详细等 Step 2 启动) |
| `test_execution_client.py` | OE ExecutionClient(Step 5 占位,详细等 Step 5 启动) |
| `test_execution_translation.py` | **Gap C(#63/#64)纯映射,20 case**:① `nt_order_to_legacy_order`(NT Order→executor 旧 Order)5 case:BUY→BACK / SELL→LAY / `null_handicap`(-9999999.0 sentinel)→0 / 真 handicap 保留 / 缺 market\|selection→None。② **`current_bets_to_fills`(成交回执)7 case**:空→[] / unmatched→[] / 新成交→full delta / 增量(5→8)→delta=3 / 同累积值→[] / 无价(avg=0)→跳过 / 缺 offerId→跳过。③ **`bet_order_progress`(reconcile 派生)8 case**:缺 offerId→None / 仅 remaining→accepted / remaining+matched→partially_filled / 仅 matched→filled / 都 0→unknown / **bet 自带 side+market+selection+price 透出** / **原始量优先 sizePlaced** / 无 sizePlaced→matched+remaining 兜底。真 `executor.place_order` + `_connect` + `_on_current_bets`→`generate_order_filled` + `generate_order_status_report(s)`(**bet 自带 `side`/`sizePlaced` 直接派生,`market+selection` 反查 instrument → 外部/重启单也能 reconcile**)= **真钱,/live-test 经 `launchers/arb_node.py` 验**(scenario 跑老栈不验 NT client);matched 帧填充值待真成交 |
| `test_data_factory_provider_wiring.py` | **slice 7A(#46)**:`OrbitExchLiveDataClientFactory.create` 按 `ArbContext.oe_scraper_config` 分支(缺→`InstrumentProvider()` 占位 / 有→真 `OrbitExchInstrumentProvider(scraper, aliases)`)|
| `test_data_client_inplay_writeback.py` | **slice 9(#49)**:`write_inplay_to_instrument_info(cache, iid, in_play)` module 级 helper(`_on_price_frame` 路径 NT 重,_cache cdef readonly Mock 困难,验 helper 即可)。case:present True / present False / cache 缺 instrument 不 raise / info=None 不 raise |
| `test_data_client_step2.py::test_update_instruments_continues_after_provider_error` | **2026-06-29 overnight 修**:OE 周期 instrument rediscovery 单轮 `load_all_async` 抛异常后 task 不退出,下一轮仍继续并成功 `_send_all_instruments_to_data_engine` |

## Slice 10c smoke 浮上(#51):OE live connect 接线修

- **`BrowserManager.start()` 幂等化**(`if self._context is not None: return`):共享 BrowserManager 被多次 client 触发 start 时不重复 init Playwright。
  - **oe-adapter-2.5 并发安全(2026-06-21 live SIGABRT 修)**:幂等守卫不防并发——`_context` 在 `await launch()` 完成后才置位,NT 并发连 OE Data+Exec → 两个 `start()` 都越过守卫 → **并发双开 Chromium → crashpad SIGABRT**(`TargetClosedError`,两 client `_connect` 同时挂)。修:`start()` 加 `asyncio.Lock` + 进锁 double-check。用例 `test_browser_manager_sharing.py::test_concurrent_start_launches_browser_once`(3 并发 start → launch 仅 1 次,fake playwright 加 sleep 放大竞态)。若 lock 后仍偶发 SIGABRT → macOS/Playwright 环境问题,非本竞态。
- **`OrbitExchDataClient._connect` 自管 start + 改用 `create_page`**(原 bug:`get_page` 是只读,首次连返 None → `.goto` AttributeError)。设计意图原是 "factory 层先 start",但 factory 未接;DataClient 自管 + 幂等更稳。
- **#66 `skip_execution`=「真连接 + mock 订单 IO」(PM/OE 对齐)**:取代旧的 OE skip `_connect` no-op(那是 Gap C `_connect` 还是 NotImplementedError 时的权宜)。现 skip 下 OE 也真连接(登录/page/general WS/账户状态),只 mock `_submit_order`(全成)+ `_cancel_*`(no-op)。`skip_execution=true` 即「安全验连接路径而不下真单」smoke。
- **#68 每 competition 一页 + 新开/刷新统一**:OE data client 价格订阅从"单 `inplay/highlights` 页"(盘口稀疏根因)改为**每 competition 一页**(key=`{sport_id}_{competition_id}`,从 instrument 的 `event_type_id`/`competition_id` 取)。`_subscribe_order_book_deltas` → `_ensure_competition_page`(**eager 订阅即开**)→ `_open_or_reload_competition_page`(不存在 create_page+挂监听(#67)+goto / 已存在 reload)。competition 页继续用老代码同款 `networkidle`,但必须传 `page_timeout`(`cfg.venues.orbitexch.page_load_timeout_sec` → 默认 120s);30s/60s/90s live smoke 均出现过 timeout。新开页 `goto` 成功后才登记到 `_comp_pages`;失败时 stop handler + close page,避免另一腿复用未加载成功的 page。`test_data_client_step2.py` 覆盖订阅即开 page_key=`comp-1_1` / 同 competition 顺序+并发订阅都只开一页 / 已存在→reload(首 goto 次 reload)/ 不在 cache→不开 / goto 失败不缓存 stale page / 新开页先注册 Playwright WS handler 且不挂 CDP probe。设计 refactor.md #68 + data/architecture.md §3.1。**live smoke 已验 OE 真盘口流入**:`OE price frame routed` + `OE OrderBookDeltas published`;PM proxy 透传修复后已同场验证 PM+OE 双边 OBD 触发 StrategyEvaluator 重评。
- **#87 competition 页可见性**:2026-06-09 DevTools/CDP 复核:前台 OE competition 页会创建 `multiple-market-prices` WS 并推目标 market 帧,但 NT live node 曾出现 competition 页打开摘要先只有 `orders` 的样本。OE 前端会读取 `document.visibilityState` / market 可见性参数订阅价格。修复:`PlaywrightBrowserManager` 固定 document 可见/hasFocus 并让 `IntersectionObserver` 返回可见;`OrbitExchDataClient._open_or_reload_competition_page` 在新开和 reload 前 `page.bring_to_front()`。`test_data_client_step2.py` 断言订阅新开页与 reload 分支都会前置页面。
- **#68/#95/#100 观测锚点**:`OrbitExchWebSocketHandler` 使用调用方 NT logger,输出 `OE WS connected(type=prices/orders/unknown)` 与每类 WS 首帧 `OE WS first frame received(type/kind/bytes)`;`OrbitExchDataClient` 继续用自身 `_log` 输出 competition 页打开后的 WS 摘要(`ws_count/ws_types`)和首个 routed price frame / 首个 `OrderBookDeltas` publish。这样 live 中用 Playwright handler 这一条运行锚点区分“prices WS 没被捕获 / 捕获但无下行 / 已有下行但解析或路由未进 DataClient”。#95 曾附加同页 CDP `Network` 旁路探针;#100 按实盘诊断要求撤回 open 时 CDP probe,避免 open/reload 两条路径观测锚点不一致。`test_ws_general_frames.py` 覆盖首帧锚点只打一次;`test_data_client_step2.py` 断言命中 routing 后 `_price_frames_seen/_price_deltas_published` 递增、未订阅 market 不递增,并覆盖新开页先注册 Playwright WS handler 且不创建 CDP session。
- **#67 OE 连接两 bug 修复(live smoke 抓出,连接路径 live 验证通过)**:① **漏关登录后弹窗** —— `_login` 加 `_dismiss_post_login_popup`(弹层盖页 → general WS 不推 BALANCE),#89 校准为“等待 `postLoginPopup` 容器出现后点击主页面区域,timeout 后静默继续”,不再固定 sleep 后点 OK;② **WS 监听注册晚于页面建 WS** —— `page.on('websocket')` 只捕获注册后新建的 WS,`_connect`/data `_connect` 改为 **先 `ws_handler.start()` 再 `goto/_login`**(老 odds_client 注释:"必须在 goto 前挂拦截")。③ exec page timeout 按 `cfg.venues.orbitexch.page_load_timeout_sec` 设置并显式传给 `goto`,避免 BrowserManager 默认 30s 在 OE 首页超时。验证(`launchers/arb_node.py` + skip=true):`popup dismissed` + OE 账户 `0.00 → 37.49 GBP` 真余额 + 两腿 Connected + MatchedPair + 0 ERROR。**Gap C 连接路径(登录/弹窗/general WS/真 BALANCE→账户状态)完整 live 验证**。**Tier 1 探针**:`scripts/gapc_place_cancel_probe.py`(真账户 place+cancel 不成交:LAY@1.01 + OE 最小 stake 7 + 默认 dry-run/`--confirm` 真下单 + 撤单兜底;`--cleanup-only` 零下单 cancel/check;BACK odds 越大越好,`BACK@1.01` 不是保护价)。**2026-06-08 live**:首跑 `_submit_order`→venue_order_id ✓ / CURRENT_BETS working ✓ / reconcile ✓,但 `_cancel_order` 暴露 `missing market_id` bug;残单经 cleanup-only 清理且活单数 0。#77 修 `_cancel_order` 从 instrument 或 CURRENT_BETS 回填 `market_id/selection_id`,并加 `test_cancel_order_passes_market_id_from_current_bets`;#78 修后复跑完整 place+cancel 通过(offerId=221973242:submit/CURRENT_BETS/reconcile/cancel 全 ✓,cleanup-only 复查活单数 0)。**Tier 2(真成交)✅ live 验完成(#82)**:`scripts/gapc_fill_probe.py`(市价 BACK@1.01 TAKER、最小注、只下单+报告不撤不对冲)真下成交单 offerId=222016509(BACK Roberto Bautista Agut £7)。**真实 matched 帧**:`sizeMatched=7.00`/`averagePrice=2.3`(BACK@1.01 在最优 back 赔率 2.3 成交,价格改善)/`sizeRemaining=0.00`;`bet_order_progress` 派生 `filled/7.0/2.3` ✓。**`generate_order_filled` 探针内 0 次=探针无 ExecEngine 局限**(`OrderAccepted` 未 apply→cache 无 voi 索引→`_on_current_bets` 反查 None 跳过),非 bug;事件路径离线补验 `test_orbitexch_client.py::test_on_current_bets_matched_fires_generate_order_filled`(预置 `add_venue_order_id`→fire last_qty=7/last_px=2.3/MAKER)。**MAKER 硬编码已评估无害(#83)**:`_on_current_bets` 无条件 `liquidity_side=MAKER` —— OE 无 maker/taker 概念(博彩交易所,CURRENT_BETS 无该字段;那是 PM CLOB 的)、fill `commission=0`、outcome 指标在 strategy/risk/portfolio 层算不读此字段 → 纯名义不改。详见 execution §4.3 Gap C 分档。

## Slice 7A 浮上(#46):scraper 浏览器自管 known divergence

- OE `OrbitExchScraper` 自管 Playwright lifecycle(独立 browser 进程,无登录共享)
- OE Data/Exec client 走 `BrowserManager.get_page("data"/"exec")`(Q2 / §6.2)
- **discovery 是第三方,Q2 原本只覆盖 Data+Exec**;scraper 跑 unauthenticated 看 competition list 够用
- 若后续 discovery 需要登录状态(私有赛事 / 用户偏好),拆 slice 7C:refactor scraper 接 `BrowserManager.get_page("discovery")`

## #62(2026-06-04):两个可见窗口修复 —— data/exec 真共享 + scraper headless

用户报"弹两个浏览器,一个停在主页"。根因:§6.2/Q2"data+exec 共享"**未落地**——`factories.py` 的 data/exec factory **各 `new` 一个 `PlaywrightBrowserManager`**;scraper 又自起一套(Slice 7A 已知),且跟随 venue `headless`(=false 可见)+ 用 data/exec 的登录 `user_data_dir`。修:
- **data+exec 共享单例**:`ArbContext.oe_browser_manager` + `_shared_oe_browser_manager(ctx,config)`(data 先建回写、exec 复用)→ 一个登录浏览器。
- **scraper 解耦 headless**:`to_oe_scraper_config` 强制 `headless=True` + `user_data_dir=None`(免登录、非持久化、后台隐身)—— **不与 data/exec 共享**(用户:免登录 + 定时跑,共享会打断登录会话/抢资源)。
- 验:`test_dispatcher` 断言 `headless is True`/`user_data_dir is None`;live smoke16 `MatchedPair (oe=2)` 证 headless scraper 发现仍产 OE instrument,0 错误。
- exec 非 skip 真接线(Gap C)后,exec 经共享 BM 取 `"execution"` page,不再多窗口。

---

## 用例

### oe-adapter-2.1: PlaywrightBrowserManager 三方共享(Q2)

**前置**: NT factory 启动构造 `_shared_manager` 单例,注入给三个组件
**输入**: 启动 Provider / DataClient / ExecutionClient
**期望**:
- 三个组件 `_browser_manager is _shared_manager` 全部为 True
- 三个组件分别用 page name `"discovery"` / `"data"` / `"execution"`
- BrowserContext 共享(三方读 cookies/session 是同一组,即同一登录态)
**验收**: 单一 BrowserManager 实例,登录态共享,三方互不污染

### oe-adapter-2.2: BrowserManager 生命周期归属

**前置**: oe-adapter-2.1
**输入**: 任一组件的 `_disconnect`
**期望**: 该组件**不调** `manager.start()` / `manager.close()`,只调 `create_page` / `close_page`
**验收**: 组件释放自己的 page,但 manager 实例不被销毁(由 NT TradingNode 生命周期管理)

### oe-adapter-2.3: 用户 user_data_dir 持久化登录态

**前置**: 配置 `user_data_dir = "./oe_session"`
**输入**: 启动 → 登录 → 关闭进程 → 重启
**期望**: 重启后 BrowserContext 自动加载 cookies,无需重新登录
**验收**: 重启后第一个调用 page 不弹登录页

---

### oe-adapter-1.1: OE Provider 冷启动加载

参考 `discovery/README.md` discovery-1.4。

### oe-adapter-1.2: OE Provider info 6 key (Q9)

参考 `discovery/README.md` discovery-1.5(Q9 关键)。

### oe-adapter-1.2b: OE Provider 最小 stake 元数据

**前置**: `OrbitExchInstrumentProvider._build_legs` 产出 `BettingInstrument`。
**输入**: 任取一条 OE 腿。
**期望**: `instrument.min_notional == 7 GBP`,表达 OE venue 最小 stake;Risk 不另维护 OE 最小额常量。
**验收**: `tests/arbitrage/discovery/test_orbitexch_provider.py::test_build_legs_sets_orbitexch_min_stake`

### oe-adapter-1.3: OE Provider 用 page name "discovery"

参考 `discovery/README.md` discovery-1.x(子用例)。

### oe-adapter-1.4: OE Provider 抓取失败处理

参考 `discovery/README.md` discovery-1.7。

---

### oe-adapter-2.x (Step 2): OE DataClient(✅ Step 2 主体落地,10 passed)
**落地**:`nautilus_trader/adapters/orbitexch/data.py` 整体重写 + `factories.py` 加 `OrbitExchLiveDataClientFactory`(`test_data_client_step2.py`)
- ✅ 继承 `LiveMarketDataClient`(取代旧 `LiveDataClient`);type-specific `_subscribe_order_book_deltas`
- ✅ `manager.get_page("data")` 拿 page,**不**调 `start()`/`close()`(共享单例)
- ✅ 输出 NT 标准 `OrderBookDeltas`(取代旧 `QuoteTick`):snapshot CLEAR + N×BACK ADD(BUY) + M×LAY ADD(SELL)
- ✅ WS price 帧解析复用 `OrbitExchMessageParser.parse_price_message`(原有)
- ✅ 路由表 `market_id+selection_id → InstrumentId`,未订阅市场静默丢弃
- ✅ **competition 页存活(#109)**:封装进 WS handler(心跳超时 + close → `on_disconnect`),DataClient 事件驱动 reload、**无 HealthCheckLoop**;见上方"#109 WS 存活封装"用例(旧 #70 HealthCheckLoop 段已失效)
- ⬜ **待 live 接**:scraper DOM 抽 `start_ts`

---

#### #70:OE 健康检查接线(Phase 分期)

§6.8.3 原文写于 #68 拆页前("单页同出赔率 + 持仓/挂单")。#68 后页拆两类:**competition 页**(DataClient,赔率)/ **execution 页**(ExecClient,`CURRENT_BETS`=持仓/挂单)。恢复机制 = **reload 页 → 该页 WS 自然重推**(非 DOM 抓;监听跨 reload 存活 #67)。故:

- **competition 页存活 ✅ 已落地(#109,2026-06-16;#111 feed-specific 修正,2026-06-19,封装进 WS handler,对称 PM)**:`adapters/orbitexch/data.py` —— **无 `HealthCheckLoop` / 无周期 scan**。存活检测封装进 `OrbitExchWebSocketHandler`(传 `clock`/`loop`/`liveness_timeout_secs=staleness_timeout_secs`/`liveness_ws_type="prices"`):只有 prices feed 的数据/心跳更新内部 `_last_frame_ns`,orders/general 心跳不能掩盖 prices WS 未出现或不下发;lazy self-rescheduling NT clock alert 心跳停 → fire `on_disconnect("liveness_timeout")`;prices WS close → `on_disconnect("close:prices")`。DataClient `handler.on_disconnect(...)` → `_on_comp_disconnect` → `_reload_comp_on_disconnect`(冷却 + reload-in-flight + disconnecting 三重防护),**对称 PM `_schedule_delayed_connect`**。connect-retry 事件化:开页失败 → `_delayed_reopen` 延迟重试。**机制差异**:PM 主动 ping、OE 被动盯 prices 入向心跳(接口对称内脏不同)。prices WS 心跳已 live 验证(空闲盘口约 25s,见 data §4.3)。
- **退役(#109/#110)**:`HealthCheckLoop`(OE data 与 PM exec 均不再用)、`_run_health_check` staleness poll、`_mark_comp_frame`/`_comp_last_frame_ns`/`_on_comp_ws_close`(全搬进 handler)。`health_interval_secs` 已从 OE config 接线清理;`staleness_timeout_secs` 改作 handler liveness timeout。
- **Phase 2 状态维度更早已退役(#108)**:`leg_settled` → `VenueExecutionLiveness`(见 oe-adapter-5.liveness)。

**离线用例(#109 WS 存活封装)**:
- handler 内部 liveness(`test_ws_general_frames.py`):
  - **oe-ws-liveness.1**:无 clock/timeout → 内部存活关闭,帧不动锚、不 fire(执行页 general WS 行为不变)(`test_liveness_disabled_without_clock`)
  - **oe-ws-liveness.2**:无帧超 timeout(心跳停=静默死亡)→ fire `on_disconnect("liveness_timeout")`(`test_liveness_fires_disconnect_on_frame_gap`)
  - **oe-ws-liveness.3**:timeout 内有帧 → 重置存活,不 fire(安静市场靠心跳保活)(`test_liveness_frame_resets_no_disconnect`)
  - **oe-ws-liveness.4**:prices close → fire `on_disconnect("close:prices")`(`test_close_prices_fires_disconnect`)
  - **oe-ws-liveness.5**:LiveClock callback 内同名 timer 仍占用 name 时,重排前先 cancel,避免 `timer_names` 冲突(`test_liveness_reschedule_cancels_existing_timer_name`)
  - **oe-ws-liveness.6**:指定 `liveness_ws_type="prices"` 后,orders 心跳不刷新存活锚,prices feed 缺失仍会 timeout(`test_liveness_filters_by_ws_type`)
- DataClient 消费侧(`test_data_client_step2.py`):
  - **data-2.page.8**:competition 页 handler 接线 `liveness_ws_type="prices"`(`test_open_page_liveness_tracks_prices_feed_only`)
  - **data-2.health.11/12/13**:事件化 connect-retry —— `_delayed_reopen` 开成功不再重排 / 仍失败再排 / 关停放弃(`test_delayed_reopen_*`)
  - **data-2.health.14/15**:`on_disconnect`(close:prices / liveness_timeout)→ 调度 reload,跑完页 reload(`test_disconnect_prices_close_schedules_reload` / `_liveness_timeout_schedules_reload`)
  - **data-2.health.16/17**:防护(非 prices/非心跳 reason / 关停 / reload 中 / 页未开 → 不调度)+ 冷却抑制风暴(`test_disconnect_guards` / `_cooldown_suppresses_storm`)
- ~~data-2.health.{1,2,3,5}(staleness poll)+ {4}(执行⊥健康)+ 旧 {14,15,16}(`_on_comp_ws_close`)~~:**已删除(#108/#109)**——staleness 移进 handler liveness(oe-ws-liveness.*),poll/ref-count 退役。

> ⚠️ **失效指针**:下方 oe-adapter-2.health.* 设计意图段是 #105/#108 HealthCheckLoop 时代记录,**已被 #109 handler 封装取代**;现行验收见上方 oe-ws-liveness.* + data-2.health.{11-17}。

---

### oe-adapter-2.health.1: OE 时间维度刷新触发(Q13,§6.8.3)— **Phase 1 ✅,见 #70**

**前置**: OE DataClient 启动并订阅某 competition X;`leg_settled` entry 不存在(未发起过 execution)
**输入**: 让该 competition 页面在阈值时间内**无任何**赔率/订单更新(冻结 mock data 或暂停 page push)
**期望**:
- 每收到 X 的赔率/订单帧时写 `last_update_ns = clock.timestamp_ns()`
- 健康检查 tick 内判 `clock.timestamp_ns() - last_update_ns > 阈值_ns` → 识别 X 时间维度 stale(用 NT clock,不用 wall-clock）
- 走刷新路径: 页面 reload → 等待重新订阅完成 → 拉一次持仓/挂单(**不拉余额**,Q17,余额靠 WS 推)→ **走 NT 标准 report 通路回写(`generate_*_status_report`)**
- 刷新完成后下一个 health tick 不再视为 stale
**验收**:
- 即使在执行前(无 `leg_settled` 状态),时间维度也能独立兜底数据新鲜度
- 静态搜索 OE 健康检查代码: **必须**调 `generate_*_report`,**不得**直接 `cache.update_*`
- 不创建 `leg_settled` entry(本场未曾 execution)
- **staleness 仅在健康检查 tick 评估,不立即刷新**:页面在 t=0 冻结,若上次 tick 在 t=-5、interval=health_check_interval,则在下一个 tick 才发现并刷新(检测延迟 = 阈值 + 最多一个 interval)
- 收帧只更新 `last_update_ns` 变量,**不**触发任何 timer 重设 / 立即刷新

### oe-adapter-2.health.1b: 无独立 staleness 轮询循环(2026-05-19)

**前置**: 检查 OE adapter 实现
**期望**:
- **没有**独立的 `_staleness_monitor_loop` / `asyncio.sleep(30)` 快轮询(旧 `odds_client.py:1472` 折叠进健康检查后删除)
- **没有** per-competition watchdog timer(不为提早发现起 alert)
- staleness 判定全部在健康检查 tick 的 callback 内完成
**验收**: 静态检查无独立 staleness 循环;旧 `_staleness_monitor_task` / `_staleness_check_interval` 在迁移收尾删除

### oe-adapter-2.health.2: OE 状态维度刷新触发(Q13)— **Phase 2 ✅ 已接线 + live reload 已验(#75)**

> ⚠️ **失效(2026-06-15)**:`leg_settled=false` 状态维度退役;新的状态维度由 ExecutionClient reconciliation 写 `VenueExecutionLiveness`,Risk 读取并门控。

**前置**: 已对 competition X 发起过一次 execution,`leg_settled` entry 存在;X 的两个方向中至少一个 `settled=false`(模拟 tracking 漏 WS 帧)
**输入**: 健康检查 tick 触发
**期望**:
- 即使页面**时间维度未 stale**(更新仍在推),也因 `settled=false` 走刷新路径
- 刷新后通过 `generate_position_status_report` / `generate_order_status_report`(**不含余额**,Q17)让 ExecutionEngine reconcile 进 cache + Portfolio + 发 `events.*` topic
- 全部对账完成后,把所有方向 `settled=true`
**验收**:
- 状态维度作为执行后对账兜底独立生效,与时间维度正交
- Strategy 通过 `on_position_event` / `on_order_event` 回调收到(若 venue 端真实状态与 cache 不一致)
- Portfolio.unrealized_pnl 与 venue 真实状态一致

### oe-adapter-2.health.3: 两触发并存,不重复刷新

**前置**: 同 oe-adapter-2.health.2,且同一比赛页面也时间 stale
**输入**: 健康检查 tick
**期望**: 单次 tick 只 reload 一次(去重)
**验收**: 两触发汇聚为一次刷新动作,不并发不重入

### oe-adapter-2.health.4: 全新未交易比赛不参与状态维度

**前置**: competition Y 已被订阅,但从未发起过 execution → 无 `leg_settled` entry
**输入**: 健康检查 tick
**期望**: Y 不进入"状态维度"扫描集(只有 entry 存在的比赛才检查 `settled=false`)
**验收**: 避免对未交易比赛做无谓刷新

### ~~oe-adapter-2.health.5: 执行在飞时 OE 健康检查跳过(Q19 全局互斥)~~ —— 已退役(#108)

> ⚠️ **失效(#108,2026-06-16)**:OE 健康⊥执行互斥退役。competition 页 reload 在另一张页、OE 下单是
> `page.evaluate`(与焦点无关),不冲突 → DataClient `is_execution_active=lambda: False`,不再因执行在飞
> 而跳过 tick;`execution.*` 订阅 + `health_check.*` publish 均删除(见 synchronization §8.6 / refactor #108)。
> 残留观察项:reload 的 `bring_to_front` 短暂背景化执行页 → 回执可能短暂延迟,由 session watchdog +
> venue_liveness 兜,留 live 观察。

---

### oe-adapter-5.live.1: OE order reconcile 写 order_alive(2026-06-15)

**前置**: `OrbitExchExecutionClient` 注入共享 `VenueExecutionLiveness`;OE `order_alive=false`。
**输入**: `_on_current_bets` 收到完整 `CURRENT_BETS` 真实快照,或 order/open-order reconcile 成功并基于该快照生成完整 reports。
**期望**: `oe_order_alive=true`;`_on_current_bets` 因 CURRENT_BETS 同时是 position 真值来源,也会写 `oe_position_alive=true`。
**验收**: 不再调用 `LegSettledRegistry.mark_venue(ORBITEXCH)`;Path B/NT fabricate 事件不置 alive;从未收到 CURRENT_BETS 快照时 report 方法应 mark dead 而不是 mark alive。launcher `LiveExecEngineConfig.open_check_interval_secs=300` 周期触发 OE order reports;WS 新鲜时只读 `_current_bets` 内存,WS stale 时才经 `_ensure_exec_snapshot_fresh` reload execution 页。

### oe-adapter-5.live.2: OE position reconcile 写 position_alive(2026-06-15)

**前置**: OE `position_alive=false`。
**输入**: position reconcile 成功,从 `CURRENT_BETS`/position 视图拿到完整真实 response。
**期望**: `oe_position_alive=true`;若该成功来自新的 `CURRENT_BETS` 快照,`_on_current_bets` 同时恢复 `oe_order_alive=true`。
**验收**: order/position 两个事实位拆分;即使当前来源同为 `CURRENT_BETS`,PM/OE 接口仍保持拆分。

### oe-adapter-5.live.3: OE reconcile 失败置对应 alive=false

**前置**: OE order/position 均 alive。
**输入**: reload timeout、未等到新 `CURRENT_BETS`、或 report 生成确认没有完整真实 response。
**期望**: 对应 `oe_order_alive` 或 `oe_position_alive=false`;持续重试 reconcile。
**验收**: WS 静默只触发探测,不直接判 dead;失败依据是 reconcile 失败。

### oe-adapter-5.live.4: 普通 OE submit 不主动置 alive=false

**前置**: OE order/position 均 alive。
**输入**: 普通 submit+track session 开始。
**期望**: 不因每次下单把 alive 置 false;若后续卡已飞进入 retry/reconcile,再按 5.live.3 置 false。
**验收**: liveness 只表达执行真相可信度,不表达“当前有执行在飞”。

---

> **schedule 系列实现基于 NT `Clock`**(§6.8.4.5):自重排 one-shot time-alert(`clock.set_time_alert_ns`),时间读 `clock.timestamp_ns()`,下次时间查 `clock.next_time_ns(name)`,关停 NT 自动 `cancel_timers()`。**不**用 `asyncio.Event` / `time.monotonic()`。

### oe-adapter-2.schedule.1: 每轮结束重排下次检查 alert(§6.8.4.5)

**前置**: OE adapter 健康检查启动,`health_check_interval_sec = 60`,t=0 完成第一轮
**输入**: 等到 t=60 第二轮自然 fire 并完成,t=120 第三轮
**期望**:
- 每轮 callback 的 `finally` 里调 `clock.set_time_alert_ns(name, now_ns + 60s, override=True)`
- `clock.next_time_ns("health_check_orbitexch")` 在各轮稳定指向下一个 fire 点
**验收**: 节奏不漂移;每轮**结束**(callback finally)才排下次 alert(不是开始)

### oe-adapter-2.schedule.2: 运行时改 interval 下一轮即时生效

**前置**: 初始 interval=60,t=0 第一轮结束后 alert 排在 t=60
**输入**: t=10 时通过配置 API 改 `health_check_interval_sec = 30`
**期望**:
- 第二轮仍按 t=60 fire(alert 已排定)
- 第二轮 callback 末尾 `set_time_alert_ns(now_ns + 30s)`(读当前 config)
- 第三轮在 t ≈ 90 fire(60 + 30)
**验收**: 不需重启;`_schedule_next()` 每次读 config → 下一轮即时生效

### oe-adapter-2.schedule.3: trigger 立即唤醒,执行完按当前 config 重新规划

**前置**: alert 排在 t=60,当前 t=20
**输入**: 外部调 `trigger_health_check()`
**期望**:
- `clock.set_time_alert_ns(name, now_ns, override=True)` → NT past/now 即时 fire(`component.pyx:333`)
- 立即执行一次健康检查(t=20)
- callback 末尾重排 alert 到 `20 + interval`(从当下起算,覆盖原 t=60)
**验收**: trigger 立即生效;后续周期从 trigger 完成时刻起算;`override=True` 覆盖原 alert 不冲突

### oe-adapter-2.schedule.4: 异常路径也重排 alert

**前置**: 一轮健康检查内部抛异常(模拟 Playwright 失败)
**输入**: 异常在 callback 内抛出
**期望**: callback 的 `try/finally` 保证 `finally` 里 `_schedule_next()` **仍然**执行,排下一次 alert
**验收**: 避免一次失败让健康检查永久停摆;静态检查 callback 有 try/finally 包裹

### oe-adapter-2.schedule.5: 不实现 block/unblock API(§6.8.4.5)

**前置**: 检查 OE adapter 健康检查实现
**期望**:
- 类上**没有** `block_health_check` / `unblock_health_check` / `is_health_check_blocked` 方法
- 实例上**没有** `_health_check_blocked` / `_blocked` 字段
- status dict 输出**没有** `"blocked"` 字段
**验收**: P6 不超前实现;旧 `services/risk/service.py` 中对应符号 Step 5/6 实施时一并删除

---

### oe-adapter-5.x (Step 5): OE ExecutionClient

**待 Step 5 启动时展开**。预期范围:
- 继承 `LiveExecutionClient`
- `_submit_order` / `_cancel_order` / `_modify_order` 实现
- 事件回写: `generate_order_submitted` / `generate_order_filled` 等
- 通过 `manager.get_page("execution")` 提交订单
- WS USER channel 订阅订单状态(如 OE 提供;否则轮询)
- Reconciliation: `generate_order_status_reports` / `generate_position_status_reports`

### oe-adapter-5.liveness(#105 A1):exec 页 WS 存活锚(§4.3bis(4))
**✅ 已落地 2026-06-13;close stale 已落地 2026-06-18**。前置:`OrbitExchWebSocketHandler.on_frame` 回调注册;输入:`_on_frame_received` 收到 empty / `'h'`(SockJS 心跳)/ `a[...]`(业务)帧;步骤:非空帧(含心跳)在分型前触发 `on_frame` 回调 → ExecClient `_mark_exec_frame` 刷 `_last_frame_ns`;`_exec_ws_fresh()`(idle=300s)判新鲜。期望:心跳/业务帧触发、empty 不触发;刚刷→fresh、超 idle→stale、从未收→stale。新增 close 语义:`close:orders`(general WS)只把 exec freshness 置 stale,不主动 reload / 不直接 mark dead;execution 页 `close:prices` 不影响 exec freshness。**验收**:`test_orbitexch_client.py::test_handler_on_frame_fires_for_heartbeat_and_data_not_empty` / `test_exec_ws_fresh_lifecycle` / `test_exec_ws_orders_close_marks_stale` / `test_exec_ws_prices_close_does_not_mark_stale`。reports 入口已由 reload-then-report 用例消费该锚点。

### oe-adapter-5.connect-first-frame(#105/#110 startup)
**✅ 已落地 2026-06-18**。前置:ExecClient `_connect` 已注册 `OrbitExchWebSocketHandler.on_frame`;输入:execution 页导航后 WS 首个非空帧到达。步骤:`_mark_exec_frame` 刷 `_last_frame_ns` 并唤醒 `_connect` 的有限等待;期望:`_connect` 返回前 exec WS 至少开始流动,避免 NT startup reconciliation 在 DOM ready 但 WS 首帧未到时误触发 reload-then-report。该等待不写 `VenueExecutionLiveness`;`CURRENT_BETS` 真值仍由 reports/reconciliation 判定。**验收**:`test_exec_first_frame_resolves_connect_waiter`。

### oe-adapter-5.liveness-current-bets(#108):_on_current_bets → VenueExecutionLiveness
**✅ 已落地 2026-06-15**。前置:`OrbitExchExecutionClient` 注入共享 `VenueExecutionLiveness`;输入:任一 CURRENT_BETS 帧(`_on_current_bets([])`);步骤:`_on_current_bets` 缓存完整快照并标记 `ORBITEXCH` 的 `order_alive=true` 与 `position_alive=true`;期望:`venue_alive("ORBITEXCH")` 为 true。**验收**:`test_orbitexch_client.py::test_on_current_bets_marks_oe_liveness_alive`。

### oe-adapter-5.reload(#105 A2):reload-then-report 机制(§4.3bis(3))
**✅ 已接入 reports 2026-06-15**。前置:`_FakePageReload`(记 reload 次数 + 可选 on_reload 模拟 CURRENT_BETS 重推)。用例:① WS 新鲜(`_mark_exec_frame` 后)→ `_ensure_exec_snapshot_fresh` 不 reload(reload_count==0);② 陈旧 → reload + CURRENT_BETS 重推 → True(reload_count==1);③ reload 后 CURRENT_BETS 不重推 → `_reload_exec_page` 超时(`_reload_bets_wait_ns` 调小)→ False(reconcile 失败=venue dead);④ 两并发 `_ensure_exec_snapshot_fresh` → single-flight 只 reload 一次;⑤ 历史 `_current_bets` 存在但 WS stale 且 reload 失败 → `generate_order_status_report(s)` / `generate_position_status_reports` 不再沿用陈旧快照,按维度 mark dead;⑥ reload 成功 → reports 继续保持 alive。**验收**:`test_orbitexch_client.py::test_ensure_fresh_skips_reload_when_ws_fresh` / `_reloads_when_stale_and_succeeds` / `test_reload_exec_page_timeout_returns_false` / `test_ensure_fresh_single_flight_one_reload` / `test_reconcile_reports_stale_snapshot_reload_failure_marks_dead` / `test_reconcile_reports_stale_snapshot_reload_success_stays_alive`。

### oe-adapter-5.position(#105):CURRENT_BETS → 持仓聚合(§4.3bis(2))
**✅ 已落地并接入 reconciliation(2026-06-15/#108,2026-06-16/#110/#111)**。前置:`_current_bets` 快照(BACK/LAY bet,带 `sizeMatched`/`averagePrice`/`marketId`/`selectionId`);算法:纯函数 `current_bets_to_positions` 按 selection 聚合 `net=ΣBACK_matched−ΣLAY_matched`、`avg_px=主方向(net 符号侧)成交量加权 averagePrice`(反向当平仓只减 qty),`net==0`/matched=0 跳过;`generate_position_status_reports` 经 `_resolve_oe_instrument(market_id,selection_id)` 反查 instrument → `PositionStatusReport(quantity, position_side, avg_px_open)`。**验收**:`test_orbitexch_client.py::test_positions_single_back_long` / `_two_back_size_weighted_avg` / `_mixed_back_lay_dominant_side_avg` / `_lay_dominant_short` / `_net_zero_skipped` / `_unmatched_skipped` / `test_generate_position_status_reports_aggregates`;reports 入口另由 reload-then-report 用例覆盖。launcher 当前 `reconciliation=True` 且 `open_check_interval_secs=300` / `position_check_interval_secs=300` 全局开启。

**`general` 频道帧格式(2026-05-22 实测抓帧锁定)**:`prices` 与 `general` 两个 WS;`general`(SockJS 下行 `a[...]`)**承载多类帧,按顶层 key 分型**(`message_parser.parse_general_frame` 已实现,`test_ws_general_frames.py` 覆盖):
- `{"BALANCE":{"balance":"37.49","avBalance":null}}` —— `balance` 是**字符串**,WS 已含挂单占用。
- `{"CURRENT_BETS":[<bet>,...]}` —— 当前注单(抓到样本为空 `[]`)。
- payload 兼容:顶层 key 下可能再包 JSON 字符串;parser 会解嵌套 JSON,校验 `BALANCE` 为 dict / `CURRENT_BETS` 为 list,并过滤非 dict bet item,避免 callback 因字符串 payload 抛 `'str' object has no attribute 'get'`。
- 上行订阅请求 `["{...subscribe:true...}"]`(无 `a`/无数据);**未知 key 帧时不时收到 → 忽略**。

### oe-adapter-5.ws.1: 订单帧解析 → generate_order_*(envelope 已解,**item schema 待 populated 抓帧**)
**前置**: `parse_general_frame` 已能分型 + 透传 `CURRENT_BETS` 列表(envelope 已确认、已测)
**输入**: 一条非空 `CURRENT_BETS` 帧
**期望**: OE ExecClient `_on_current_bets_frame` 把每个 bet → `generate_order_*` / position report 回写 NT 标准管道。**(#108:原"经 `generate_order_*` 置 `leg_settled`"已退役;执行健康改由 `_on_current_bets` / reports 写 `VenueExecutionLiveness`,见 5.liveness-*)**
**验收**:
- **已**(✅):envelope 分型 + 列表透传(ws.1 解析层)
- **待**:单 bet item 字段映射 —— 工作假设与 REST `/customer/api/currentBets` `bets[]` 同源(`marketId`/`selectionId`/`sizeMatched`/`averagePrice`/`side`/...),**需 populated 抓帧确认**后实写 bet→OrderStatusReport 映射

### oe-adapter-5.client: OrbitExchExecutionClient 骨架(✅ 离线核心已测,集成 /live-test)
**落地**: `nautilus_trader/adapters/orbitexch/execution.py`(`tests/arbitrage/execution/test_orbitexch_client.py`)
- **已测(离线)**: 离线可构造(super 只需 instrument_provider + 标准 NT 依赖);`_on_general_frame` BALANCE→`generate_account_state`(`oe_balance_to_account_balances`:WS 已净挂单→total=free GBP)、未知/null 忽略;`_modify_order`→`generate_order_modify_rejected`(OE 不支持改单);`_submit_order` session 门控(cancel-only 丢弃 / executor 失败→reject)
- **oe-adapter-5.client.popup.1(#89)**:登录后弹窗关闭策略。前置:`_login` 已进入 `/customer/`;输入:弹窗容器 `div[class*="_postLoginPopup_"]` 在 timeout 内出现;步骤:`_dismiss_post_login_popup` 等容器可见后调用 `page.mouse.click(24,160)` 点主页面区域;期望:不点 OK 按钮,记录 dismiss。**验收**:`test_dismiss_post_login_popup_clicks_main_page_when_popup_visible`。
- **oe-adapter-5.client.popup.2(#89)**:无弹窗 / timeout 分支。前置同上;输入:等待弹窗容器超时;步骤:`_dismiss_post_login_popup`;期望:不抛异常、不点击页面、继续连接。**验收**:`test_dismiss_post_login_popup_timeout_continues_without_click`。
- **oe-adapter-5.client.pagelock(#105)**:页锁串行碰页操作。前置:ExecClient 注入慢 `_FakeExecutor.cancel_order`(0.02s sleep + 记录并发数);输入:`asyncio.gather` 两笔 `_cancel_order`(O-1/O-2);步骤:两笔并发经 `_page_lock` 进 `executor.cancel_order`;期望:executor 调用**永不并发**(`max_concurrent==1`)——NT 命令均 create_task 并发,页锁在资源层串行,避免同页并发 placeBets/cancel 丢回执。**验收**:`test_orbitexch_client.py::test_page_lock_serializes_concurrent_page_ops`。
- **#63 Gap C:`_connect` + 翻译 + 撤单结构接通**(原 `NotImplementedError` seam → 实现;仅非 skip 触达):
  - `nt_order_to_legacy_order` 纯映射(`test_execution_translation.py` 5 case)+ `_place_via_executor`(守卫 + executor.place_order)
  - `_connect`:共享 BM `create_page("execution")` + 持久化 profile 未登录才 `_login`(填 user/pwd 等 `/customer/`)+ `OrbitExchExecutor` + `OrbitExchWebSocketHandler.on_order_update(_on_general_frame)` + 初始 account state;`_disconnect` 停 ws_handler(不 close 共享 BM,#62)
  - `_cancel_order`→`_cancel_one`(executor.cancel_order + `generate_order_canceled/_cancel_rejected`;#77 必须带 `market_id`/`selection_id`,优先 instrument,缺则 CURRENT_BETS 回填)/ `_cancel_all_orders`→`cancel_all_unmatched` / `_cancel_residual_one`→`_cancel_one`
  - **订单回执已实写**:`current_bets_to_fills`(CURRENT_BETS 快照→`offerId` 算 `sizeMatched` delta)+ `_on_current_bets`→`generate_order_filled`。**2026-06-06 live 抓帧确认** item schema(`offerId`==venue_order_id 是 join key,修正旧 `marketId` 假设);仅 unmatched 态实测,matched 填充值待真成交。
  - **live 已分档验证**:连接/余额、place+cancel、真成交 matched 帧均已验;2026-06-09 `launchers/arb_node.py`
    真执行校准 #85:OE `placeBets` venue 回执返回 offerIds;下一轮 cancel-only 能撤旧 open order,说明
    NT cache 已有 open order + venue_order_id。未成交订单按 Q15 等到 30s 绝对超时,属于当前默认语义。

### oe-adapter-5.ws.2: WS 余额帧 → generate_account_state(✅ 解析层+客户端路由已实现+测)
**前置**: `parse_general_frame` 识别 `BALANCE` 帧 → `{"type":"balance","balance":float,"av_balance":...}`(已测,含 null/字符串/非法值/嵌套 JSON 字符串 payload 健壮)
**输入**: 一条 `BALANCE` 帧
**期望**: OE ExecClient `_on_balance_frame` → `generate_account_state(...)` 写 Cache(待 OE ExecClient 落地接线)
**验收**:
- **已**(✅):`general` 帧捕获 + BALANCE 解析(`balance` 字符串→float)
- **待**:OE ExecClient 把 parsed balance → `generate_account_state`(随 OE 客户端落地)
- OE 余额经 WS 被动维护(对齐 §5.5/§5.6 "被动 WS" + Q17);cache 余额 = WS 上报值,`_check_balance` 直接信不再减;`get_balance()` 页面抓取作过渡兜底,权威源是 WS 帧

---

> **2026-06-19 协调修正**:带完整 opportunity metadata 的多腿套利,跨 venue cancel-only 归 `ArbLiveExecutionEngine` barrier 统一判定(见 synchronization §8.4bis / execution §3.5)。本节 OE per-client cancel-only 只验无 metadata / fallback 的单 instrument 行为。

### oe-adapter-5.session.1: cancel-only session(残留挂单)(Q13)

**前置**: instrument I 上有未成交残留挂单(该 pair 已发起过 execution)
**输入**: strategy 调 `submit_order(new_order)`,execution 入口检查到残留
**期望**:
- session 退化为 cancel-only,**丢弃 `new_order`**(不进队列,不延后下发)
- 撤掉残留 → track 到 CANCELED terminal(#108:不再写 `leg_settled`;残单撤单纳入 `PairInFlightGate.exec_count`,见 synchronization §8.4)
- strategy 端不到下一轮重发,系统不替它补发
**验收**: cancel session 单一职责,submit 被显式丢弃
- **日志锚点(#85)**:`Execution session cancel-only` 打印新单 `client_order_id` 与残留单
  `client_order_id/venue_order_id`,用于 live 确认下一轮确实先撤旧 open order。

### oe-adapter-5.session.2: submit+track session(无残留)(Q13)

**前置**: instrument I 无残留挂单
**输入**: strategy 调 `submit_order(order)`
**期望**:
- session 走 submit+track: 下单 → 等成交 terminal
- 命中 FILLED / CANCELED / REJECTED 任一 terminal → `_end_session`(#108:不再写 `leg_settled`;执行健康由 reconcile 写 `VenueExecutionLiveness`)
**验收**: track 必达 terminal 才算 session 结束

### oe-adapter-5.session.3: 移除 recovery loop(Q13)

**前置**: 任一 session
**输入**: tracking 收到 terminal 后
**期望**:
- execution 不**再启**新一轮 plan / retry
- 不存在 "execution 内部撤后再下" 路径(strategy 后议补救)
**验收**: 静态检索 execution 代码无 recovery 循环;关联 `bug_compensating_cancel_missing` 留在 strategy 设计 TODO

> ⚠️ **失效横幅(#108,2026-06-15):本节及以下所有 `leg_settled` 状态机用例(oe-adapter-5.health.1 ~ 5.timeout.*)全部退役**。`LegSettledRegistry` 已删除;执行健康真值改由 `OrbitExchExecutionClient` 写 `VenueExecutionLiveness`(order/position alive),Risk 读取门控。现行验收见上方 **oe-adapter-5.liveness-current-bets / 5.liveness-reload(#108)**。以下保留为迁移前记录,迁移时删除或改写。

### oe-adapter-5.health.1: leg_settled 状态机(Q13,语义=通讯通道存活信号)

**前置**: 首次对 competition X 发起 execution(2-way 比赛)
**输入**: execution 启动 → tracking 收到 home 任一确认事件(`OrderAccepted` / partial `OrderFilled` / full `OrderFilled` 任一即可)→ away 完全无响应 → OE health check 触发兜底刷新
**期望**:
- 启动时创建 entry `leg_settled = [false, false]`
- home 任意确认事件落地 cache → `[true, false]`(无论 partial 还是 full)
- away 仍 `false` → health check 看到 → 刷新页面 → 通过 `generate_*_status_report` 同步 → `[true, true]`
- 下一次 execution 启动 → `[false, false]`(重置,不删除 entry)
**验收**: entry 生命周期 = 首次创建后保留,每次 execution 重置;**settled=true 不要求 order terminal**

### oe-adapter-5.health.2: 非 execution 触发的事件无 entry 不创建(Q13 边界)

**前置**: competition Y 从未发起过 execution → 无 `leg_settled` entry;但 venue 端推一笔事件(`OrderFilled` 含 partial / `OrderCanceled` / `OrderAccepted` 等任意 ——场景: 历史挂单延迟成交 / 手动在 venue 端下单 / 上一会话遗留)
**输入**: OE WS 帧 / Playwright CDP 拦截到该帧
**期望**:
- adapter 调 `generate_order_*(...)` 进 NT 标准管道
- ExecutionEngine.reconcile 写 `cache.orders` +(若 fill)派生 Position → `cache.positions`
- Strategy 通过 `on_order_*` / `on_position_event` 收到通知
- **`leg_settled` entry 不被创建**(无 execution 历史)
**验收**:
- cache 与 Portfolio 一致(NT 标准路径)
- Strategy 收到事件
- 静态检查 `leg_settled` 集合:Y 不在其中

### oe-adapter-5.health.3: 非 execution 触发的事件命中已有 entry 时置 settled=true(Q13 边界)

**前置**: competition X 已 execution 过 home / away,`leg_settled[X] = [true, true]`;之后又一次 execution 启动,`leg_settled[X] = [false, false]`;此刻一笔历史挂单的成交事件迟到(对应 X 的 home 方向,可以是 partial fill)
**输入**: WS 帧到达
**期望**:
- 走 NT 标准管道(同 5.health.2)
- 因为该方向已有 entry,**置 `leg_settled[X][home] = true`**(无论事件是 partial 还是 terminal)
**验收**: entry 集合不变(只更新值,不增删)

### oe-adapter-5.health.4: partial fill 也触发 settled=true(Q13 partial 语义)

**前置**: 首次对 competition X home 方向 execution;`leg_settled[X][home] = false`
**输入**: venue 推一笔 partial `OrderFilled`(order.status → `PARTIALLY_FILLED`)
**期望**:
- adapter 调 `generate_order_filled(...)` 进 NT 标准管道
- ExecutionEngine 推进 Order 状态机到 `PARTIALLY_FILLED`(非 terminal)
- **`leg_settled[X][home] = true`**(基于"任何确认事件"语义,不等 terminal)
**验收**:
- order status 在 cache 中为 `PARTIALLY_FILLED`(确实未 terminal)
- settled 已为 true,下一个 health tick 不再对这条腿触发"无回信"刷新

### oe-adapter-5.health.5: OrderAccepted 也触发 settled=true(Q13 partial 语义)

**前置**: 首次对 competition X home 方向 execution;venue 端 latency 较高,仅返回 `OrderAccepted`,尚未撮合
**输入**: WS 帧推 `OrderAccepted`
**期望**:
- 进 NT 标准管道
- `leg_settled[X][home] = true`(说明 submit 已到 venue,通讯通道存活)
**验收**: 即使没有任何 fill,settled 也已为 true → health check 不再误判"完全没回信"
- **日志锚点(#85)**:`Execution session accepted` 打印
  `client_order_id/venue_order_id/instrument_id`,用于 live 确认 accepted 事件进入 session 漏斗。

---

> **timeout 系列实现基于 NT `Clock` 一次性 time-alert**(§6.8.5):session 启动 `clock.set_time_alert_ns(f"exec_timeout_{coid}", submit_ts_ns + timeout_ns, callback)`;收到 terminal 即 `clock.cancel_timer(...)`;关停 NT 自动 `cancel_timers()`。**不**用 `asyncio.wait_for`。

### oe-adapter-5.timeout.1: submit+track session 早于超时完成(Q15)

**前置**: session timeout 配置 = 30s
**输入**: submit → 5s 时 venue 推 terminal `OrderFilled` (full)
**期望**:
- 收到 terminal → `clock.cancel_timer(f"exec_timeout_{coid}")` 取消超时 alert
- session 在 5s 时正常结束
- timeout callback 未 fire
**验收**: 验证 terminal 抢先 cancel alert,watchdog 不触发

### oe-adapter-5.timeout.2: submit+track session 超时(partial fill 卡住)(Q15)

**前置**: session timeout = 30s;`leg_settled[X][home] = false`
**输入**: submit → 1s 时收到 partial fill(qty=$30,order.status → `PARTIALLY_FILLED`)→ 之后 venue 不再推任何事件 → 30s timeout
**期望**:
- partial 1s 时:`leg_settled[X][home] = true`(通讯通道存活)
- 30s timeout:**session 直接结束**,**execution 不做任何动作**(不撤、不重试)
- order 在 cache 中保持 `PARTIALLY_FILLED` 状态,position 为 $30 long
- Strategy 下一轮调 submit 时,触发 §6.8.5 残留检测 → cancel-only session 撤剩余 $70
**验收**:
- timeout 路径无任何 `_cancel_order` / `_submit_order` 调用
- 静态搜索 execution timeout 处理代码:仅 log + 结束 session
- 状态自洽:cache.order = `PARTIALLY_FILLED`,position = $30,settled = true,strategy 下轮闭环

### oe-adapter-5.timeout.3: submit+track session 超时(零 venue 响应)(Q15 极端,默认契约)

**前置**: session timeout = 30s;`leg_settled[X][home] = false`
**输入**: submit → venue 端无任何响应(可能 submit 没到 / WS 死)→ 30s timeout
**期望**:
- 默认 30s timeout:session 结束,无任何补救
- `leg_settled[X][home]` 仍 = false(从未收到确认)
- 下一个 OE health check tick 看到 `settled=false` → 触发兜底刷新页面 → 通过 `generate_*_status_report` 同步 → settled=true
**验收**:
- 共用 session 默认 timeout 不试图自救,**依赖健康检查兜底**
- 这是"通讯通道死了"的兜底闭环验证

### oe-adapter-5.timeout.4: cancel-only session 超时(Q15)

**前置**: instrument I 有残留挂单;session timeout = 30s
**输入**: strategy 调 submit 触发 cancel-only session → adapter 发 cancel → venue 不推 CANCELED → 30s timeout
**期望**:
- session 结束,**仅 log warning**,不再做任何动作(不重发 cancel、不进新 session)
- order 在 cache / venue 仍可能是 `ACCEPTED` 或 `PARTIALLY_FILLED`
- strategy 下一轮调 submit 时,残留检测仍会触发新一轮 cancel-only session(本质上是"再撤一次")
**验收**: 避免无限循环;依靠 strategy 下轮自然重试

### oe-adapter-5.timeout.5: 超时 alert 不被 partial fill 重设(Q15)

**前置**: session timeout = 30s
**输入**: submit → 5s partial fill → 15s partial fill → 25s partial fill → 30s timeout fire(尽管最后 partial 距 timeout 仅 5s)
**期望**:
- timeout alert 仍在 `submit_ts + 30s` fire,**与最后 partial 时刻无关**
- 绝对超时语义:alert 一次性设在 submit 时刻 + timeout,partial 事件**不调** `set_time_alert`(不重设)
**验收**: 静态检查:partial fill 处理路径无 `set_time_alert_ns` 重设超时 alert 的调用

### oe-adapter-5.timeout.6: accepted 后未成交等待绝对超时(#85 校准)

**前置**: session timeout = 30s;OE `_place_via_executor` 返回 `status=OK + offerIds`;`_submit_order` 成功路径调用 `generate_order_accepted`,订单保持 unmatched。
**输入**: strategy submit → OE 下单 accepted → 30s 内没有 fill/cancel/reject/expire terminal。
**期望**:
- submit+track session 继续等待直到 30s 绝对超时,不因 accepted 提前结束。
- timeout 只结束 session / 释放执行闸,不做 cleanup / retry / recovery。
- 下一轮机会如果发现 cache 仍有 open order,进入 cancel-only,先撤旧单并丢弃当次 submit。
**验收**:
- live #85 复验:两笔 `placeBets` 返回 offerIds 后 30s timeout;下一轮 cancel-only 成功撤
  `222032569`/`222032570`,反证 cache 已有 open order + venue_order_id。
- 诊断候选 `_page_write_lock` / request-response 日志已撤回。
