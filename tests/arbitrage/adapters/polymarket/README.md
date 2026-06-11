# PM 适配器测试

PM 部分**完全使用上游 NT 的适配器**(`nautilus_trader/adapters/polymarket/{data,execution,providers,factories}.py`),自研代码全部删除。

本目录的测试目的是**验证上游版本满足我们套利系统的需求**,不是测试上游本身(NT 框架已有自己的测试)。

对应章节: `refactor.md §5.1.1, §5.2.1, §5.5, §6.5`

## 锁定决定

- Q1: InstrumentId = `{condition_id}-{token_id}.POLYMARKET`(上游已定,直接用)
- Q10: 不加 lock 包装,直接用上游裸 `PolymarketExecutionClient`
- Q11.3: `skip_execution` 通过 `SkipExecutionPolymarketClient(PolymarketExecutionClient)` 子类覆盖
- Q13(2026-05-19,Q-L 翻盘): PM adapter 内部承担健康检查 = 默认周期 + 可被外部事件中断;动作 = 拉持仓/挂单/余额 → **翻译为 `PositionStatusReport` / `OrderStatusReport`,走 NT 标准 reconcile 通路**(`generate_*_report` → ExecutionEngine reconcile → 写 cache + Portfolio + 发 `events.position/order.*` topic);余额变化走 `generate_account_state`(已是 NT 标准);execution session 单一职责(cancel-only 或 submit+track,都 track 到 terminal);移除 recovery loop。详见 `refactor.md §6.8`

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_upstream_integration.py` | 上游 PM 适配器在我们配置下能正常加载 / 订阅 / 下单 / 事件回写 |
| `test_arb_provider.py` | **#55/#57** series-based 发现纯函数:27 tests 覆盖 `_teams_from_event`(权威队名源,顺序无关 / abbr 小写 / 缺失或不全返 None)、`_parse_team_names`(fallback:vs./vs/正则、competition 前缀清洗、`-`/`?`/`, scheduled for` 清理、无 vs 返 None)、`_ticker_abbrs`、`_role_for_token`(2-way `ordering=home` 正排 / `ordering=away` 反排=MLB、单市场 3-outcome 正反排、3-way binary home/away/draw_yes、No token 跳过、未知后缀跳过、空 ticker 返空) |
| `test_sports.py` | **#60** PM Sports 比分信号(`sports.py`):4 tests —— `parse_sport_result`(实采 wnba live / atp ended+`finished_ts` / 缺 `gameId`→None)+ `SportsGameUpdate` to_dict/from_dict roundtrip。WS 连接(`PolymarketSportsDataClient`)经 /live-test 验(公开 firehose)。**映射键 `game_id`** == gamma `event["gameId"]`(`arb_provider` 抽入 `info["game_id"]`);eviction 由 `ended` 驱动(matching,见 matching README)|
| `test_data_client_ws_retry.py` | PM DataClient market WS 启动连接失败后保留订阅并重试;disconnect/no subscriptions 不重试;首个 `OrderBookDeltas` 发布计数/日志锚点 |
| `tests/arbitrage/config/test_dispatcher.py` / `test_loader.py` | PM adapter 接线前置:项目 `venues.polymarket.ws_url` 兼容旧 full endpoint(`/ws/market` / `/ws/user`),dispatcher 传给上游 `PolymarketWebSocketClient` 前归一化为 base URL(`/ws/`);`proxy_url` 从 JSON 或 env 注入并透传给 PM Data/Exec client |

---

## 用例

### pm-adapter-1.1: 上游 InstrumentProvider 在套利系统配置下加载

**前置**: 用 `PolymarketInstrumentProviderConfig` 配置 PM 私钥 + sport 过滤器
**输入**: 启动 NT TradingNode + 上游 `PolymarketInstrumentProvider`
**期望**: Cache 中有 PM `BinaryOption` 列表,字段完整
**验收**: 不需要修改上游代码即可工作

### pm-adapter-1.2: 套利系统自定义 info 字段填充(✅ 结构落地,extraction TODO)
**落地**: `nautilus_trader/adapters/polymarket/arb_provider.py` + `arb_factories.py:ArbPolymarketLiveDataClientFactory`(`tests/arbitrage/adapters/polymarket/test_arb_provider.py` 4 passed)
- 评估结果:**上游 info=market_info(gamma dict)缺 6-key** → 走"子类化 PolymarketInstrumentProvider 补"路径
- `ArbPolymarketInstrumentProvider._parse_instrument` super 后调 `enrich_pm_six_key_info(market_info, outcome)` update info
- 当前 enricher 是 **best-effort seam**:`sport ← market_info["category"]`(能拿就拿);其它 5-key 空字符串/0 占位
- ⬜ **TODO live**:实写需 PM gamma `/events/{event_id}` HTTP + ticker 拆解(参旧 `odds_client.py:255+`);现 matching `events_from_instruments` 见空 key 跳过 → **PM 侧暂不参与匹配**(结构完整,extraction 实写时下游不动)

### pm-adapter-2.1: 上游 DataClient 输出 OrderBookDelta 而非 dict

**前置**: 启动上游 `PolymarketDataClient`,订阅一个 `BinaryOption`
**输入**: PM WS 推送 book 更新
**期望**: 通过 NT MessageBus 收到 NT 标准 `OrderBookDelta`
**验收**: 数据出口确认是 NT 类型(便于 ArbitrageStrategy 使用 `cache.order_book(...)`)

### pm-adapter-2.2: PM WS URL 归一化为上游 base URL

**前置**: `ArbConfig.venues.polymarket.ws_url` 可填新 base URL `wss://ws-subscriptions-clob.polymarket.com/ws/`,也可填旧服务遗留 full endpoint `.../ws/market` 或 `.../ws/user`
**输入**: `to_polymarket_data_client_config(cfg)` / `to_polymarket_exec_client_config(cfg)`
**期望**: 两者输出的 `base_url_ws` 均为 `.../ws/`,由上游 `PolymarketWebSocketClient` 自行拼接 `market` / `user`
**验收**: `tests/arbitrage/config/test_dispatcher.py::test_polymarket_ws_url_normalized_to_nt_base_url` 覆盖旧/新写法;live smoke 日志不得出现隐式 `.../ws/marketmarket` 或 `.../ws/marketuser` 目标

