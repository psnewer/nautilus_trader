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
- Q13(2026-05-19): OE adapter 内部承担健康检查,**吸收原 OE 网页监控**;两个刷新触发并存(时间维度 + `leg_settled=false`);**刷新后的持仓/挂单数据走 NT 标准 report 通路**(`generate_position_status_report` / `generate_order_status_report`),与 PM 对齐;execution session 单一职责(cancel-only 或 submit+track,都 track 到 terminal);移除 recovery loop。详见 `refactor.md §6.8`
- Q17(2026-05-19): **健康检查不碰余额** —— OE 余额走 WS 被动推(reactive,**已含挂单占用**),健康检查只保 WS/页面活;OE 无 REST 不拉余额。可用余额 `_check_balance` 直接信 WS 上报值(不再减),与 PM 自扣非对称

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_browser_manager_sharing.py` | Q2 验收: 三方共享 manager + page 命名 + 生命周期 |
| `test_provider.py` | OE Provider 的具体行为(Q9 字段 / 抓取失败 / page 命名) |
| `test_data_client.py` | OE DataClient(Step 2 占位,详细等 Step 2 启动) |
| `test_execution_client.py` | OE ExecutionClient(Step 5 占位,详细等 Step 5 启动) |

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

### oe-adapter-1.3: OE Provider 用 page name "discovery"

参考 `discovery/README.md` discovery-1.x(子用例)。

### oe-adapter-1.4: OE Provider 抓取失败处理

参考 `discovery/README.md` discovery-1.7。

---

### oe-adapter-2.x (Step 2): OE DataClient

**待 Step 2 启动时展开**。预期范围:
- 继承 `LiveMarketDataClient`(不是 `LiveDataClient`)
- `_subscribe_order_book_deltas` 实现
- 通过 `manager.get_page("data")` 拿 page,**不**调 `start()` / `close()`
- 输出 NT 标准 `OrderBookDelta`(不是 `QuoteTick`)
- WS 帧解析(从 `odds_client.py` 平移)+ Playwright CDP 抓取
- 重连机制(Playwright page reload,从 `odds_client.py` 平移)
- **健康检查 / 网页刷新归本 client(Q13)** —— 见下方 oe-adapter-2.health.*

---

### oe-adapter-2.health.1: OE 时间维度刷新触发(Q13,§6.8.3)

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

### oe-adapter-2.health.2: OE 状态维度刷新触发(Q13)

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

### oe-adapter-2.health.5: 执行在飞时 OE 健康检查跳过(Q19 全局互斥)

**前置**: 一次 execution session 进行中(`_execution_active=true`,含 OE 腿)
**输入**: OE 健康检查 alert fire
**期望**: tick 开头判 `_execution_active` → **整个 tick 跳过**(不 reload 页面、不 reconcile);§6.8.4.5 finally 照常重排下次 alert
**验收**:
- 页面 reload 不会在执行期冲掉执行用页面 / WS
- OE 健康检查订阅 `execution.*` 维护 `_execution_active` 镜像;`execution.finished` 后下个 alert 正常执行
- OE 健康检查 tick 开始/结束也 publish `health_check.started/finished`(供 strategy pre-check 放弃机会)

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

**`general` 频道帧格式(2026-05-22 实测抓帧锁定)**:`prices` 与 `general` 两个 WS;`general`(SockJS 下行 `a[...]`)**承载多类帧,按顶层 key 分型**(`message_parser.parse_general_frame` 已实现,`test_ws_general_frames.py` **8 passed**):
- `{"BALANCE":{"balance":"37.49","avBalance":null}}` —— `balance` 是**字符串**,WS 已含挂单占用。
- `{"CURRENT_BETS":[<bet>,...]}` —— 当前注单(抓到样本为空 `[]`)。
- 上行订阅请求 `["{...subscribe:true...}"]`(无 `a`/无数据);**未知 key 帧时不时收到 → 忽略**。

### oe-adapter-5.ws.1: 订单帧解析 → generate_order_*(envelope 已解,**item schema 待 populated 抓帧**)
**前置**: `parse_general_frame` 已能分型 + 透传 `CURRENT_BETS` 列表(envelope 已确认、已测)
**输入**: 一条非空 `CURRENT_BETS` 帧
**期望**: OE ExecClient `_on_current_bets_frame` 把每个 bet → `generate_order_*` / position report 回写 NT 标准管道;`leg_settled` 按 §6.8.2 置 true(经 `generate_order_*` 覆盖,腿键=instrument_id)
**验收**:
- **已**(✅):envelope 分型 + 列表透传(ws.1 解析层)
- **待**:单 bet item 字段映射 —— 工作假设与 REST `/customer/api/currentBets` `bets[]` 同源(`marketId`/`selectionId`/`sizeMatched`/`averagePrice`/`side`/...),**需 populated 抓帧确认**后实写 bet→OrderStatusReport 映射

### oe-adapter-5.client: OrbitExchExecutionClient 骨架(✅ 离线核心已测,集成 /live-test)
**落地**: `src/arbitrage/execution/orbitexch.py`(`tests/arbitrage/execution/test_orbitexch_client.py`,全 arb 套件 76 passed)
- **已测(离线)**: 离线可构造(super 只需 instrument_provider + 标准 NT 依赖);`_on_general_frame` BALANCE→`generate_account_state`(`oe_balance_to_account_balances`:WS 已净挂单→total=free GBP)、未知/null 忽略;`_modify_order`→`generate_order_modify_rejected`(OE 不支持改单);`_submit_order` session 门控(cancel-only 丢弃 / executor 失败→reject)
- **live seam(/live-test 验)**: `_connect`(browser/executor/general WS)、`_place_via_executor`(NT Order→executor 旧 Order,market_id/selection_id 取自 instrument.info)、`_cancel_*`(executor);`CURRENT_BETS` item→事件 + reports 待 populated 抓帧

### oe-adapter-5.ws.2: WS 余额帧 → generate_account_state(✅ 解析层+客户端路由已实现+测)
**前置**: `parse_general_frame` 识别 `BALANCE` 帧 → `{"type":"balance","balance":float,"av_balance":...}`(已测,含 null/字符串/非法值健壮)
**输入**: 一条 `BALANCE` 帧
**期望**: OE ExecClient `_on_balance_frame` → `generate_account_state(...)` 写 Cache(待 OE ExecClient 落地接线)
**验收**:
- **已**(✅):`general` 帧捕获 + BALANCE 解析(`balance` 字符串→float)
- **待**:OE ExecClient 把 parsed balance → `generate_account_state`(随 OE 客户端落地)
- OE 余额经 WS 被动维护(对齐 §5.5/§5.6 "被动 WS" + Q17);cache 余额 = WS 上报值,`_check_balance` 直接信不再减;`get_balance()` 页面抓取作过渡兜底,权威源是 WS 帧

---

### oe-adapter-5.session.1: cancel-only session(残留挂单)(Q13)

**前置**: instrument I 上有未成交残留挂单;`leg_settled` entry 已存在
**输入**: strategy 调 `submit_order(new_order)`,execution 入口检查到残留
**期望**:
- session 退化为 cancel-only,**丢弃 `new_order`**(不进队列,不延后下发)
- 撤掉残留 → track 到 CANCELED terminal → 命中方向 `leg_settled=true`
- strategy 端不到下一轮重发,系统不替它补发
**验收**: cancel session 单一职责,submit 被显式丢弃

### oe-adapter-5.session.2: submit+track session(无残留)(Q13)

**前置**: instrument I 无残留挂单
**输入**: strategy 调 `submit_order(order)`
**期望**:
- session 走 submit+track: 下单 → 等成交 terminal
- 命中 FILLED / CANCELED / REJECTED 任一 terminal,对应方向 `leg_settled=true`
**验收**: track 必达 terminal 才算 session 结束

### oe-adapter-5.session.3: 移除 recovery loop(Q13)

**前置**: 任一 session
**输入**: tracking 收到 terminal 后
**期望**:
- execution 不**再启**新一轮 plan / retry
- 不存在 "execution 内部撤后再下" 路径(strategy 后议补救)
**验收**: 静态检索 execution 代码无 recovery 循环;关联 `bug_compensating_cancel_missing` 留在 strategy 设计 TODO

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

### oe-adapter-5.timeout.3: submit+track session 超时(零 venue 响应)(Q15 极端)

**前置**: session timeout = 30s;`leg_settled[X][home] = false`
**输入**: submit → venue 端无任何响应(可能 submit 没到 / WS 死)→ 30s timeout
**期望**:
- 30s timeout:session 结束,无任何补救
- `leg_settled[X][home]` 仍 = false(从未收到确认)
- 下一个 OE health check tick 看到 `settled=false` → 触发兜底刷新页面 → 通过 `generate_*_status_report` 同步 → settled=true
**验收**:
- timeout 不试图自救,**依赖健康检查兜底**
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
