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
| `test_arb_provider.py` | **#55/#57** series-based 发现纯函数:27 tests 覆盖 `_teams_from_event`(权威队名源,顺序无关 / abbr 小写 / 缺失或不全返 None)、`_parse_team_names`(fallback:vs./vs/正则、competition 前缀清洗、`-`/`?`/`, scheduled for` 清理、无 vs 返 None)、`_ticker_abbrs`、`_role_for_token`(2-way `ordering=home` 正排 / `ordering=away` 反排=MLB、单市场 3-outcome 正反排、3-way binary home/away/draw_yes、No token 跳过、未知后缀跳过、空 ticker 返空) |
| `test_sports.py` | **#60/#127** PM Sports 比分信号 + PMSPORTS synthetic anchor(`sports.py`):6 tests —— `parse_sport_result`(实采 wnba live / atp ended+`finished_ts` / 缺 `gameId`→None)+ `SportsGameUpdate` to_dict/from_dict roundtrip + `PolymarketSportsInstrumentProvider` 产出 `.PMSPORTS` non-tradable anchor + `PolymarketSportsLiveDataClientFactory` 读取 `target_competitions_by_data_source["PMSPORTS"]` / `competition_to_sport_by_data_source["PMSPORTS"]`。WS 连接(`PolymarketSportsDataClient`)经 /live-test 验(公开 firehose)。2026-06-29 live 诊断确认 PM Sports 发协议层 ping(约 15s),`websockets` 自动回 pong;生产代码关闭客户端主动 keepalive ping(`ping_interval=None`),避免本地 `keepalive ping timeout` 误杀连接。**映射键 `game_id`** == gamma `event["gameId"]`(`arb_provider` / PMSPORTS provider 抽入 `info["game_id"]`);eviction 由 `ended` 驱动(matching,见 matching README)|
| `test_data_client_ws_retry.py` | PM DataClient market WS 启动连接失败后保留订阅并重试;disconnect/no subscriptions 不重试;首个 `OrderBookDeltas` 发布计数/日志锚点 |
| `test_data_client_ws_retry.py::test_update_instruments_continues_after_provider_error` | **2026-06-29 overnight 修**:PM 周期 instrument rediscovery 单轮 `initialize(reload=True)` 抛异常后 task 不退出,下一轮仍继续并成功 `_send_all_instruments_to_data_engine` |
| `test_parsing_min_size.py` / `tests/integration_tests/adapters/polymarket/test_parsing.py` | 上游 PM market payload → `BinaryOption` 翻译;本项目要求 `minimum_order_size` 映射到 `min_quantity` |
| `tests/arbitrage/config/test_dispatcher.py` / `test_loader.py` | PM adapter 接线前置:项目 `venues.polymarket.ws_url` 只接受上游 base URL(`/ws` 或 `/ws/`),dispatcher 传给上游 `PolymarketWebSocketClient` 前归一化为 base URL(`/ws/`);`proxy_url` 从 JSON 或 env 注入并透传给 PM Data/Exec client |

早期 `test_upstream_integration.py` skipped 空壳已删除。PM discovery/info/min-size/WS retry 的离线主路径由上表测试覆盖;PM 真下单、撤单、reconcile 仍按下方 pm-adapter-5.* 用例经 live/preflight 验,不保留永久 skipped pytest。

---

## 用例

### pm-adapter-1.1: 上游 InstrumentProvider 在套利系统配置下加载

**前置**: 用 `PolymarketInstrumentProviderConfig` 配置 PM 私钥 + sport 过滤器
**输入**: 启动 NT TradingNode + 上游 `PolymarketInstrumentProvider`
**期望**: Cache 中有 PM `BinaryOption` 列表,字段完整
**验收**: 不需要修改上游代码即可工作

### pm-adapter-1.2: 套利系统自定义 info 字段填充(✅ 已落地)
**落地**: `nautilus_trader/adapters/polymarket/arb_provider.py` + `arb_factories.py:ArbPolymarketLiveDataClientFactory`(`tests/arbitrage/adapters/polymarket/test_arb_provider.py` 覆盖纯函数;`test_debug_data_factories.py` 覆盖 provider 回写 `instrument_provider_by_venue["POLYMARKET"]`)
- 评估结果:**上游 info=market_info(gamma dict)缺 matching key** → 走"子类化 PolymarketInstrumentProvider 补"路径
- `ArbPolymarketInstrumentProvider.load_all_async` 走 Gamma `/sports` 取 series/order,再按 series 调 `/events?series_id=...` 拉内嵌 teams + markets。
- `_load_moneyline_market` 只接 moneyline 主市场,创建 PM token 后写 `sport/competition/home_team/away_team/selection_role/game_id`;`selection_role` 与 OE/SE 对齐为 `home/draw/away`;`start_ts` 不写入 matching info。
- 验收:`test_arb_provider.py` 覆盖 teams/title 解析、role 解析、moneyline instrument info 写入;完整 Gamma HTTP 路径仍由 live smoke 验。