### pm-adapter-2.3: PM market WS 启动连接失败自动重试

**前置**: StrategyEvaluator 已对 PM instrument 发起 `SubscribeOrderBook`;PM DataClient 已记录 token 订阅
**输入**: `_delayed_connect` 中第一次 `PolymarketWebSocketClient.connect()` 抛网络异常(如 `Operation timed out`)
**期望**: 不丢订阅、不让 task error 终止;DataClient 记录 warning 并按至少 5s 间隔重新调 `_delayed_connect`
**验收**: `test_data_client_ws_retry.py` 覆盖失败重排与 disconnect 期间不重试;live smoke 若 PM CLOB WS 网络 transient,后续应看到重复 retry warning 而不是永久无 PM 盘口

### pm-adapter-2.3b: PM market WS 显式代理透传

**前置**: 当前网络访问 PM CLOB WS 需要 HTTP(S) proxy;进程 env 中存在 `POLYMARKET_PROXY_URL` 或 `https_proxy` / `http_proxy`
**输入**: `load_arb_config` + `to_polymarket_data_client_config(cfg)` / `to_polymarket_exec_client_config(cfg)`
**期望**: `cfg.venues.polymarket.proxy_url` 被注入并透传到 PM Data/Exec config;JSON 显式 `proxy_url` 优先于 env。#98 起同一个 `proxy_url` 也必须配置到 `py_clob_client_v2` CLOB REST transport,显式代理存在时关闭环境代理继承,确保 WS/REST 同路由。
**验收**: `tests/arbitrage/config/test_loader.py::test_env_injects_polymarket_proxy_when_json_missing`、`test_json_polymarket_proxy_wins_over_env`、`tests/arbitrage/config/test_dispatcher.py::test_polymarket_exec_client_config_maps_proxy`、`tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_factory_configures_v2_http_proxy`;live 诊断中 NT pyo3 `WebSocketClient` 显式 `proxy_url=http://127.0.0.1:7890` 可连接 `wss://ws-subscriptions-clob.polymarket.com/ws/market`,PM CLOB REST 也走同一配置路由。

