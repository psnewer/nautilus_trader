# SharpExch 适配器测试计划

SharpExch(SE) 第一阶段按 OE 型 venue 接入,但测试独立成目录,避免把 SE 行为混进 OE 用例。设计见
`docs/arbitrage/architectures/sharpexch/architecture.md`。

## 范围

- `SharpExchInstrumentProvider`
- `SharpExchDataClient`
- `SharpExchExecutionClient`
- SharpExch factories / config dispatcher / env 注入
- PM↔SE matching、Risk venue liveness、skip/live smoke 验收

## 用例

### se-adapter-1.1:配置 schema 与 env 注入

**前置**:环境变量包含 `SHARPEXCH_USERNAME` / `SHARPEXCH_PASSWORD`。
**输入**:加载含 `discovery.sharpexch` / `venues.sharpexch` 的 `arb_config`。
**期望**:env 凭证注入 `cfg.venues.sharpexch`;JSON 中若写入凭证字段,触发 `ConfigWarning`;`venues.sharpexch.enabled` 默认 false,显式 true 才进 runtime;可用 `venues.orbitexch.enabled=false` 跑 PM+SE external smoke。
**验收**:已落地。`tests/arbitrage/config/test_loader.py` / `test_dispatcher.py` 覆盖 env 注入、JSON 凭证 warning、Data/Exec config 映射、`to_se_discovery_config`,以及 SE runtime 开关默认 false / JSON true 可解析 / external venues 从 enabled venue 推导。

### se-adapter-1.2:API discovery 生成 BettingInstrument

**前置**:fixture 来自 `POST /customer/api/sport/details?page=0&size=60`。
**输入**:`SharpExchDiscoveryClient.discover_events()` 返回 market events。
**期望**:只保留目标 competition 与 `Match Odds`;runner 映射为 `home/draw/away`;Provider 产出 `BettingInstrument`;InstrumentId 为 `{market_id}-{selection_id}.SHARPEXCH`。
**验收**:已落地。`test_discovery_client.py` 覆盖 API fixture 解析、competition 过滤、`sport_details_request` 生成 Wimbledon `sport/details` 请求、指定 page/size、`json_fetcher` 分页直到短页、重复页停机保护;`test_factories.py` 覆盖 Data factory 在 discovery config 存在时注入 browser `json_fetcher`,并把 `discovery.sharpexch.sports` 原样传入 Provider;`test_provider.py` 覆盖 Provider 产 `BettingInstrument`、`.SHARPEXCH` venue、Q9 info 六字段完整,以及 `load_all_async()` 把配置的 sport configs 传给 discovery。2026-07-01 zero-order probe 实测 browser fetcher 同源要求:必须在 customer iframe context 内执行 `sport/details` fetch;分页取回 242 个 Tennis events,其中 `Men's Wimbledon 2026` 为 64 个。

### se-adapter-1.3:SE 最小 stake 元数据

**前置**:确认 SE venue 最小 stake 配置。
**输入**:Provider 构造 instrument。
**期望**:`min_notional = Money(12, USD)`;Risk 不维护 SE 特殊常量。
**验收**:已落地。2026-07-01 真单 preflight 确认 SE 最小 stake 为 12 USD;`test_provider.py::test_build_legs_sets_usd_min_stake` 覆盖 USD 原生口径。

### se-adapter-2.1:价格 WS 转 OrderBookDeltas