### pm-adapter-1.3: PM 最小下单 share 映射

**前置**: PM Gamma/CLOB market payload 含 `minimum_order_size`(来自 `orderMinSize`)。
**输入**: `parse_polymarket_instrument(market_info, token_id, outcome, ...)`
**期望**: 产出的 `BinaryOption.min_quantity` 等于 `minimum_order_size`(当前 venue 默认 5 shares),由 NT RiskEngine 在本地拒绝 `quantity < 5`。
**验收**: `tests/arbitrage/adapters/polymarket/test_parsing_min_size.py::test_parse_polymarket_instrument_sets_min_quantity_from_order_min_size`

## #233:2-way canonical claim

- `test_arb_provider.py::test_2way_tokens_use_canonical_yes_no_claims` 与 `test_load_moneyline_market_writes_matching_info_keys` 锁定 2-way home token=`claim=yes`、away token=`claim=no`；两者都是真实 PM token。

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
**期望**: `cfg.venues.polymarket.proxy_url` 被注入并透传到 PM Data/Exec config;JSON 显式 `proxy_url` 优先于 env。#98 起同一个 `proxy_url` 也必须配置到 `py_clob_client_v2` CLOB REST transport,显式代理存在时关闭环境代理继承,确保 WS/REST 同路由。#111 起 PM ExecClient 内部 Data API async `HttpClient`(`/positions`)也必须传同一个 `proxy_url`,避免周期 position 对账直连。
**验收**: `tests/arbitrage/config/test_loader.py::test_env_injects_polymarket_proxy_when_json_missing`、`test_json_polymarket_proxy_wins_over_env`、`tests/arbitrage/config/test_dispatcher.py::test_polymarket_exec_client_config_maps_proxy`、`tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_factory_configures_v2_http_proxy`、`test_polymarket_data_api_http_client_uses_proxy`;live 诊断中 NT pyo3 `WebSocketClient` 显式 `proxy_url=http://127.0.0.1:7890` 可连接 `wss://ws-subscriptions-clob.polymarket.com/ws/market`,PM CLOB REST 与 Data API `/positions` 也走同一配置路由。

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

### pm-adapter-5.1d: PM submit 本地/传输异常不拖满 session timeout

**前置**: `ArbPolymarketExecutionClient._submit_order` 已 `_begin_session`,但上游 PM submit 在收到 venue ack 前抛异常(例如 market WS / CLOB 路由 `Connection reset by peer`)。
**输入**: 一笔 PM `LimitOrder` 进入 `_submit_order`。
**期望**:
- 生成 `OrderDenied`,原因包含 `PM submit exception before venue acknowledgement`
- 立即结束当前 execution session,发布 `execution.finished`,释放 per-pair 执行闸
- 不生成 `OrderRejected`,不写 `VenueExecutionLiveness`,避免把本地/传输异常误写成 venue 真相可信
**验收**: live 日志应看到 `Polymarket submit failed before venue acknowledgement ...` 后不再等待 `Execution session timeout`。当前尚未补离线 fake-client 单测;后续可用 stub 上游 `_submit_order` 抛异常覆盖。

### pm-adapter-5.2: 撤单接口