### pm-adapter-2.3c: PM proxy 钱包 signature_type 透传

**前置**: PM 账户使用 Polymarket proxy/funder 钱包,env 配置 `POLYMARKET_SIGNATURE_TYPE=2`
**输入**: `load_arb_config` + `to_polymarket_data_client_config(cfg)` / `to_polymarket_exec_client_config(cfg)`
**期望**: `venues.polymarket.signature_type` 从 env 转 int 注入,并透传到 PM Data/Exec config。proxy/funder 钱包必须用 `2`,否则上游 CLOB balance endpoint 按 EOA(`0`)查 collateral,NT 账户状态会显示 `0.000000 USDC.e`
**验收**: `tests/arbitrage/config/test_loader.py::test_env_injects_polymarket_credentials` 覆盖 env 注入;`tests/arbitrage/config/test_dispatcher.py::test_polymarket_exec_client_config_maps_signature_type` 覆盖 Exec config 透传;live 只读探针确认 `signature_type=0 → 0.000000 USDC.e`,`signature_type=2 → 67.916080 USDC.e`

### pm-adapter-2.4: PM 首个 OBD 发布观测锚点

**前置**: PM market WS 已连并收到 book snapshot 或 price change
**输入**: `_handle_deltas` / quote update 生成 `OrderBookDeltas`
**期望**: 第一次发布时记录 `PM OrderBookDeltas published: instrument_id=..., deltas=...`,并递增 `_book_deltas_published`
**验收**: `test_data_client_ws_retry.py::test_publish_deltas_records_first_pm_obd`;live smoke 用该日志判定 PM 盘口已进入 NT DataEngine 前的数据出口

### pm-adapter-5.1: 上游 ExecutionClient 下单 + 事件回写

**前置**: 测试网或 paper trading 账户
**输入**: NT `Strategy.submit_order(...)` 一笔限价单
**步骤**:
1. ExecutionEngine 路由到上游 `PolymarketExecutionClient._submit_order`
2. py_clob_client_v2 签名 + 提交
3. 通过 USER channel 收到 ack → `generate_order_submitted` / `generate_order_accepted`
4. 模拟成交后 → `generate_order_filled`
**期望**: Strategy.on_order_* 系列回调全部触发
**验收**:
- 订单生命周期完整(submitted → accepted → filled)
- `bug_polymarket_order_version_mismatch` **不复现**(PM adapter 使用 `py_clob_client_v2` L2 client)

### pm-adapter-5.1b: PM py_clob_client_v2 surface 锁定