**前置**:SE/OE 型 `multiple-market-prices` frame fixture;真实 SE frame 后续 live smoke 再替换/补充。
**输入**:`SharpExchMessageParser.parse_price_message(frame)` + `se_runner_to_book_deltas(iid, runner, ts)`。
**期望**:BACK 档转 BUY;LAY 档转 SELL;每帧先 CLEAR 再 ADD;空档返回 None。
**验收**:已落地。`test_message_parser.py` 覆盖 `bdatb/bdatl` dict 档与 `batb/batl` list 档;`test_data.py` 覆盖 CLEAR+BUY/SELL、空档、无效 size,以及 `se_price_message_to_book_deltas` 按 `selection_id -> InstrumentId` routing 生成多 runner deltas、跳过未订阅/空档 runner、坏消息返回空列表;同时覆盖 `se_market_price_message_to_book_deltas` 按 `market_id -> selection_id -> InstrumentId` routing 找 market、输出 `market_id/in_play/runners/subscribed_selections/deltas`、未路由 market/坏消息返回 None、已路由但空档时保留 frame 元信息并返回空 deltas;`se_publish_routed_book_deltas` 逐个调用注入 publish、可选写入 in_play、空 payload / 空 deltas no-op;`se_handle_price_frame` 组合 routing+publish,返回 `published_count`,未路由 no-op,空档返回 metadata 但不 publish。

### se-adapter-2.2:订阅即开 competition 页

**前置**:cache 中已有 SE instrument,含 sport_id/competition_id/market_id/selection_id。
**输入**:`_subscribe_order_book_deltas`。
**期望**:注册 `market_id + selection_id -> InstrumentId` routing;打开 `{base_url}/customer/sport/{sport_id}/competition/{competition_id}`;同一 competition 多腿并发订阅只开一页。
**验收**:DataClient runtime 生命周期边界与 routing/page-ref/open-reload 纯 helper 已落地。`test_data_client.py` 覆盖 `SharpExchDataClient` 离线构造、`_connect` 启动注入 browser manager、首轮加载 provider 并把 instruments 送入 DataEngine、`_disconnect` 取消周期发现 task 并停止 handlers、`_update_instruments` 单轮异常后下一轮继续、`_subscribe_order_book_deltas` 从 cache instrument 注册三张状态表并调用注入式 open/reload、同一 competition home/away 并发订阅经 `_comp_pages_lock` 只开一页、订阅开页失败后排 `_delayed_reopen`、`_delayed_reopen` 在仍订阅时重开/解订后 no-op/失败后再排下一轮、`_unsubscribe_order_book_deltas` 清理状态、`_on_price_frame` 发布 `OrderBookDeltas` 并写 `instrument.info["in_play"]`、未路由 market no-op。`test_data.py` 覆盖 `se_routing_entry_from_instrument` 把 market/selection 转字符串、缺字段返回 None,`se_update_market_routing` 写入 `market_id -> selection_id -> InstrumentId`,以及 `se_remove_market_routing` 解订移除;同时覆盖 `se_subscription_plan_from_instrument` 合并 routing/page ref、`se_update_subscription_state` 一次性写入 `market_id -> selection_id -> InstrumentId` / `market_id -> page_key` / `page_key -> (sport_id,competition_id)` 三张状态表且坏 instrument 无副作用,`se_remove_subscription_state` 解订时删除 selection、空 market、无引用 page ref,但保留同 market 其它 selection 与同 page 其它 market;同时覆盖 `se_competition_page_ref_from_instrument` 生成 page key `{sport_id}_{competition_id}`、缺 sport/competition 返回 None,`se_competition_page_url` 生成 competition URL,`se_should_reopen_missing_page` 只在未关停、页仍缺失、且仍有 market 订阅指向该 page_key 时允许重开,`se_reopen_missing_page` 在 gate 放行且 page ref 存在时调用注入 open-page、gate 拒绝/缺 ref 时 no-op、open 失败向上抛,`se_ensure_competition_page` 对已开页 no-op、缺页调用注入 open-page、坏 plan no-op、open 失败向上抛,`se_websocket_summary` 生成包含 active WS 与 frame count 的 open/reload 日志摘要,`se_wait_for_websocket_frames` 覆盖 prices 计数命中与超时,以及 `se_open_or_reload_competition_page` 新开时先 start handler 再 `goto(domcontentloaded)` 并短等 prices 首帧、reload 复用已有 page 并 `reload(domcontentloaded)` 后等 prices 新帧、新开失败 stop handler + close page。2026-07-01 skip_execution node smoke 已验证:SE data connected、PM↔SE MatchedPair 生成、两条 SE leg 订阅后只打开一次 `2_12597512` competition page;该页 prices WS 随后连接并收到首帧,但本轮未等到 SE routed price/OBD publish,仍作为后续 live smoke 观察项。