**前置**: 已下一笔挂单
**输入**: `Strategy.cancel_order(...)`
**期望**: `_cancel_order` → CLOB `cancel_order(OrderPayload)`。响应 `canceled[]` 只表示撤单请求被 CLOB 接收,不立即 `generate_order_canceled`;真实完成以 USER WS `CANCELLATION` 事件为准。`not_canceled` 中的订单走 `generate_order_cancel_rejected`(其中 `already canceled or matched` 保持抑制,等待 WS/成交终态)。REST cancel 与 USER WS cancellation 可能重复到达,若 cache 中订单已是 `CANCELED` 必须跳过重复终态;若第一条 cancel terminal 已发出但 cache 尚未 apply,也必须用 client 侧有界去重窗口跳过重复终态。
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_cancel_order_success_waits_for_ws_cancellation_event` / `test_polymarket_cancel_success_skips_duplicate_canceled_order` / `test_polymarket_cancel_success_skips_duplicate_before_cache_updates` / `test_polymarket_cancel_order_reject_generates_cancel_rejected_event`;live cancel-only 需同时看到 `Execution session cancel-only` 与 `Cancel confirmed ...` / `OrderCanceled`,最后 venue `open_order_count=0`

### pm-adapter-5.3: Reconciliation(启动重连对账)

**前置**: 模拟 Strategy 重启后,venue 上有上一会话遗留的挂单
**输入**: NT TradingNode 启动 + ExecutionEngine reconciliation
**期望**: ExecutionEngine 调上游 `generate_order_status_reports` / `generate_position_status_reports`,把遗留挂单/仓位补到 Cache
**验收**: Cache 中能查到这些挂单,Strategy 可以选择保留或撤销

### pm-adapter-5.3b: PositionStatusReport 携带 PM 平均开仓成本

**前置**: PM Data API `/positions` 返回某 token 持仓,字段包含 `size` 与 `avgPrice`(或兼容旧名 `avg_price`)。
**输入**: `PolymarketExecutionClient.generate_position_status_reports(...)`
**步骤**:
1. 一次性分页拉取 `/positions`
2. 按 `{conditionId}-{asset}.POLYMARKET` 映射回 `InstrumentId`
3. 将 `size` 转为 `PositionStatusReport.quantity`
4. 将 `avgPrice` / `avg_price` 转为 `PositionStatusReport.avg_px_open`
**期望**: NT cache/Portfolio 中由该 report 派生的 Position 带真实平均成本;缺失或无法解析时 `avg_px_open=None`,但不得丢掉 quantity report,也不在 strategy/risk 侧估算。
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_position_report_maps_avg_price_from_data_api` / `test_polymarket_position_report_keeps_quantity_when_avg_price_unknown`

### pm-adapter-5.3c: PM order/position reconcile 写 VenueExecutionLiveness(2026-06-15)