**前置**: 本地 `py_clob_client_v2` 版本为当前项目依赖版本
**输入**: `get_polymarket_http_client` / `PolymarketExecutionClient._submit_limit_order` / `_submit_order_list` / cancel/report 路径
**步骤**:
1. factory 返回 `py_clob_client_v2.ClobClient`
2. 单笔路径调用 v2 `ClobClient.post_order`
3. 批量路径构造 v2 `PostOrdersArgs`
4. order status reports 调 `get_open_orders`
5. 单笔撤单调 `cancel_order(OrderPayload(...))`
**期望**: PM adapter 不回退旧 `py_clob_client`;使用 v2 SDK 的订单提交 / 查询 / 撤单 surface
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_execution_uses_py_clob_client_v2_surface`

### pm-adapter-5.1c: PM CLOB REST geoblock + API readiness preflight

**前置**: `skip_execution=false`,PM ExecClient 准备真连接;`venues.polymarket.proxy_url` 已由 JSON 或 env 决定。
**输入**: `PolymarketExecutionClient._connect`
**步骤**:
1. 用与 CLOB REST 相同的 `proxy_url` 请求官方 `https://polymarket.com/api/geoblock`
2. 若 country 属 API-blocked / close-only / blocked region,直接失败并阻止真实 submit
3. 若 country 属 frontend-only restricted(官方当前 JP),不因 `blocked=true` 一刀切失败;继续后续 CLOB REST 检查
4. launcher 只读 `--preflight-polymarket` 额外调用 CLOB `get_server_time()` + authenticated `get_open_orders()` + `get_balance_allowance()`
**期望**: API-blocked / close-only / REST 不通的出口不会进入真下单路径;JP 这类 frontend-only restricted 不误拦 API 路径;`signature_type` / funder 配错导致余额为 0 时也在启动前失败;错误信息包含 country/region、signature_type 或 SDK transport 失败原因。
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_geoblock_preflight_rejects_blocked_route` / `test_polymarket_geoblock_preflight_allows_frontend_only_restricted_country` / `test_polymarket_geoblock_preflight_uses_configured_proxy`;`tests/arbitrage/launchers/test_arb_node.py::test_main_preflight_polymarket_sdk_error_returns_2` / `test_preflight_polymarket_trading_uses_exec_proxy` / `test_preflight_polymarket_trading_rejects_zero_balance`。2026-06-10 JP 出口实测 preflight OK:`server_time` 可读、`open_order_count=0`、`balance=67.916080 USDC.e`;AU/NSW 仍按官方 API-blocked fail fast。

### pm-adapter-5.2: 撤单接口

**前置**: 已下一笔挂单
**输入**: `Strategy.cancel_order(...)`
**期望**: `_cancel_order` → CLOB `cancel_order(OrderPayload)` → 响应 `canceled[]` 中的订单立即 `generate_order_canceled` 回写;`not_canceled` 中的订单走 `generate_order_cancel_rejected`(其中 `already canceled or matched` 保持抑制,等待 WS/成交终态)。REST cancel 与 USER WS cancellation 可能重复到达,若 cache 中订单已是 `CANCELED` 必须跳过重复终态。
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_cancel_order_success_generates_canceled_event` / `test_polymarket_cancel_success_skips_duplicate_canceled_order` / `test_polymarket_cancel_order_reject_generates_cancel_rejected_event`;live cancel-only 需同时看到 `Execution session cancel-only` 与 `Cancel confirmed ...` / `OrderCanceled`,最后 venue `open_order_count=0`

### pm-adapter-5.3: Reconciliation(启动重连对账)

**前置**: 模拟 Strategy 重启后,venue 上有上一会话遗留的挂单
**输入**: NT TradingNode 启动 + ExecutionEngine reconciliation
**期望**: ExecutionEngine 调上游 `generate_order_status_reports` / `generate_position_status_reports`,把遗留挂单/仓位补到 Cache
**验收**: Cache 中能查到这些挂单,Strategy 可以选择保留或撤销

### pm-adapter-5.account.1: 余额刷新是事件驱动,无周期 timer(Q17)

**前置**: PM ExecutionClient 启动
**输入**: 分别触发 (a) `_connect()`;(b) 一笔成交达 `POLYMARKET_FINALIZED_TRADE_STATUSES`(链上确认)
**期望**: 两种情形各调一次 `_update_account_state` → `get_balance_allowance` → `generate_account_state` 写 cache
**验收**:
- 上游 `execution.py` 内**无 `set_timer` / 周期轮询**;静态搜索确认
- NT 无默认 `QueryAccount` 周期发送(全库仅反序列化处实例化)
- **健康检查不拉余额**(Q17):PM 余额完全靠这两个事件,§6.8.4 健康检查只对账持仓/挂单不碰余额;可用余额由 `_check_balance` 自扣在途挂单(risk-6.3b)