### se-adapter-2.3:prices WS liveness

**前置**:WS handler 绑定 prices feed。
**输入**:prices 心跳/业务帧、close、超时。
**期望**:prices 入向帧刷新存活锚;close/timeout 触发 competition reload;general/orders 心跳不掩盖 prices feed stale。
**验收**:基础 handler 与 reload 判定已部分落地。`test_websocket_handler.py` 覆盖 Playwright page listener start/stop、WS URL 分型、SockJS `a[...]` 解包、prices callback、general/order callback、上行 subscribe 帧跳过、首帧日志只打一次、close callback、坏 JSON 不进业务 callback。`test_data.py` 覆盖 `se_should_reload_on_disconnect`:只接受 `close:prices` / `liveness_timeout`,并处理 disconnecting、reload-in-flight、页不存在、冷却窗防护;同时覆盖 `se_reload_competition_on_disconnect` 组合 helper:gate 放行时 reload 已有页、gate 拒绝时不动作、reload 异常后清掉 reload-in-flight、缺 page ref 时不写 cooldown。`test_data_client.py` 覆盖 `SharpExchDataClient._on_comp_disconnect` 对 prices close 建 reload task、忽略 orders close、`_reload_comp_on_disconnect` 失败时不抛并清 reload-in-flight。NT clock liveness 仍待后续切片。

### se-adapter-5.1:登录与 general WS

**前置**:skip execution 模式,真实 SE 登录凭证来自 env。
**输入**:启动 `SharpExchExecutionClient._connect`。
**期望**:登录成功;捕获 general WS;`BALANCE` 帧生成 `AccountState(account_id=SHARPEXCH-001, base_currency=USD)`;`CURRENT_BETS` 帧刷新 `_current_bets`。
**验收**:general 帧纯 parser 已落地:`test_message_parser.py` 覆盖 `BALANCE` dict/嵌套 JSON、`CURRENT_BETS` list/嵌套 JSON、未知帧忽略、`parse_order_message` 兼容别名。余额纯映射已落地:`test_execution_translation.py` 覆盖 `se_balance_to_account_balances` 生成 USD 口径 `total=free=balance,locked=0`。SE runtime 现在按真实站点 USD 原生口径处理:`BALANCE` / `CURRENT_BETS` / fills / place payload 均不乘除 fx;`normalize_current_bets_to_usd` 纯函数保留,但 ExecutionClient 调用 `fx=1.0`。`SharpExchExecutionClient` 已接入 runtime,离线测试覆盖构造与生命周期:`test_execution_client.py` 覆盖 `_connect` 用 fake browser/page 启动共享 browser、创建 `"execution"` page、先注册 websocket handler再经 `se_login` 导航、生成 0 USD 初始 `AccountState`,`_disconnect` 停 WS handler 并清 page、`BALANCE` 生成 USD `AccountState` 并维护 `SE balance frame routed` 一次性锚点计数、fx command 不改变 SE USD runtime、未知帧忽略、`CURRENT_BETS` 刷新 USD 快照、维护 `SE CURRENT_BETS routed` 一次性锚点计数并标记 execution liveness。`test_websocket_handler.py` 覆盖 `on_frame` 对 SockJS 心跳也触发,供 ExecutionClient 刷新 general WS 存活锚;`test_execution_client.py` 覆盖 report 入口在无可信快照时 reload execution page 并等待 CURRENT_BETS 重推、超时则 fail-closed 标记 SE liveness dead。`test_probe_script.py` 覆盖 customer iframe 与登录表单并存时仍必须提交凭据、无登录表单时才复用 customer iframe、登录后 `postLoginPopup` 可见时点击主页面关闭、无弹窗时静默继续。2026-07-01 zero-order probe 复验:真实登录后 competition 页 general WS 收到 `BALANCE` 2 帧与 `CURRENT_BETS` 2 帧,parser 识别 4 个 general 业务帧;`OPEN_BETS_COUNTER` 仍可能出现 `DENIED/CLOSED`,不作为 execution hard gate。

