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
| `test_arb_provider.py` / `test_gamma_keyset.py` | **#55/#57/#289** series-based 发现：前者覆盖 teams/title/role 纯映射；后者覆盖 `/events/keyset` 多页聚合、旧 limit/offset 清理、游标传递、非法 schema 与重复 cursor fail-closed |
| `test_sports.py` | **#60/#127/#273** PM Sports 比分信号 + PMSPORTS synthetic anchor(`sports.py`)：解析、状态管线、per-game 订阅、NT 原生 `WebSocketClient` 接线与 app-level pong。Sports WS 显式复用 PM proxy，初连后台重试、连接后由 NT client 断线重连；协议层 ping/pong 由 NT client 处理。**映射键 `game_id`** == gamma `event["gameId"]`；eviction 由 `ended` 驱动(matching,见 matching README) |
| `test_data_client_ws_retry.py` | PM DataClient market WS 启动连接失败后保留订阅并重试;disconnect/no subscriptions 不重试;首个 `OrderBookDeltas` 发布计数/日志锚点 |
| `test_data_client_ws_retry.py::test_update_instruments_continues_after_provider_error` | **2026-06-29 overnight 修**:PM 周期 instrument rediscovery 单轮 `initialize(reload=True)` 抛异常后 task 不退出,下一轮仍继续并成功 `_send_all_instruments_to_data_engine` |
| `test_execution_ack.py` | **#256 起** PM ack 只来自 WS(不再回执即 ack):14 tests —— `_mark_accepted_emitted` 去重(首次 True / 重复 False / 有界挤出最老)、HTTP 回执成功只索引 `venue_order_id`(`_post_signed_order`/`_process_batch_response` 均不 `generate_order_accepted`)、失败只 reject、WS `PLACEMENT` 对 SUBMITTED 单 ack / 已 ack 则幂等跳过、**order 消息 `UPDATE`/`CANCELLATION` 到达同样 ack**(`CANCELLATION` 先补 ack 再走既有撤单终态)、WS trade 消息 MATCHED/MINED/CONFIRMED 任一先到达即 ack(taker 单无 PLACEMENT,靠这条证明已被接收)、PLACEMENT 与 trade 消息共用去重表不重复 ack、CONFIRMED 补 ack 之余仍正常生成 fill。见 execution architecture §3.1 #256(#253 已失效)。|
| `test_parsing_min_size.py` / `tests/integration_tests/adapters/polymarket/test_parsing.py` | 上游 PM market payload → `BinaryOption` 翻译；`minimum_order_size` 映射到两侧通用 `min_quantity`，BUY-only 1 USD 映射到 `info.min_buy_notional` |
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
- `ArbPolymarketInstrumentProvider.load_all_async` 走 Gamma `/sports` 取 series/order,再按 series 调 `/events/keyset?series_id=...` 游标分页拉内嵌 teams + markets；PMSPORTS 共用同一分页 helper。
- `_load_moneyline_market` 只接 moneyline 主市场,创建 PM token 后写 `sport/competition/home_team/away_team/selection_role/game_id`;`selection_role` 与 OE/SE 对齐为 `home/draw/away`;`start_ts` 不写入 matching info。
- 验收:`test_arb_provider.py` 覆盖 teams/title 解析、role 解析、moneyline instrument info 写入；`test_gamma_keyset.py` 覆盖分页协议；真实 Gamma keyset 完整性仍由 live smoke 验。

### pm-adapter-1.3: PM 两侧最小下单 share 映射

**前置**: PM Gamma/CLOB market payload 含 `minimum_order_size`(来自 `orderMinSize`)。
**输入**: `parse_polymarket_instrument(market_info, token_id, outcome, ...)`
**期望**: 产出的 `BinaryOption.min_quantity` 等于 `minimum_order_size`(当前 venue 默认 5 shares)，且不再生成 `info["min_buy_quantity"]`；BUY-only 的 `info["min_buy_notional"]` 保持 1 USD。
**验收**: `tests/arbitrage/adapters/polymarket/test_parsing_min_size.py::test_parse_polymarket_instrument_sets_two_sided_share_minimum`

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
**期望**: `cfg.venues.polymarket.proxy_url` 被注入并透传到 PM Data/Exec config;JSON 显式 `proxy_url` 优先于 env。#98 起同一个 `proxy_url` 也必须配置到 `py_clob_client_v2` CLOB REST transport,显式代理存在时关闭环境代理继承,确保 WS/REST 同路由;transport 固定 `retries=1`,只覆盖 `ConnectError` / `ConnectTimeout`；共享 client 的 connect/TLS timeout 为 15s，read/write/pool timeout 保持 5s。#111 起 PM ExecClient 内部 Data API async `HttpClient`(`/positions`)也必须传同一个 `proxy_url`,避免周期 position 对账直连。
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