### pm-adapter-5.account.2: free=total 陷阱 —— reported 快照清空 NT 自算 locked(Q17)

**前置**: PM `total=100`,一笔未成交挂单本应占用 60
**输入**: `_update_account_state` 发 `generate_account_state(reported=True, locked=0, free=100)`
**期望**:
- `CashAccount.apply()` 见 `is_reported` → 清空 `_balances_locked`(cash.pyx:178-179)
- cache 中 `account.balance_free()` = 100(未反映挂单占用)
**验收**:
- 记录此为**已知行为**(Polymarket 链上不托管未撮合单,venue 视角 free=total 属实)
- 因此可用余额的"扣挂单"责任落在 `ArbitrageRiskEngine._check_balance` 自算(见 risk-6.3b),**不在 adapter 改上游**

---

### pm-adapter-5.health.1: PM 周期健康检查走 NT report 通路(Q13 / Q-L 翻盘)

**前置**: PM adapter 启动,健康检查周期 = 配置默认值;某 competition X 已发起过 execution → `leg_settled` entry 存在
**输入**: 等到下一个周期 tick 自然触发
**期望**:
- 拉一次持仓 + 挂单(REST / Data API;**不拉余额**,Q17)
- 持仓差异 → `generate_position_status_report(report)` → ExecutionEngine.reconcile → `cache.update_position` + `Portfolio.update_position` endpoint + `events.position.{strategy_id}` topic
- 挂单差异 → `generate_order_status_report(report)` → 同上 (orders 通道)
- 余额**不在此拉**(完全靠上游事件,见 pm-adapter-5.account.1)
- 拉取成功后 PM 侧 X 的所有方向 `leg_settled=true`
**验收**:
- Portfolio.unrealized_pnl / margins_init 与 venue 真实状态一致
- Strategy 订阅的 `on_position_event` / `on_order_event` 回调被触发(健康检查发现差异时)
- 静态搜索 PM adapter 健康检查代码: **必须**调 `generate_*_report`,**不得**直接 `cache.update_*`

### pm-adapter-5.health.4: 健康检查 tick 内顺带 merge/redeem(Q18b)

**前置**: 健康检查那次 `/positions` 原始响应里:condition A 两 outcome 都持仓(可 merge);condition B `redeemable=true`(可 redeem)
**输入**: 健康检查 tick 触发(reconcile 持仓/挂单后)
**期望**:
- 宿主 = **PM `ExecutionClient` 薄子类**(Q18c);tick 内 reconcile 后调 `self._settlement.run(positions_raw)`
- 复用同一次 `/positions` 拉取,**不另起请求**
- `PolymarketSettlement`(普通类)→ condition A `contract.merge_positions(...)`;condition B `contract.redeem_positions(...)`
- merge/redeem 决策细节见 `tests/arbitrage/settlement/README.md`(settlement-8.x)
**验收**:
- **不存在独立 `PolymarketSettlementActor` / 独立周期调度**(静态检查)
- 链上编排逻辑在 `PolymarketSettlement` 普通类,**不内联进 ExecutionClient 方法**(ExecutionClient 只持引用 + 触发)
- merge/redeem 的 `TxResult` 失败时:**仅 log,不影响** 本 tick 的 `venue_connected` / `leg_settled` 判定(结果不作健康判据)
- 失败下次 tick 重试(幂等)

### pm-adapter-5.health.5: 执行在飞时健康检查 tick 跳过(Q19 全局互斥)

**前置**: 一次 execution session 进行中(`execution.started` 已发,`_execution_active=true`)
**输入**: 健康检查 alert fire
**期望**: tick 开头判 `_execution_active` → **整个 tick 跳过**(不拉 `/positions`、不 reconcile、不 merge/redeem);§6.8.4.5 finally 照常重排下次 alert
**验收**:
- 健康检查与执行**不并发**;跳过不报错
- execution 收到 terminal/timeout 发 `execution.finished` 后,下个 alert 正常执行
- 断言 PM 健康检查订阅了 `execution.*` 并维护本地 `_execution_active` 镜像