**前置**: `ArbPolymarketExecutionClient` 注入共享 `VenueExecutionLiveness`;PM 初始 `order_alive=false`,`position_alive=false`。
**输入/期望**:
- PM order/open-order reconcile 成功并拿到完整真实 response → `pm_order_alive=true`
- PM position reconcile 成功并拿到完整真实 response → `pm_position_alive=true`
- 任一路径失败/超时/response 不完整 → 对应 alive 置 false,另一维不被误改
- **(#122 改)PM report 失败 = `mark_*_dead + 返空`(不再 raise)**:PM 上游 RetryManager 因网络/SSL 失败返回 `None` 而非抛异常时,Arb 子类仍须识别为 query failure 并 `pm_order_alive=false`(不能把空 reports 当真实空响应),但**处理改为 `mark_dead` 后 `return []`/`None`,不再 `raise`**——对齐 OE base(`orbitexch/execution.py:691/733`),避免 startup reconciliation 因 PM 瞬时失败抛异常 →「Execution state could not be reconciled」→ kernel 跳过 `trader.start()` → actors 卡 READY、web 不绑。venue 仍 fail-closed(标 dead),靠后续成功对账自愈;**不**在容忍时 mark_alive。用例:`test_polymarket_client.py::test_arb_generate_position_reports_failure_marks_dead` / `test_arb_generate_order_reports_retry_failure_marks_dead` / `test_arb_generate_single_order_report_retry_failure_marks_dead` / `test_arb_generate_order_reports_fill_retry_failure_marks_dead` 改断言 `reports==[]`/`report is None` + `not *_alive`(不再 `pytest.raises`)。
- PM `generate_fill_reports` 扫用户历史 trades 时,遇到当前未加载/未匹配的 instrument 属于目标市场外历史成交,应 DEBUG 跳过、不刷 WARN、不影响 liveness;open-order report 的未知 instrument 仍保留 WARNING。
- `venues.polymarket.max_retries` / `retry_delay_initial_ms` / `retry_delay_max_ms` 只做显式透传,默认 None,避免无意改变真钱 submit/cancel 语义;若为周期 order 对账抗瞬时 SSL/proxy timeout 显式开启,需知道上游同一 retry pool 也覆盖 PM submit/cancel/report。
**验收**:
- PM `venue_alive` 只由 `pm_order_alive && pm_position_alive` 派生,不存第三份状态。
- 不再调用旧 leg 状态;PM liveness 只由 report/position health check 成功路径写入。
- Risk 在 PM-only 或 PM+OE opportunity 中读到 PM 任一维 false 时 fail-closed deny。
- launcher `LiveExecEngineConfig.open_check_interval_secs=300` 周期触发 PM order reports;若前一轮 retry failure 置 `pm_order_alive=false`,后续真实拿到 open-order response(即使真实空 `[]`)会恢复 `pm_order_alive=true`。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_fill_history_unknown_instrument_is_debug_noise` 覆盖历史 fill 中未知 instrument 只打 DEBUG 并跳过。
- `tests/arbitrage/config/test_dispatcher.py::test_polymarket_exec_client_config_maps_retry_params` / `test_polymarket_exec_client_config_retry_params_default_none` 覆盖 PM retry 参数显式透传与默认不变。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_order_reports_retry_failure_marks_dead` / `test_arb_generate_single_order_report_retry_failure_marks_dead` / `test_arb_generate_order_reports_fill_retry_failure_marks_dead` 覆盖上游吞掉 retry failure 的 order-liveness fail-closed 行为。

### pm-adapter-5.account.1: 余额刷新触发(Q17)

**前置**: PM ExecutionClient 启动
**输入**: 分别触发 (a) `_connect()`;(b) 显式 `QueryAccount`; (c) PM `generate_position_status_reports(...)` 成功返回 reports。
**期望**: 三种情形各调一次 `_update_account_state` → `get_balance_allowance` → `generate_account_state` 写 cache;实时 `CONFIRMED` trade 只产 fill,不刷新余额。
**验收**:
- 上游 `execution.py` 内**无 `set_timer` 私有余额轮询**;周期刷新复用 NT 原生 position reconciliation。
- NT 无默认 `QueryAccount` 周期发送(全库仅反序列化处实例化)
- PM position reconciliation 成功后会刷新余额,用于覆盖 accepted 本地预扣后的保守 cache `free`。
- position reports 成功但余额刷新失败时,只 warning;reports 原样返回,`pm_position_alive` 保持 true。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_realtime_fill_waits_for_confirmed_status` 覆盖实时 trade: `MATCHED` 不产 NT fill,`CONFIRMED` 才按成交量产 fill。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_realtime_maker_fill_uses_maker_order_fields` 覆盖实时 maker trade:按 `maker_orders` 中属于本账户的 `order_id` / `matched_amount` / `price` 产 fill。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_marks_alive_and_dispatches_settlement` 覆盖 position reconcile 成功后余额刷新。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_balance_refresh_failure_does_not_fail_reconcile` 覆盖余额刷新失败不影响 position reconcile。

### pm-adapter-5.account.2: free=total 陷阱已由 accepted 本地预扣替代(Q17 修订已落地)

**前置**: PM `total=100`,一笔未成交挂单本应占用 60
**输入**: `_update_account_state` 发 `generate_account_state(reported=True, locked=0, free=100)`
**期望**:
- `CashAccount.apply()` 见 `is_reported` → 清空 `_balances_locked`(cash.pyx:178-179)
- 真值快照仍表示 PM CLOB 当前可用余额;accepted 后 execution session 会本地预扣并再次写 AccountState,使 cache `free` 成为保守可用余额
**验收**:
- 记录此为**已知行为**(Polymarket 链上不托管未撮合单,venue 视角 free=total 属实)
- 不再把“扣挂单”责任放到 `ArbitrageRiskEngine._check_balance`;PM 与 OE/SE 一样通过 execution accepted 事件预扣,公式为 `quantity * probability_from_price(POLYMARKET, price)`
- 风险侧只读 `account.balance_free(currency)`,避免 accepted 预扣与 Risk open-order 自扣双重扣减

---

### pm-adapter-5.health.1: PM 周期健康检查走 NT report 通路(Q13 / Q-L 翻盘)

> ⚠️ **部分失效(2026-06-15)**:`leg_settled=true` 相关验收退役;改为 pm-adapter-5.3c 的 `VenueExecutionLiveness` order/position alive 写入。健康检查/reconcile 仍走 NT report 通路。

**前置**: PM adapter 启动,健康检查周期 = 配置默认值。
**输入**: 等到下一个周期 tick 自然触发
**期望**:
- 拉一次持仓 + 挂单(REST / Data API;**不拉余额**,Q17)
- 持仓差异 → `generate_position_status_report(report)` → ExecutionEngine.reconcile → `cache.update_position` + `Portfolio.update_position` endpoint + `events.position.{strategy_id}` topic
- 挂单差异 → `generate_order_status_report(report)` → 同上 (orders 通道)
- 余额**不在此拉**(完全靠上游事件,见 pm-adapter-5.account.1)
- 拉取成功后 PM 对应 liveness 维度置 alive(见 pm-adapter-5.3c)
**验收**:
- Portfolio.unrealized_pnl / margins_init 与 venue 真实状态一致
- Strategy 订阅的 `on_position_event` / `on_order_event` 回调被触发(健康检查发现差异时)
- 静态搜索 PM adapter 健康检查代码: **必须**调 `generate_*_report`,**不得**直接 `cache.update_*`

### pm-adapter-5.health.4: NT 连续 position 对账内 merge/redeem(#110,2026-06-16;取代旧"健康检查 tick")

> **触发改了**:不再有 PM `HealthCheckLoop`/`_run_health_check`。merge/redeem 由 **NT 连续 position 对账**(`position_check_interval_secs=300`)周期调 `generate_position_status_reports(None)` 时触发。触发路径经 /live-test 验到 position reconcile override;真实链上 merge/redeem 需在具备 settlement 持仓且用户授权后另验。纯映射 `pm_raw_position_to_settlement` 离线测见 `test_polymarket_client.py`。

**前置**: 对账那次 `/positions` 原始响应里:condition A 两 outcome 都持仓(可 merge);condition B `redeemable=true`(可 redeem)
**输入**: NT 连续 position 对账周期到 → `generate_position_status_reports(None)`
**期望**:
- 上游 `_fetch_user_positions` 全量拉一次 /positions,stash `_last_raw_positions`(**一次拉喂两用**:报告 + 结算,不另起请求)
- 拉成功 → `mark_position_alive`;**拉失败 → `mark_position_dead` 并抛**(venue dead)
- **结算 fire-and-forget + single-flight**:`if _settlement and not _settlement_inflight: create_task(_run_settlement(raw))` —— 不 `await`(链上 tx 数秒,绝不阻塞 NT 对账循环 / inflight check);前一次未完成则本轮跳过
- `_run_settlement` 用 `pm_raw_position_to_settlement(item)`(原始 dict 键:`conditionId`/`size`/`negativeRisk`/`redeemable`)→ `PolymarketSettlement.run` → merge/redeem。
- `PolymarketContractService` 对标准二元走 `CtfCollateralAdapter + pUSD`,对 negRisk 走 `NegRiskCtfCollateralAdapter`;两者使用 inherited collateral-adapter ABI,negRisk redeem 由 adapter 自行读取调用者 YES/NO 链上余额。不再直接打底层 CTF+USDC.e,避免 merge 后资金停在页面 `Confirm pending deposit / Activate Funds`。
- 成功 merge/redeem 后当前默认不主动调用 `update_balance_allowance(COLLATERAL)`;切到 collateral adapter+pUSD 后先由 live 验证是否仍需手动同步。代码保留 `_sync_collateral_balance_allowance_after_settlement()` helper,恢复时也不主动 `_update_account_state`。
- 决策细节见 `tests/arbitrage/settlement/README.md`(settlement-8.x)
**验收**:
- launcher 构造并注入 `PolymarketSettlement`:cleanup 关闭或缺 PM 链上凭证时跳过;凭证齐全且 `PolymarketContractService.initialize()` 成功时接线;失败不阻塞节点启动。
- **无 `HealthCheckLoop`/`_run_health_check`/独立调度**(静态检查);链上编排在 `PolymarketSettlement`,不内联进 ExecutionClient
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_marks_alive_and_dispatches_settlement`:证明 PM override 成功路径会 `mark_position_alive` 并用同一次 `_last_raw_positions` fire-and-forget 触发 settlement。
- `tests/arbitrage/execution/test_polymarket_client.py::test_run_settlement_does_not_auto_sync_collateral_balance_after_successful_tx`:证明成功 merge/redeem 后当前默认不自动同步 CLOB collateral balance allowance。
- `tests/arbitrage/execution/test_polymarket_client.py::test_run_settlement_does_not_sync_collateral_balance_without_successful_tx`:证明没有成功 tx 时不触发同步。
- `tests/arbitrage/settlement/test_contract_offload.py::test_standard_merge_uses_ctf_collateral_adapter_and_pusd`:证明标准 merge 发往 collateral adapter 且使用 pUSD。
- `tests/arbitrage/settlement/test_contract_offload.py::test_neg_risk_merge_uses_neg_risk_ctf_collateral_adapter`:证明 negRisk merge 发往 negRisk collateral adapter。
- `tests/arbitrage/settlement/test_contract_offload.py::test_neg_risk_redeem_uses_inherited_collateral_adapter_abi`:证明 negRisk redeem 使用 collateral adapter 的 inherited selector、pUSD 参数与正确 target。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_failure_marks_dead`:证明 `/positions` report 失败会 `mark_position_dead` 并抛给 NT 对账。
- `tests/arbitrage/launchers/test_arb_node.py::test_make_pm_settlement_initializes_contract_and_flags`:证明 launcher 将 PM 链上凭证 / relayer 配置映射到 `PolymarketContractService`,并把 cleanup flags 传给 `PolymarketSettlement`。
- merge/redeem `TxResult` 失败:**仅 log,不判 `VenueExecutionLiveness` dead**;下个对账周期幂等重试(min(size))

### ~~pm-adapter-5.health.5: 执行在飞时健康检查 tick 跳过~~ —— 已退役(#110)

> ⚠️ **失效(#110)**:PM 无 HealthCheckLoop,merge/redeem 走 NT 对账。原"健康检查⊥执行"互斥(`_run_health_check` 的 `is_execution_active` 守卫)随之退役 —— 结算 fire-and-forget,不与执行互斥;并发由 single-flight 守卫防重复提交。NT 对账 vs OE 下单的兼容性见 synchronization §8(OE 下单 page.evaluate 与对账互不冲突)。
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

> **退役(#110)**:旧 `pm-adapter-5.schedule.*` 自写健康检查调度用例已删除。
> 当前 PM merge/redeem 由 NT 连续 position reconciliation 触发,不再有 PM
> `HealthCheckLoop` / `_run_health_check` / 独立健康检查调度。现行验收见
> `pm-adapter-5.3c` 与 settlement 相关用例。

> ⚠️ **失效横幅(#108,2026-06-15):以下 5.session.1 ~ 5.timeout.4 中所有 `leg_settled` 置位 / 状态机语义(尤其 5.health.2~5 整节)退役** —— `LegSettledRegistry` 已删除,执行健康真相改由 `ArbPolymarketExecutionClient` reconcile 写 `VenueExecutionLiveness`(order/position alive),现行验收见 **pm-adapter-5.3c**。**注意**:cancel-only / submit+track 的 session 入口与超时 watchdog 机制**本身仍有效**(见 synchronization §8.4 + execution §4.2),只是不再写 `leg_settled`。

> **2026-06-19 协调修正**:带完整 opportunity metadata 的多腿套利,跨 venue cancel-only 归 `ArbLiveExecutionEngine` barrier 统一判定(见 synchronization §8.4bis / execution §3.5)。本节 PM per-client cancel-only 只验无 metadata / fallback 的单 instrument 行为。

### pm-adapter-5.session.1: cancel-only session(残留挂单)(Q13)

**前置**: PM 上 token T 有未成交残留挂单
**输入**: strategy 调 `submit_order(new_order)`,execution 入口检查到残留
**期望**:
- session 退化为 cancel-only,**丢弃 `new_order`**
- 撤单 → track 到 CANCELED(#108 后不再写 `leg_settled`)
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

## #228:PM 3-way 暴露 NO token(2026-07-15)

- `test_arb_provider.py`:`_role_and_claim_for_token`(原 `_role_for_token`)3-way binary 路径 YES/NO 都产腿,role 同为所属 market 的 role,claim="yes"/"no" 区分;2-way / 单市场路径 claim 恒空。`test_load_moneyline_market_3way_binary_exposes_yes_and_no` 覆盖一个 binary market 产 2 条 instrument(info 带 claim);2-way 用例断言 `"claim" not in info`。

## #234:PM BUY-only 最小金额

- `test_parsing_min_size.py` 同时锁定市场 `minimum_order_size → BinaryOption.min_quantity` 与 `info["min_buy_notional"]=1.0`；`BinaryOption.min_notional` 保持 None，防止 SELL 被错误套用 1 USD 下限。