### pm-adapter-2.5: PM source/binary 同一 market 的 OBD 批次(#357/#358)

**前置**:同一 condition 的 YES/NO instrument 已建立 market custom-data 订阅。
**输入**:两个 asset 的初始 snapshot 分别到达，以及一条同时含 YES/NO change 的
`price_change` WS 消息。
**步骤/期望**:首个 snapshot 只缓冲；第二个到达后只发一个含两腿的
`OrderBookFrameDeltas`，其内含一个 `MarketOrderBookDeltas`。PM condition 的
`source_market_id == binary_market_id`。snapshot 齐备前夹入的 price change 已写 local book，首批必须
从当前 local book 重建，不得回放旧 snapshot；bootstrap 后的单腿 resnapshot
立即发单腿 market 批次。后续同一 price-change 消息的多 asset 变化按
instrument 合并，也只发一个 market 批次；market-only 订阅不额外发
per-instrument OBD。
**验收**:`test_market_snapshots_wait_for_all_members_before_publish` /
`test_market_bootstrap_uses_latest_local_book_after_interleaved_quote` /
`test_quotes_publish_one_market_batch_for_all_changed_assets`。

### pm-adapter-2.6: PM source single-flight 与增量快照合流（#362）

**前置**：同一 condition 已有一个带 `frame_id` 的源帧进入 DataEngine、尚未收到
`OrderBookFrameProcessed`。**输入**：期间连续收到同 token 或 YES/NO 多 token 的增量。
**期望**：DataClient 不继续向 DataEngine 入队；每次增量仍先更新 `_local_books`，pending
从 local book 重建各变化 token 的最新完整 `CLEAR + ADD` 快照。收到当前 frame completion
后只 flush 一帧最新状态；退订后的迟到 completion 不复活旧帧，断线 CLEAR 不被后续 snapshot
覆盖。**验收**：`test_market_frame_waits_for_processed_before_flushing_pending`，以及
`tests/unit_tests/live/test_market_frame.py` 的合流/barrier/退订/拒绝用例。

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

### pm-adapter-5.1a: 订单级市价标记在 PM 最终提交边界转官方 market order

**前置**:订单 metadata 的 `market` 分别缺失/`false`/`true`；输入仍是 Strategy 生成的 NT
`LimitOrder`，计划 price/qty 不变。
**输入**:BUY `quantity=10,price=0.40` 与 SELL `quantity=10`。
**步骤**:订单通过通用校验后进入 `_submit_limit_order`，adapter 在提交边界读取 metadata。
**期望**:缺失/`false` 时委托上游 limit 提交；`true` 时构造 `MarketOrderArgs` 并以 FOK 提交。BUY
`amount=10×0.40=4 USDC`，SELL `amount=10 shares`；BUY 从 SDK 真实 `SignedOrderV2` 的扁平
`takerAmount` 字段取得 base quantity，并通过 `OrderUpdated` 对齐 NT 本地订单数量。BUY
`price=0`，由 SDK 动态取价；SELL `price=0.01`，扩大 FOK 扫盘范围且卖出量仍为 10 shares。
不得给 BUY 使用 `0.99` 极端签名价，也不得把 BUY 的 10 shares 直接作为极端价格限价单的
size，避免破坏 share/quote 口径。
**验收**:`tests/arbitrage/execution/test_polymarket_client.py::test_pm_order_without_market_metadata_delegates_to_upstream_limit` /
`test_pm_market_metadata_uses_official_market_order_at_submit_boundary`。

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

### pm-adapter-5.1d: PM submit 结果未知的一次性 in-flight 恢复