### pm-adapter-5.health.2: PM 外部事件抢占周期等待(Q13)

**前置**: 正处于"等待下一个周期 tick"的休眠期(距上次 health 拉取仅 1s,周期 = 60s)
**输入**: 外部消息触发"立即健康检查"
**期望**:
- 跳过剩余 ~59s 等待
- 立刻执行一次 health 拉取(仍走 NT report 通路)
- 此次后周期计时**从当下重新起算**
**验收**: 外部触发能即时打断等待,沿用现有"被打断"语义

### pm-adapter-5.health.3: 健康检查触发 NT 事件链(Q13 / Q-L)

**前置**: 一次健康拉取发现 venue 端有一笔 cache 缺失的 fill / position
**输入**: 拉取结果送进 `generate_position_status_report` / `generate_order_status_report`
**期望**:
- ExecutionEngine 推进 Order 状态机(`_apply_event_to_order`)
- ExecutionEngine 派生 Position(`_handle_position_update`)
- Portfolio 收到 `update_order` / `update_position` endpoint
- Strategy 收到 `events.order.{strategy_id}` / `events.position.{strategy_id}` topic
**验收**: 四条 NT 标准路径**全部触发**;cache 与 Portfolio 一致;Strategy 不退化到主动轮询 cache

---

> **schedule 系列实现基于 NT `Clock`**(§6.8.4.5):自重排 one-shot time-alert(`clock.set_time_alert_ns`),时间读 `clock.timestamp_ns()`,下次时间查 `clock.next_time_ns(name)`,关停 NT 自动 `cancel_timers()`。**不**用 `asyncio.Event` / `time.monotonic()`。

### pm-adapter-5.schedule.1: 每轮结束重排下次检查 alert(§6.8.4.5)

**前置**: PM adapter 健康检查启动,`health_check_interval_sec = 60`,t=0 完成第一轮
**输入**: 等到 t=60 / 120 / 180 各轮自然 fire
**期望**: 每轮 callback 末尾 `clock.set_time_alert_ns(name, now_ns + 60s, override=True)`,`clock.next_time_ns` 稳定指向下一 fire 点
**验收**: 每轮**结束**(callback finally)才排下次 alert(不是开始)

### pm-adapter-5.schedule.2: 运行时改 interval 下一轮即时生效

**前置**: 初始 interval=60,t=0 第一轮结束 alert 排在 t=60
**输入**: t=10 改 `health_check_interval_sec = 30`
**期望**:
- 第二轮仍 t=60 fire(alert 已排定)
- 第二轮 callback 末尾 `set_time_alert_ns(now_ns + 30s)`(读当前 config)
- 第三轮在 t ≈ 90 fire
**验收**: 无需重启;`_schedule_next()` 每次读 config

### pm-adapter-5.schedule.3: trigger 立即唤醒(对应原"外部消息可立即触发")

**前置**: alert 排在 t=60,当前 t=20
**输入**: 外部调 `trigger_health_check()`
**期望**:
- `clock.set_time_alert_ns(name, now_ns, override=True)` → NT past/now 即时 fire
- 立即拉一次持仓/挂单/余额(t=20)
- callback 末尾重排 alert 到 `20 + interval`
**验收**: 与 §6.8.4 "外部消息可立即触发" 锁定行为一致

### pm-adapter-5.schedule.4: 异常路径也重排 alert

**前置**: 一轮拉取内部抛异常(模拟 PM API 失败)
**输入**: 异常在 callback 内抛出
**期望**: callback `try/finally` 保证 `finally` 里 `_schedule_next()` 仍执行,排下次 alert
**验收**: 避免一次 API 失败让健康检查永久停摆

### pm-adapter-5.schedule.5: 不实现 block/unblock API(§6.8.4.5)