### se-adapter-5.2:NT order 到 SE 下单 payload

**前置**:SE instrument + NT `LimitOrder`。
**输入**:`nt_order_to_legacy_order` 或 SE 等价转换函数。
**期望**:BUY → BACK,SELL → LAY;order quantity 直接作为 USD stake 发送;venue 字段为 `SHARPEXCH`,不能复用 `ORBITEXCH`。
**验收**:纯映射已部分落地。`test_execution_translation.py` 覆盖 BUY→BACK、SELL→LAY、`null_handicap`→0、真实 handicap 保留、缺 market/selection 返回 None。当前为守住“三不原则”未改共享 `Venue` enum,使用 SE 本地 `SharpExchLegacyOrder(venue="sharpexch")`。出站 payload 纯函数已落地:`se_order_to_place_bets_payload` 覆盖 USD `order.size` 原值、bad fx 拒绝、赔率夹紧、SE 前端真实字段(`page="competition"`,`persistenceType="LAPSE"`,`showLayOddsEnabled=false`,`betUuid` 随机后缀、普通单不发送 `fillOrKill=false`)。placeBets 响应解析已落地:`parse_place_bets_response` 覆盖 `offerIds[betUuid]`、fallback 第一个 offer id、fallback betUuid、全局错误与市场级错误。`SharpExchExecutor.place_order` page-bound 薄封装已离线落地:`test_executor.py` 用 fake page 覆盖 payload 发送、context cookie CSRF fallback、offer id 解析、无 page、bad fx;`test_probe_script.py` 覆盖 `se_customer_context` 优先使用 customer iframe;`test_execution_client.py` 覆盖 `_submit_order` 对 executor 成功生成 accepted、失败生成 rejected、异常时 reject 并结束 session、cancel-only 不触发下单,并覆盖 `_place_via_executor` 把 NT order 翻译为 `SharpExchLegacyOrder` 后传给 executor 与 execution page。2026-07-01 手动 DevTools capture 与自动 live probe 均验证 SE 前端 `BACK @100 size=12` 返回 `status=OK + offerIds`。

### se-adapter-5.3:CURRENT_BETS 到 order/position reports

**前置**:SE populated `CURRENT_BETS` fixture。
**输入**:parser + report 生成。
**期望**:offer/order id 能 join NT order;`sizeMatched` / `sizeRemaining` / `averagePrice` 正确转 fill/order progress;position report 聚合 BACK/LAY 净持仓。
**验收**:order/fill/position 纯函数已部分落地。`test_execution_translation.py` 覆盖 `current_bets_to_fills` 的 empty/unmatched/full delta/incremental/no-new-match/no-price/missing-offer 分支,`bet_order_progress` 的 accepted/partially_filled/filled/unknown、side/market/selection/price 透出、`sizePlaced` 优先与 fallback,以及 `current_bets_to_positions` 的单 BACK、BACK 加权均价、BACK/LAY 混合、LAY dominant、net zero、unmatched skip。`test_execution_client.py` 覆盖 `_on_current_bets` 对已 join 的 `offerId` 生成 `generate_order_filled`,成交 delta 用原始 `sizeMatched` 后按 fx 转 USD;同时覆盖 `generate_order_status_reports` / `generate_order_status_report` 从 `_current_bets` 生成 `OrderStatusReport`,按 venue_order_id 过滤单条 report,`generate_position_status_reports` 聚合净持仓,以及无 CURRENT_BETS 快照时 fail-closed 标记 liveness dead。真实 reload-then-report 仍待 `_connect`/page/WS 后续切片。