**前置**:`LiveExecEngineConfig.inflight_check_retries=1`;PM signed order 在 POST 前已有确定性 order hash。
**输入**:一笔 PM `LimitOrder` 的 POST 结果因超时/断线/回执丢失而不明确，订单保持 `SUBMITTED`;超过 NT in-flight threshold 后收到 `QueryOrder`。
**期望**:
- POST 前登记 `client_order_id -> order hash`;明确拒绝仍生成 `OrderRejected`，结果不明确不生成本地终态、不提前结束 session
- `QueryOrder` 只调用一次 `get_order`，不改变 PM order/position liveness
- 有效 report 经 `_send_order_status_report` 进入 ExecEngine 通用订单更新
- 异常、空响应、查不到或解析失败不发送 report；NT 不再次访问 venue
- 已卡在飞处理不调用 `_end_session`，不读写 `_active_sessions` / `pair_inflight`
**验收**:`test_polymarket_client.py` 覆盖 signed hash、submit 歧义、成功/失败均不改变 liveness、有效 report 更新与无 session 调用；launcher 测试锁定 `inflight_check_retries=1`。

### pm-adapter-5.1e: PM submit 明确拒绝与传输未知分流

**输入**:同一 signed order 的 POST 分别抛出带 HTTP 状态码的 `PolyApiException`，以及
`status_code=None` 的网络异常。
**期望**:前者立即生成 `OrderRejected` 并由标准终态收口 session；后者保持 `SUBMITTED`，
不伪造拒绝，交给 NT in-flight check。
**验收**:`test_polymarket_http_submit_rejection_is_not_ambiguous` /
`test_polymarket_transport_submit_failure_remains_ambiguous`。

### pm-adapter-5.1f: PM 页面手工 taker 成交更新本地 Position

**输入**:USER trade `CONFIRMED` 无本地 `client_order_id`，成交方向为 TAKER。
**期望**:adapter 先发送同一 venue order 的 `OrderStatusReport(ACCEPTED)` 建立 NT external order，
再发送真实 `FillReport`；不得让孤立 fill 因缺订单映射被 ExecEngine 丢弃。

### pm-adapter-5.1g: USER WS 枚举外状态映射为 rejected

**输入**:USER `order` 或 `trade` 消息的 `status` 不属于对应 Polymarket 枚举；实盘样本是
`event_type=trade` 的 FOK 未完全成交回执把 `CANCELED_` 与错误说明拼进 `status`。
**期望**:该消息不归入 timeout/断线/空回执；adapter 从原始 payload 提取 taker/maker/order id，
反查本地订单并生成 `OrderRejected`。行为不读取 `enable_timeout`；已知枚举仍走原标准解析。
**验收**:`test_polymarket_unknown_user_ws_status_generates_order_rejected` /
`test_polymarket_known_user_ws_status_is_not_reclassified` /
`test_polymarket_ws_decoder_routes_validation_error_to_unknown_status_handler`。
**验收**:`test_polymarket_external_taker_fill_bootstraps_order_before_fill` 锁定 report 顺序、方向、
数量与成交价。

### pm-adapter-5.1h: 单笔 post-only 透传 CLOB

**前置**：Strategy 已构造 `LimitOrder(is_post_only=True, time_in_force=GTC)`。
**输入/步骤**：调用 PM 单笔 `_post_signed_order`，检查传给官方 v2
`ClobClient.post_order(signed_order, order_type, post_only)` 的第三个参数。
**期望**：`order.is_post_only=True` 原样传为 `True`，由 CLOB 拒绝任何会立即成交的订单；
普通订单传 `False`，保持原 GTC 限价行为。当前 Strategy 逐张提交，不走批量 `post_orders`。
**验收**：✅ `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_post_only_is_forwarded_to_clob_submit`。

### pm-adapter-5.2: 撤单接口