**前置**: 检查 PM adapter 健康检查实现
**期望**:
- 类上**没有** `block_health_check` / `unblock_health_check` / `is_health_check_blocked` 方法
- 实例上**没有** `_health_check_blocked` 字段
- status dict 输出**没有** `"blocked"` 字段
**验收**: P6 不超前实现;旧 `services/risk/service.py` 中对应符号 Step 5/6 实施时一并删除

### pm-adapter-5.session.1: cancel-only session(残留挂单)(Q13)

**前置**: PM 上 token T 有未成交残留挂单
**输入**: strategy 调 `submit_order(new_order)`,execution 入口检查到残留
**期望**:
- session 退化为 cancel-only,**丢弃 `new_order`**
- 撤单 → track 到 CANCELED → 对应方向 `leg_settled=true`
**验收**: cancel session 单一职责,submit 被显式丢弃

### pm-adapter-5.session.2: submit+track session(Q13)

**前置**: PM token T 无残留挂单
**输入**: strategy 调 `submit_order(order)`
**期望**: 下单 → track 到 FILLED / CANCELED / REJECTED → 对应方向 `leg_settled=true`
**验收**: track 必达 terminal

### pm-adapter-5.session.3: 移除 recovery(Q13)

**前置**: 任一 session
**输入**: tracking 收到 terminal
**期望**: execution **不再启**新一轮 plan / retry;补救留给 strategy 后议
**验收**: 静态检索 PM execution 代码无 recovery 循环

### pm-adapter-5.health.2: 非 execution 触发的事件无 entry 不创建(Q13 边界)

**前置**: PM token T 从未发起过 execution → 无 `leg_settled` entry;但 venue 端推一笔事件(`OrderFilled` 含 partial / `OrderCanceled` / `OrderAccepted` 等任意 ——场景: 历史挂单延迟成交 / WS USER channel 推迟到达 / 上一会话遗留)
**输入**: WS USER channel 帧到达
**期望**:
- adapter 调 `generate_order_*(...)` 进 NT 标准管道
- ExecutionEngine.reconcile → cache + Portfolio + `events.order/position.*` topic
- Strategy 通过 `on_order_*` / `on_position_event` 收到通知
- **`leg_settled` entry 不被创建**(无 execution 历史)
**验收**: cache 与 Portfolio 一致;Strategy 收到事件;`leg_settled` 集合不含该方向

### pm-adapter-5.health.3: 非 execution 触发的事件命中已有 entry 时置 settled=true(Q13 边界)

**前置**: PM competition X 已 execution 过,`leg_settled[X] = [false, false]`(刚启动新一轮 execution 重置后);一笔历史挂单的成交事件迟到(对应 X 某方向,可以是 partial fill)
**输入**: WS USER channel 帧到达
**期望**:
- 走 NT 标准管道
- **置该方向 `settled=true`**(无论是 partial 还是 terminal,因为已有 entry)
**验收**: `leg_settled` entry 集合不变(只更新值,不增删)

### pm-adapter-5.health.4: partial fill 也触发 settled=true(Q13 partial 语义)

**前置**: 首次对 PM competition X home 方向 execution;`leg_settled[X][home] = false`
**输入**: WS USER channel 推一笔 partial `OrderFilled`(order.status → `PARTIALLY_FILLED`)
**期望**:
- adapter 调 `generate_order_filled(...)` 进 NT 标准管道
- ExecutionEngine 推进 Order 状态机到 `PARTIALLY_FILLED`(非 terminal)
- **`leg_settled[X][home] = true`**(基于"任何确认事件"语义,不等 terminal)
**验收**:
- cache 中 order.status = `PARTIALLY_FILLED`
- settled 已为 true,下一个 health tick 不再对这条腿触发"无回信"刷新

### pm-adapter-5.health.5: OrderAccepted 也触发 settled=true(Q13 partial 语义)