### se-adapter-5.4:SE 撤单 payload 与响应解析

**前置**:已有 `market_id` 与 `venue_order_id`。
**输入**:`se_order_to_cancel_bets_payload(market_id, venue_order_id)` 与 `parse_cancel_bets_response(response)`。
**期望**:payload 为 `/customer/api/cancelBets` 同源结构 `{market_id:[{"offerId": venue_order_id,"betType":"EXCHANGE"}]}`;缺 market/offer id 返回 None;无 error 的 dict 视为成功,空/非法/带 error 响应视为失败。
**验收**:已落地。`test_execution_translation.py` 覆盖 payload 正常结构、缺 market/offer id、完整 open bet 透传、成功响应、空响应、非法响应、error 响应。`SharpExchExecutor.cancel_order` page-bound 薄封装已离线落地:`test_executor.py` 用 fake page 覆盖 payload 发送、成功解析、无 page、缺 ids、完整 open bet 透传;`test_probe_script.py` 覆盖 `se_customer_context` 优先使用 customer iframe;`test_execution_client.py` 覆盖 `_cancel_order` 从 instrument 取 market_id、executor 成功后生成 canceled。2026-07-01 手动 DevTools capture 已验证 SE 前端 cancelBets 发送完整 open bet 对象并返回 `status=OK`。

### se-adapter-6.1:Risk required venues 支持 SE

**前置**:opportunity metadata 的 `expected_legs` 包含 SE leg。
**输入**:提交任意一条该 opportunity 的 order。
**期望**:Risk 从 metadata 推导 required venues 时包含 `SHARPEXCH`;SE order/position liveness 任一 false 时 deny;两者 true 时继续后续门控。
**验收**:已落地。`tests/arbitrage/risk/test_engine.py::test_liveness_gate_parses_sharpexch_expected_leg` 覆盖 `expected_legs` 中 `sharpexch:*` 被解析为 `SHARPEXCH`,SE liveness 缺失时 fail-closed、order/position 均 alive 后通过;同文件也覆盖 SE decimal odds 概率门控与 SE USD free 余额门控。

### se-adapter-7.1:Data/Exec factories 离线构造

**前置**:`ArbContext` 已准备 SE discovery config / venue liveness / arbitrage params。
**输入**:`SharpExchLiveDataClientFactory.create(...)` 与 `ArbSharpExchLiveExecClientFactory.create(...)`。
**期望**:Data/Exec factory 复用同一个 `ctx.se_browser_manager`;discovery config 缺失时 Data factory 使用占位 `InstrumentProvider`;discovery config 存在时构造 `SharpExchDiscoveryClient + SharpExchInstrumentProvider` 并按 `fx` 注入 Provider;Exec factory 缺 `venue_liveness` 早失败,存在时注入 liveness/session timeout/fx。
**验收**:已落地。`test_factories.py` 覆盖 discovery 缺失/存在两条 Data factory 分支、discovery 存在时注入 browser `json_fetcher`、Data+Exec 共享 browser manager、共享 `ctx.se_browser_lock` 串行化 data discovery fetch 与 exec login、Exec factory 缺 context 早失败、Exec factory 从 ArbContext 注入 `venue_liveness` / `se_session_timeout_secs` / `fx`。`test_arb_node.py` 覆盖 launcher 按 `venues.*.enabled` 注册 runtime:默认 PM+OE,PM anchor 不可关闭,至少一个 external venue,PM+SE smoke 不注册 OE,PM+OE+SE 同时注册两个 external venue。

### se-adapter-live.0:独立 zero-order 网站事实 probe