**前置**: 已下一笔挂单
**输入**: `Strategy.cancel_order(...)`
**期望**: `_cancel_order` → CLOB `cancel_order(OrderPayload)`。响应 `canceled[]` 包含目标 venue order ID 时立即 `generate_order_canceled` 并结束 cancel session；不等待可能缺失的 USER WS `CANCELLATION`。迟到的 WS cancellation 必须按 cache 状态和 client 侧有界窗口幂等跳过。`not_canceled` 中的订单走 `generate_order_cancel_rejected`(其中 `already canceled or matched` 保持抑制,等待 WS/成交终态)。
**验收**: `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_cancel_order_success_generates_canceled_event_and_ends_session` / `test_polymarket_deferred_cancel_success_generates_canceled_event_and_ends_session` / `test_polymarket_cancel_success_skips_duplicate_canceled_order` / `test_polymarket_cancel_success_skips_duplicate_before_cache_updates` / `test_polymarket_cancel_order_reject_generates_cancel_rejected_event`;live cancel-only 需同时看到 `Execution session cancel-only` 与 `Cancel confirmed ...` / `OrderCanceled`,最后 venue `open_order_count=0`

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
- **PM report 查询失败 = adapter 恢复异常并继续抛**（#259）:默认 `max_retries=None`
  等价不重复请求，但 `RetryManager.run()` 仍会把第一次 `PolyApiException` 转成 `None`。Base
  `PolymarketExecutionClient` 的 order-list、single-order、fill 三个 report 方法各自在 manager
  释放前检查 `result`，失败恢复 `last_exception`；真实请求成功但结果为空才允许返回 `[]`/`None`。
  Arb 子类不再替换共享 retry pool，也不直接写 liveness；启动/周期 reconciliation 上层按返回或异常裁决。
- PM `generate_fill_reports` 扫用户历史 trades 时,遇到当前未加载/未匹配的 instrument 属于目标市场外历史成交,应 DEBUG 跳过、不刷 WARN、不影响 liveness;open-order report 的未知 instrument 仍保留 WARNING。
- `venues.polymarket.max_retries` / `retry_delay_initial_ms` / `retry_delay_max_ms` 只做显式透传,默认 None（等价 `max_retries=0`）,避免无意改变真钱 submit/cancel 语义;若显式开启,需知道上游同一 retry pool 也覆盖 PM submit/cancel/report。
**验收**:
- PM `venue_alive` 只由 `pm_order_alive && pm_position_alive` 派生,不存第三份状态。
- 不再调用旧 leg 状态;PM liveness 只由启动/周期 reconciliation 的查询结果写入。
- Risk 在 PM-only 或 PM+OE opportunity 中读到 PM 任一维 false 时 fail-closed deny。
- launcher `LiveExecEngineConfig.open_check_interval_secs=300` 周期触发 PM order reports;若前一轮 retry failure 置 `pm_order_alive=false`,后续真实拿到 open-order response(即使真实空 `[]`)会恢复 `pm_order_alive=true`。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_fill_history_unknown_instrument_is_debug_noise` 覆盖历史 fill 中未知 instrument 只打 DEBUG 并跳过。
- `tests/arbitrage/config/test_dispatcher.py::test_polymarket_exec_client_config_maps_retry_params` / `test_polymarket_exec_client_config_retry_params_default_none` 覆盖 PM retry 参数显式透传与默认不变。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_report_methods_restore_retry_manager_failure` 覆盖三个 base report 入口恢复异常；`test_arb_generate_order_reports_failure_does_not_write_liveness` / `test_arb_generate_single_order_report_failure_does_not_write_liveness` 锁定 adapter 不越权写 liveness，上层裁决见 execution README 4.5.1~4.5.3。

### pm-adapter-5.account.1: 余额刷新触发(Q17)

**前置**: PM ExecutionClient 启动
**输入**: 分别触发 (a) `_connect()`;(b) 显式 `QueryAccount`; (c) PM `generate_position_status_reports(...)` 成功返回 reports。
**期望**: 三种情形各调一次 `_update_account_state` → 单次 `get_balance_allowance` → `generate_account_state` 写 cache;请求失败立即抛给调用方,不在余额方法内部重试;实时 `CONFIRMED` trade 只产 fill,不刷新余额。
**验收**:
- 上游 `execution.py` 内**无 `set_timer` 私有余额轮询**;周期刷新复用 NT 原生 position reconciliation。
- NT 无默认 `QueryAccount` 周期发送(全库仅反序列化处实例化)
- PM position reconciliation 成功后会刷新余额,用于覆盖 accepted 本地预扣后的保守 cache `free`。
- position reports 成功但余额刷新失败时,只 warning;reports 原样返回,`pm_position_alive` 保持 true。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_realtime_fill_waits_for_confirmed_status` 覆盖实时 trade: `MATCHED` 不产 NT fill,`CONFIRMED` 才按成交量产 fill。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_realtime_maker_fill_uses_maker_order_fields` 覆盖实时 maker trade:按 `maker_orders` 中属于本账户的 `order_id` / `matched_amount` / `price` 产 fill。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_settles_without_writing_liveness` 覆盖 position reports 成功后余额刷新且 adapter 不写 liveness。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_balance_refresh_failure_does_not_fail_reconcile` 覆盖余额刷新失败不影响 position reconcile。
- `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_balance_query_failure_is_not_retried` 覆盖余额请求失败只调用一次 CLOB client。

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
- 上游第一次 `_fetch_user_positions` 拉 `/positions` 并 stash `_last_raw_positions`，仅用于
  reports 候选与 settlement 输入。