**前置**: 首次对 PM competition X home 方向 execution;venue 端 latency 较高,仅返回 `OrderAccepted`,尚未撮合
**输入**: WS USER channel 推 `OrderAccepted`
**期望**:
- 进 NT 标准管道
- `leg_settled[X][home] = true`(说明 submit 已到 venue,通讯通道存活)
**验收**: 即使没有任何 fill,settled 也已为 true

---

> **timeout 系列实现基于 NT `Clock` 一次性 time-alert**(§6.8.5):session 启动 `clock.set_time_alert_ns(f"exec_timeout_{coid}", submit_ts_ns + timeout_ns, callback)`;收到 terminal 即 `clock.cancel_timer(...)`;关停 NT 自动 `cancel_timers()`。**不**用 `asyncio.wait_for`。

### pm-adapter-5.timeout.1: submit+track session 早于超时完成(Q15)

**前置**: session timeout = 30s
**输入**: submit → 5s 时 venue 推 terminal `OrderFilled` (full)
**期望**:
- 收到 terminal → `clock.cancel_timer(f"exec_timeout_{coid}")` 取消超时 alert
- session 在 5s 时正常结束
- timeout callback 未 fire
**验收**: 验证 terminal 抢先 cancel alert,watchdog 不触发

### pm-adapter-5.timeout.2: submit+track session 超时(partial fill 卡住)(Q15)

**前置**: session timeout = 30s;`leg_settled[X][home] = false`
**输入**: submit → 1s 时收到 partial fill(qty=$30 of $100)→ 之后 venue 不再推任何事件 → 30s timeout
**期望**:
- partial 1s 时:`leg_settled[X][home] = true`
- 30s timeout:**session 直接结束**,**execution 不做任何动作**
- order 在 cache 中保持 `PARTIALLY_FILLED`,position = $30
- Strategy 下一轮调 submit 时,触发残留检测 → cancel-only session 撤剩余 $70
**验收**:
- timeout 路径无任何 `_cancel_order` / `_submit_order` 调用
- 静态搜索 PM execution timeout 处理代码:仅 log + 结束 session
- 状态自洽:闭环依赖 strategy 下轮

### pm-adapter-5.timeout.3: submit+track session 超时(零 venue 响应)(Q15 极端)

**前置**: session timeout = 30s;`leg_settled[X][home] = false`
**输入**: submit → venue 端无任何响应(可能 PM API 失败 / WS USER channel 断 / 签名错误未到 venue)→ 30s timeout
**期望**:
- 30s timeout:session 结束,无任何补救
- `leg_settled[X][home]` 仍 = false
- 下一个 PM health check 周期 tick 看到 `settled=false` → 触发兜底拉取 → 通过 `generate_*_status_report` 同步
**验收**:
- timeout 不试图自救,**依赖健康检查兜底**

### pm-adapter-5.timeout.4: cancel-only session 超时(Q15)

**前置**: PM token T 有残留挂单;session timeout = 30s
**输入**: strategy 调 submit 触发 cancel-only session → adapter 调上游 `_cancel_order` → venue 不推 CANCELED → 30s timeout
**期望**:
- session 结束,**仅 log warning**,不再做任何动作
- order 在 cache / venue 仍可能是 `ACCEPTED` 或 `PARTIALLY_FILLED`
- strategy 下一轮调 submit 时,残留检测仍会触发新一轮 cancel-only session
**验收**: 避免无限循环

### pm-adapter-5.timeout.5: 超时 alert 不被 partial fill 重设(Q15)

**前置**: session timeout = 30s
**输入**: submit → 5s/15s/25s 各一笔 partial → 30s timeout fire
**期望**:
- timeout alert 仍在 `submit_ts + 30s` fire,与最后 partial 时刻无关
- 绝对超时语义:alert 一次性设在 submit 时刻 + timeout,partial 事件不重设
**验收**: 静态检查:partial fill 处理路径无 `set_time_alert_ns` 重设超时 alert 的调用