**前置**:用户明确要求启动真实浏览器;环境变量含 `SHARPEXCH_USERNAME` / `SHARPEXCH_PASSWORD`。
**输入**:`python3 -m scripts.se_probe --config arb_config.json --headed [--write-dir /tmp/se_probe]`。
**期望**:脚本只登录 SE、fetch `sport/details`、打开 competition 页、监听 prices/general WS;不启动 TradingNode,不进入 Matching/Strategy/Risk,不调用 placeBets/cancelBets。
**验收**:2026-07-01 已实跑通过:login ok,关闭登录后弹窗,`sport/details` 分页返回约 240 个 Tennis events(随 SE 当前赛事变化),其中 `Men's Wimbledon 2026` 为 64 个;打开 competition 页后 orders/prices WS active,price frame 可解析。登录判定修正后复验:`sport/details` 返回 251 个 Tennis events,competition 页 30s 采样中 price_frames=80/parsed_price=80,general_frames=20/parsed_general=4,balance_frames=2,current_bets_frames=2。后续发现 `_wait_after_login` 先等主 URL 会在 SE iframe app 下白等,已改为优先等 customer iframe;`se_fetch_json` 增 AbortController 超时,避免 `sport/details` 挂起时 probe 静默卡住。`--write-dir` 只写脱敏 `se_probe_redacted.json`。离线安全逻辑由 `test_probe_script.py` 覆盖:敏感字段脱敏、样本数量限制、competition URL 生成、customer iframe context 选择、competition 计数摘要、登录表单优先、post-login popup dismiss。

### se-adapter-live.1:skip live smoke

**前置**:`debug.skip_execution=true`,用户明确要求启动。
**输入**:启动含 PM+OE+SE 的 arb node。
**期望**:SE discovery 有 instruments;SE prices WS 有 first frame / routed price / OBD publish;SE general WS 有 first frame / balance;不发生真实下单。
**验收**:2026-07-01 用 `/tmp/arb_config_se_smoke.json` 实跑通过零下单烟测(`strategy.enabled=false`,`debug.skip_execution=true`,`web.enabled=false`):SE exec orders/prices WS first frame、`SE balance frame routed`、`SE CURRENT_BETS routed: bets=0`、`SharpExch login successful`、`SHARPEXCH-001` 初始 USD AccountState、SE data `SharpExchDataClient connected`、`Reconciliation for SHARPEXCH succeeded`、node RUNNING、PM↔SE `MatchedPair ...|SHARPEXCH`、两条 `.SHARPEXCH` `SubscribeOrderBook`、DataClient-SHARPEXCH competition page 只开一次并订阅两条 leg、SE competition orders/prices WS first frame、`SE price frame routed: market_id=1.259592581, runners=2, subscribed_selections=2`、`SE OrderBookDeltas published: instrument_id=1-259592581-19924831-None.SHARPEXCH, deltas=7`。未观察到真实下单。

### se-adapter-live.2:真单 place+cancel probe

**前置**:用户明确授权真单。
**输入**:`python3 -m scripts.se_place_cancel_probe --config arb_config.json --headed --confirm --size 12`。
**期望**:脚本先真登录并在 customer context 中 `sport/details` 发现一条 SE instrument;构造 `SELL/LAY @ 1.01` 小额单;`_submit_order` 取得 `venue_order_id`;`CURRENT_BETS` 出现 working order;`_cancel_order` 成功后该单消失或 `sizeRemaining=0`;finally 按 CURRENT_BETS 兜底撤所有 SE 活单。
**验收**:已 live 通过。2026-07-01 用户明确授权后,为避开 Cloudflare discovery 抖动,使用已知 `market_id=1.259494210/selection_id=8960879` 跳过 `sport/details` 执行最小真单 probe:`BACK @100,size=12` → `venue_order_id=22157223`;`CURRENT_BETS` 收到 2 帧并显示该单 `remaining=12.0,matched=0.0`;`generate_order_status_reports` 派生 1 条 report;随后 `_cancel_order` 成功,该单消失/remaining=0;finally 兜底活单数 0。脚本默认 dry-run,不带 `--confirm` 不调用 placeBets;`--cleanup-only` 零下单,只按 CURRENT_BETS 撤活单。