- settlement 在 report 协程内 await；没有尝试 merge 时返回第一次 reports；一旦尝试
  merge，无论结果成功或失败，都同轮再拉 `/positions`，只返回第二次 reports，然后拉
  `/closed-positions` 同步 realized PnL。
- 最终 reports 拉成功或失败均由方法返回/抛异常表达；启动/周期 reconciliation 上层据此
  `mark_position_alive/dead`。
- `_run_settlement` 用 `pm_raw_position_to_settlement(item)`(原始 dict 键:
  `conditionId`/`size`/`negativeRisk`/`redeemable`)→
  `PolymarketSettlement.run` → merge/redeem。
- #282/#331:position reconcile 另读 `/closed-positions`；current/closed 的 `realizedPnl` 是
  per-asset 累计快照，分别聚合后以 current 覆盖重叠 instrument，closed-only instrument 保留；
  最终本地状态校验通过后才写 `RealizedPnlLedger` 基线差，closed 查询失败保留旧基线。
- #308:order/position reconcile 返回携带请求前摘要的 `GuardedReports`；拉取成功由上层先 mark alive。
  `/closed-positions` 只作为 deferred payload，不在 adapter 内提交。状态变化由 ExecEngine 应用前
  统一丢弃，不把过期空 order 响应解释成 venue 缺单；网络失败由 adapter raise、上层 mark dead。
  position batch 把 deferred realized instrument（含无 PositionReport 的 closed-only 腿）也纳入摘要校验，
  准入并选择性提交后重取应用阶段摘要，避免 ledger revision 的预期变化
  让同批 report 自我失效；单份 report 与空 batch 派生的 flat report 均在最终 NT reconcile 入口复核。
  验收：`test_position_reconcile_returns_stale_guard_when_state_changes_during_fetch`、
  `test_position_reconcile_defers_realized_when_state_changes_during_closed_fetch`、
  `test_position_reconcile_returns_stale_guard_when_state_changes_during_balance_refresh`、
  `test_arb_generate_order_reports_returns_stale_guard_when_local_state_changes`，以及
  `test_engine_barrier.py::test_stale_{order_report_batch,position_report_batch,mass_status}_*`。
- merge 成功不另算 condition PnL、不生成 synthetic `OrderFilled`；真实账户样本确认 closed
  realized 已包含历史 merge，对账前旧持仓也已表达同一 outcome PnL。离线验收见
  `test_realized_by_instrument_aggregates_rows_within_one_snapshot`、
  `test_position_reconcile_deduplicates_realized_snapshot_overlap`、
  `test_position_reconcile_sets_external_minus_native_realized_baseline`。
- `PolymarketContractService` 对标准二元走 `CtfCollateralAdapter + pUSD`,对 negRisk 走 `NegRiskCtfCollateralAdapter`;两者使用 inherited collateral-adapter ABI,negRisk redeem 由 adapter 自行读取调用者 YES/NO 链上余额。不再直接打底层 CTF+USDC.e,避免 merge 后资金停在页面 `Confirm pending deposit / Activate Funds`。
- 成功 merge/redeem 后当前默认不主动调用 `update_balance_allowance(COLLATERAL)`;切到 collateral adapter+pUSD 后先由 live 验证是否仍需手动同步。代码保留 `_sync_collateral_balance_allowance_after_settlement()` helper,恢复时也不主动 `_update_account_state`。
- 决策细节见 `tests/arbitrage/settlement/README.md`(settlement-8.x)
**验收**:
- launcher 构造并注入 `PolymarketSettlement`:cleanup 关闭或缺 PM 链上凭证时跳过;凭证齐全且 `PolymarketContractService.initialize()` 成功时接线;失败不阻塞节点启动。
- **无 `HealthCheckLoop`/`_run_health_check`/独立调度**(静态检查);链上编排在 `PolymarketSettlement`,不内联进 ExecutionClient
- `test_arb_generate_position_reports_settles_without_writing_liveness`:证明无 merge 尝试时
  adapter 正常返回 reports 但不越权标活。
- `test_settlement_attempt_refetches_positions_before_returning_reports`:分别以 merge 成功和失败
  证明严格调用顺序均为 `positions → merge → positions → closed positions → balance`，
  且只返回第二次 reports。
- `tests/arbitrage/execution/test_polymarket_client.py::test_run_settlement_does_not_auto_sync_collateral_balance_after_successful_tx`:证明成功 merge/redeem 后当前默认不自动同步 CLOB collateral balance allowance。
- `tests/arbitrage/execution/test_polymarket_client.py::test_run_settlement_does_not_sync_collateral_balance_without_successful_tx`:证明没有成功 tx 时不触发同步。
- `tests/arbitrage/settlement/test_contract_offload.py::test_standard_merge_uses_ctf_collateral_adapter_and_pusd`:证明标准 merge 发往 collateral adapter 且使用 pUSD。
- `tests/arbitrage/settlement/test_contract_offload.py::test_neg_risk_merge_uses_neg_risk_ctf_collateral_adapter`:证明 negRisk merge 发往 negRisk collateral adapter。
- `tests/arbitrage/settlement/test_contract_offload.py::test_neg_risk_redeem_uses_inherited_collateral_adapter_abi`:证明 negRisk redeem 使用 collateral adapter 的 inherited selector、pUSD 参数与正确 target。
- `tests/arbitrage/execution/test_polymarket_client.py::test_arb_generate_position_reports_failure_does_not_write_liveness`:证明 `/positions` report 失败只向上抛；上层负责 `mark_position_dead`。
- `tests/arbitrage/launchers/test_arb_node.py::test_make_pm_settlement_initializes_contract_and_flags`:证明 launcher 将 PM 链上凭证 / relayer 配置映射到 `PolymarketContractService`,并把 cleanup flags 传给 `PolymarketSettlement`。
- merge/redeem `TxResult` 失败:**仅 log,不判 `VenueExecutionLiveness` dead**;下个对账周期幂等重试(min(size))

### ~~pm-adapter-5.health.5: 执行在飞时健康检查 tick 跳过~~ —— 已退役(#110)

> ⚠️ **失效(#110)**:PM 无 HealthCheckLoop,merge/redeem 走 NT 对账。原"健康检查⊥执行"互斥(`_run_health_check` 的 `is_execution_active` 守卫)随之退役。#283 起 settlement 在 position report 协程内 await；同步 SDK IO 已丢线程池，不阻塞 app loop；并发仍由 single-flight 守卫。
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
- order 在 cache 保持 `PENDING_CANCEL` 直至事件/reconcile 更新；一次 inflight QueryOrder 仍无有效报告时生成 `OrderCancelRejected(UNKNOWN)` 并恢复撤单前 open 状态
- strategy 下一轮调 submit 时重新识别该残单并再次走 cancel-only
**验收**: 未知撤单结果不伪造 `CANCELED`，残单仍可重撤

### pm-adapter-5.timeout.5: 超时 alert 不被 partial fill 重设(Q15)

**前置**: session timeout = 30s
**输入**: submit → 5s/15s/25s 各一笔 partial → 30s timeout fire
**期望**:
- timeout alert 仍在 `submit_ts + 30s` fire,与最后 partial 时刻无关
- 绝对超时语义:alert 一次性设在 submit 时刻 + timeout,partial 事件不重设
**验收**: 静态检查:partial fill 处理路径无 `set_time_alert_ns` 重设超时 alert 的调用

## #228:PM 3-way 暴露 NO token(2026-07-15)

- `test_arb_provider.py`:`_role_and_claim_for_token`(原 `_role_for_token`)3-way binary 路径 YES/NO 都产腿,role 同为所属 market 的 role,claim="yes"/"no" 区分;2-way / 单市场路径 claim 恒空。`test_load_moneyline_market_3way_binary_exposes_yes_and_no` 覆盖一个 binary market 产 2 条 instrument(info 带 claim);2-way 用例断言 `"claim" not in info`。

## #234/#346:PM 两侧最小 share 与 BUY-only 金额

- `test_parsing_min_size.py` 锁定市场 `minimum_order_size → BinaryOption.min_quantity`、`info["min_buy_notional"]=1.0`，并确认不再生成 `info["min_buy_quantity"]`：share 下限约束 BUY/SELL，1 USD 金额下限只约束 BUY。
- parsing 继续写 `info["order_size_increment"]=0.01`；Strategy submitter 的 SELL 路径向下取整，BUY 路径正常四舍五入，两侧均保留两位。验收见 strategy `test_submitter.py`。

## #250/#322:PMSPORTS CustomData 状态管线(已落地,`test_sports.py`)

> #322(2026-08-05,live-unvalidated):单通道整帧广播 → **按语义分通道(phase/score)+ 每通道变化才发**。
> phase 从统一布尔 `live`/`ended` 派生(PRE/IN_PLAY/POST),score 从 `score` 串 diff;matching/strategy 均只订 `phase`。
> 见 data §3.4.2、refactor #322。

### pm-adapter-sports.state.1:准入更新先写 Cache 再发布

**用例**:`test_processor_writes_store_before_publish`。
**期望/验收**:调用顺序严格为 `store.put` → publish;发布时 Store 已可读到本次完整状态。
#322:首帧对每个已订阅通道都视作变化 → put 后逐通道发布(`phase` 先于 `score`)。

### pm-adapter-sports.state.2:兴趣门控(定了就推,不定就不推)

**用例**:`test_processor_interest_gate_drops_unsubscribed_games`。
**期望/验收**:未订阅**任何通道**的比赛的帧不存不推;已订阅比赛正常入库并按通道发布。

### pm-adapter-sports.state.3:附加 filter 拒绝不污染 Cache

**用例**:`test_processor_filter_reject_keeps_store`。
**期望/验收**:Store 保持旧值,不发布(filter 为二级架构 seam,默认全收)。

### pm-adapter-sports.state.4:Store 写失败禁止发布

**用例**:`test_processor_store_write_failure_blocks_publish_and_retries`。
**期望/验收**:不发布;下一条更新仍可重试成功。

### pm-adapter-sports.state.5:per-(game,channel) DataType/topic

**用例**:`test_per_game_channel_data_types_route_to_distinct_topics` +
matching/strategy README 的接线用例(经 NT per-(game,channel) topic 路由到 consumer)。
**期望/验收**:每场每通道独立 topic `SportsGameUpdate.game_id=<gid>.channel=<ch>`;metadata 两键
参与 DataType 身份且键序固定(game_id→channel,保 publish/subscribe topic 串一致);
`game_id_of_data_type` / `channel_of_data_type` 正确反解。

### pm-adapter-sports.state.6:有效性规则与归零回收

**用例**:`test_processor_no_channel_change_refreshes_cache_without_publish` /
`test_processor_stale_frame_dropped` / `test_processor_rejects_frames_after_ended_terminal_state` /
`test_store_roundtrip_and_delete`。
**期望/验收**:phase 与 score 均未变的帧只刷时戳不发布(#322 逐通道"只存不发");`ts_event` 倒退丢弃;
ended 在 `phase` 通道放行一次后该场帧全拒(终态,覆盖退订异步小窗);Store codec roundtrip 正确,
`delete` 真删除(归零回收路径,依赖 #250 新增的 NT `Cache.delete`)。动态状态只存 Store,不写 `Instrument.info`。

### pm-adapter-sports.state.8:#322 逐通道分发 + 多通道 Store 回收

**用例**:`test_processor_score_change_only_publishes_score_channel` /
`test_processor_phase_change_only_publishes_phase_channel` /
`test_processor_only_publishes_subscribed_channels` /
`test_multichannel_store_reclaim_waits_for_all_channels`。
**期望/验收**:phase 不变只比分变 → 只发 `score`;比分不变 phase 跃迁 → 只发 `phase`;
未订阅的通道其字段变化不发布(但仍写 Store);同场订了 phase+score 时退订其一 Store 不回收,
两通道全归零才 `delete`(依赖基类先 `_remove_subscription` 再调 `_unsubscribe` 的时序)。

### pm-adapter-sports.state.9:比分变化日志

**用例**:`test_processor_logs_score_after_store_write_even_without_score_subscription`。
**期望/验收**:首个比分或比分变化在 Store 成功写入后记录 INFO，包含比赛、旧比分→新比分及
`period/elapsed/status`；只订 `phase` 时同样记录，但不额外发布 `score` 消息。

### pm-adapter-sports.state.7:NT 原生 WS 与显式代理

**用例**:`test_sports_ws_uses_nt_client_with_explicit_proxy` /
`test_sports_ws_initial_failure_retries` /
`test_sports_ws_text_ping_uses_nt_client_pong` /
`tests/arbitrage/config/test_dispatcher.py::test_sports_data_client_config_maps_data_source_url_and_pm_proxy`。
**期望/验收**:PMSPORTS 使用 NT pyo3 `WebSocketClient`;dispatcher 将 PM `proxy_url` 透传给
Sports config;禁用客户端主动 heartbeat;app-level `ping` 经同一 client 回复 `pong`。
初连失败由后台 task 每 5s 重试，连接成功后的断线重连归 NT client。

### pm-adapter-data.disconnect-clear：断线窗口盘口失效（#359/#360）

**用例**：native `test_send_waits_during_reconnection`、pyo3
`tests/integration_tests/network/test_websocket.py::test_reconnect_after_close`、wrapper
`test_handle_disconnect_reports_only_client_subscriptions`、DataClient
`test_ws_disconnect_clears_only_disconnected_shard_books` /
`test_ws_disconnect_does_not_clear_during_client_shutdown`。

**期望/验收**：NT Rust `WebSocketClient` 仅在 `Active → Reconnect` 成功切换时触发一次
`post_disconnection`，主动关闭不触发；Python binding 把回调调度到原 event loop；PM wrapper
只上报断线 `client_id` 所辖 token。DataClient 清空这些 token 对应的 local book，并通过
`OrderBookFrameDeltas` 清 DataEngine managed book；健康 WS 分片及其 token 不受影响。原生重连
成功后仍执行既有重新订阅，由新 snapshot 恢复盘口。离线已验证，尚未 live 验证。

### pm-adapter-exec.cancel.grouped:grouped cancel 复用同步预建 session

**前置**:Execution grouped CancelOrder barrier 已收齐并 release PM 撤单命令。
**输入**:command params 含 mixin 写入的 `arb_cancel_session_started=True`。
**期望/验收**:`ArbPolymarketExecutionClient` 把该标记透传给上游 PM `_cancel_order`，
不重复 `_begin_cancel_session`；真实 CLOB cancel 与 USER CANCELLATION 终态路径不变。
通用同步入口由 `tests/arbitrage/execution/test_session.py::
test_cancel_track_marks_execution_active_before_dispatch` 覆盖；PM 已预建 session 仍触达 venue
由 `test_polymarket_client.py::test_polymarket_residual_cancel_reaches_venue_despite_active_session`
覆盖。

### pm-adapter-sports.phase-store:#365 PMS 推进聚合 phase

**输入**：PMS `SportsGameUpdate` 依次为 IN_PLAY、POST。**期望**：详细 payload 仍写
`SportsGameStateStore`，同时在 phase 通道发布前推进共享 `SportsPhaseStore`；POST 仍只来自
PMS ended。全部 sports channel 退订归零时两个 Store 一并回收。**验收**：
`test_processor_advances_shared_phase_store_from_pms_state`，以及 common
`test_sports_phase.py` 的单调推进/删除用例。

### pm-adapter-exec.cancel.ack-policy:撤单请求 ACK 接入 session

**输入**:CLOB 返回任一正常撤单响应，再分别解析 `canceled[]` / `not_canceled`。
**期望/验收**:adapter 把正常响应的统一 ACK 交给共用 cancel session；reason 文本不参与 session
收口。`canceled[]` 包含目标订单时同时生成真实 `OrderCanceled`，不等待 USER WS。
接线由 `test_polymarket_cancel_order_success_generates_canceled_event_and_ends_session` 和
`test_polymarket_cancel_order_reject_generates_cancel_rejected_event` 验收；结果未知时结束 session
但不生成拒绝/撤单终态，由 `test_polymarket_cancel_order_unknown_result_ends_session` 验收。
