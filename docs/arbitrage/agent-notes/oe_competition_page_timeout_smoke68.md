---
name: oe_competition_page_timeout_smoke68
description: "Claude 最新会话迁移:#68 每 competition 一页后,competition 页保留 networkidle;OE 页面默认 timeout 统一为 120s;PM+OE 双边 OBD 同场到齐并触发 StrategyEvaluator 重评已用 NT-node skip=true live 验证。"
metadata:
  node_type: memory
  type: project
  originSessionId: 218d30e5-cb09-4c32-bdb4-6d3c7214a6f7
---

**2026-06-07 Claude 最新会话迁移(#68 后续).**

背景:OE DataClient 已从单 `inplay/highlights` 页改为**每 competition 一页**。订阅 OBD 时,`StrategyEvaluator` 对 matched pair 发 `SubscribeOrderBook`,OE DataClient 用 instrument 的 `event_type_id`(sport_id)+`competition_id` 打开 `/customer/sport/{sport_id}/competition/{competition_id}` 页面,并在 `goto` 前挂 `OrbitExchWebSocketHandler`。

关键调试结论:
- `/tmp/smoke68.log` 证明 NT node 路径真实跑到 `TradingNode RUNNING`、`MatchedPair ATP|Flavio Cobolli|Alexander Zverev`、PM/OE OBD subscribe 命令。
- 第一轮 smoke 失败点是 OE competition 页 `Page.goto: Timeout 30000ms exceeded ... waiting until "networkidle"`。当时没有价格 WS 日志/盘口帧,是因为 `goto` 超时打断订阅流程。
- Claude 中间一度推断应改 `domcontentloaded`,但随后核对老 `odds_client.py` 发现老代码也是 `networkidle`,只是显式用 `page_load_timeout_sec*1000`。最终落地应对齐老代码:保留 `networkidle`,并在 `page.goto/reload(..., timeout=timeout_ms)` 传入配置值。
- 当前代码已落地: `nautilus_trader/adapters/orbitexch/config.py` 默认 `page_timeout=120000`;`nautilus_trader/adapters/orbitexch/data.py` 的 `_open_or_reload_competition_page` 使用 `wait_until="networkidle", timeout=self._config.page_timeout`。

最后状态:
- 后续 `smoke68b(90s timeout)` 后台任务被停止/killed。任务输出只看到 `MatchedPair` 和 `OE competition page opened: 2_12803182`,没有足够输出证明价格 WS connected / 盘口 OBD delta 真流入策略。
- 因此**不能声明 #68 live smoke 已通过**。当前可信状态是:代码已按 120s 默认 timeout 修正,但仍需重新跑 `launchers/arb_node.py` skip=true smoke,并用日志确认:
  1. `OE competition page opened` 后无 `Page.goto TimeoutError`;
  2. OE price WS handler 有 connected/frame 日志;
  3. OE `OrderBookDelta` 真由 DataClient 流入 DataEngine/StrategyEvaluator,不是只看到 subscribe 命令;
  4. 若 PM WS 仍出现 `Operation timed out`,需单独归类为 PM 网络问题,不要混同为 OE #68 失败。

相关已迁移记忆:
- [[gap_c_oe_exec_live_validated]]:Gap C connect path 已经用 NT node skip=true live 验过;OE Tier 1 true place+cancel 不成交也已于 2026-06-08 验过。仍待的是 Tier 2 真成交 matched 帧。
- [[bug_pm_exec_connect_balance_fatal]]:PM CLOB 网络抖动会让 exec connect 失败或后续 WS 超时,判定 OE smoke 时需要区分。

**2026-06-07 Codex 接手后继续验证.**

预飞行:
- 使用 `arb_config.example.json`;`debug.enabled=true` 且 `debug.overrides.skip_execution.enabled=true,value=true`,因此真实登录/连接但不真下单。
- 无残留 `launchers.arb_node` / 旧 scenario runner 进程。
- 注意:2026-06-07 已补 `src/arbitrage/config/dispatcher.py` 映射,`venues.orbitexch.page_load_timeout_sec` 会传给 OE Data/Exec client 的 `page_timeout`;用户确认 OE 页面默认等待 120s。

第一次 Codex smoke(`python3 -u -m launchers.arb_node --config arb_config.example.json`,skip=true):
- PASS:PM/OE Data 与 Exec 均 Connected;OE 真余额 `37.49 GBP`;`TradingNode RUNNING`;`MatchedPair ATP|Flavio Cobolli|Alexander Zverev`;OE competition 页 90s timeout 路径不再出现 `Page.goto TimeoutError`。
- FAIL/风险:同一 `2_12803182` competition 页被打开两次,原因是 home/away 两腿并发订阅时 `_ensure_competition_page` 在 `await create_page(...)` 前未登记 in-flight 状态,两个协程都判断"未开"。
- 外部网络问题:PM market WS `_delayed_connect` 出现 `Connection reset by peer (os error 54)`。

已修复并验证:
- `OrbitExchDataClient` 增加 `_comp_pages_lock`,在 `_ensure_competition_page` 内包住 page_key 检查 + 首次 open,防止并发双开同一 competition 页。
- 新增单测 `test_concurrent_subscribe_same_competition_dedups_page`;目标测试 `tests/arbitrage/adapters/orbitexch/test_data_client_step2.py -q` 结果 `15 passed`。
- 同步 `docs/arbitrage/architectures/data/architecture.md` 和 `tests/arbitrage/adapters/orbitexch/README.md`。

第二次 Codex smoke(同命令,skip=true,修复后):
- PASS:同一 MatchedPair 的两条 OE 订阅只出现一次 `OE competition page opened: 2_12803182`,随后两条 OE subscribed 连续完成;并发双开竞态 live 复验通过。
- 仍未完全验证:未看到可证明 OE price WS connected/frame 或 StrategyEvaluator 基于 OE OBD 的评估输出。
- 阻塞因素:PM market WS 仍在 `_delayed_connect` 阶段 `Connection reset by peer (os error 54)`,双边 OBD 评估无法闭环。后续要继续 #68 真盘口验证,应先处理/规避 PM market WS 网络 reset,或者临时引入只验 OE price WS 的观测锚点(例如 DataClient 日志计数),避免把 PM 网络问题误判为 OE data 失败。

第三次 Codex smoke(加 DataClient 观测锚点后,同命令,skip=true):
- PASS:OE DataClient 自身日志确认真盘口流入:
  - `OE competition page opened: 2_12803182 (ws_count=0, ws_types={})`(页面 open 后立即看 active WS 仍可能为 0,不能作为失败条件);
  - 约 9 秒后 `OE price frame routed: market_id=1.258926623, runners=2, subscribed_selections=2`;
  - 随后 `OE OrderBookDeltas published: instrument_id=1-258926623-16570658-None.ORBITEXCH, deltas=7`。
- 结论:**#68 OE competition 页 + price WS → DataClient routing → NT `OrderBookDeltas` publish 已用 NT-node skip=true live 验证**。仍未验证的是 PM+OE 双边 OBD 共同驱动 StrategyEvaluator 完整评估/下单闭环。
- 代码补充:DataClient 低噪声锚点(`ws_count/ws_types`、首个 routed price frame、首个 `OrderBookDeltas`) + 计数器 `_price_frames_seen/_price_deltas_published`;目标单测仍 `15 passed`。

继续追 PM 阻塞点时发现:
- `ArbConfig.venues.polymarket.ws_url` 继承旧服务 full endpoint `wss://ws-subscriptions-clob.polymarket.com/ws/market`;
- NT 上游 `PolymarketWebSocketClient` 期望的是 base URL,内部会拼接 channel 名;
- 因此直接透传会让 DataClient 连接 `.../ws/marketmarket`,ExecClient 连接 `.../ws/marketuser`。

已修复:dispatcher 增加 PM WS URL 归一化,兼容旧 `.../ws/market` / `.../ws/user`,输出统一 `.../ws/`;`arb_config.example.json` 与 schema 默认值改为 `.../ws/`。后续 smoke 应重点看 PM market WS 是否能连到正确目标并产出 PM `OrderBookDeltas`。

后续 Codex smoke(同命令,skip=true,修 PM WS URL 后):
- PM/OE Data+Exec 均 connected;PM provider 加载 184 instruments;OE 余额先 0 后由账户状态更新回 37.49 GBP。
- Matching 仍产出 `MatchedPair ATP|Flavio Cobolli|Alexander Zverev`,StrategyEvaluator 发出 PM/OE 四条 `SubscribeOrderBook`。
- PM market WS 第一次连接出现 `Connection reset by peer`,但 DataClient 已改为保留订阅并自动重试;日志连续出现 `WebSocket connection failed ... retrying in 5s`,随后成功:
  - `PolymarketWebSocketClient: ws-client 0: Connected to wss://ws-subscriptions-clob.polymarket.com/ws/market with 2 subscriptions`
- OE 再次确认真盘口流入:`OE price frame routed` + `OE OrderBookDeltas published`。
- 仍未观察到 PM `OrderBookDeltas` / StrategyEvaluator 双边评估输出;已补 PM DataClient 首个 `PM OrderBookDeltas published` 低噪声锚点与计数器,下次 smoke 用该日志判断 PM 盘口是否进入 NT 数据管道。
- 新异常信号:`Order callback error: 'str' object has no attribute 'get'` ×2,发生在 OE exec/general 回调附近;与 PM market WS 修复无关。已定位为 `parse_general_frame` 只校验顶层 dict,但未校验 `BALANCE`/`CURRENT_BETS` payload 可能是嵌套 JSON 字符串或非 dict/list。已修复:parser 解嵌套 JSON,`BALANCE` 非 dict 忽略,`CURRENT_BETS` 非 list 忽略且过滤非 dict bet item;`test_ws_general_frames.py` 补覆盖。

再次 Codex smoke(同命令,skip=true,修 OE general parser 后):
- 启动 / 登录 / 关弹窗 / TradingNode RUNNING 正常;未再出现 `Order callback error`。
- Matching 仍产出同一 MatchedPair,StrategyEvaluator 发出 PM/OE 四条 OBD 订阅。
- PM market WS 多次 `Connection reset by peer` 后继续 retry,最终 connected:
  - `PolymarketWebSocketClient: ws-client 0: Connected to wss://ws-subscriptions-clob.polymarket.com/ws/market with 2 subscriptions`
  - `PM OrderBookDeltas published: instrument_id=..., deltas=48`
- 结论:**PM market WS → PM DataClient → NT `OrderBookDeltas` publish 已用 NT-node skip=true live 验证**。
- 本次仍未观察到 OE price frame / `OE OrderBookDeltas` 和 StrategyEvaluator 双边评估同场同时出现;此前 OE OBD 已单独验证过,后续要继续争取同一次 smoke 内 PM+OE 双边 OBD 到齐并观察 strategy evaluate。

Strategy evaluate 观测补强(2026-06-07 Codex):
- `StrategyEvaluatorConfig.log_evaluations` 原先存在但未使用,且 dispatcher 固定传 `False`。已补:
  - `StrategyEvaluator` 在 `log_evaluations=True` 时输出 schedule / skip / result / fire 锚点;
  - `ArbConfig.strategy.log_evaluations` → `to_strategy_evaluator_config` 映射;
  - `arb_config.example.json` 打开 `strategy.log_evaluations=true`(示例仍 `debug.skip_execution=true`,不会真下单)。
- 新增/更新测试:strategy evaluator 行为测试 +2(当前环境缺 async pytest 插件,该文件无法执行);dispatcher 映射测试 +1,可执行集合 `76 passed`。

最新 Codex smoke(前台命令,skip=true,PID 9303):
- 目标:用新增 `Strategy evaluate ...` 锚点确认 PM/OE OBD 同场进入 StrategyEvaluator。
- 结果:本轮未进入 discovery/matching/strategy。OE 首屏 connect 阶段超时:
  - `ExecClient-ORBITEXCH: Error on '_connect' TimeoutError(Page.goto ... waiting until "domcontentloaded")`;
  - `DataClient-ORBITEXCH: Error running '_connect' TimeoutError(Page.goto ... waiting until "networkidle")`;
  - 120s 后 `DataEngine.check_connected() == False`, `ExecEngine.check_connected() == False`。
- PM 侧在本轮已 connected,PM Sports feed 也收到首帧;但 OE data/exec connect 失败导致 actors 不会正式启动,因此**本轮不能用于判断 StrategyEvaluator / OBD 闭环**。
- 已停止 PID 9303,残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。
- 后续修复:确认 `venues.orbitexch.page_load_timeout_sec` 当时只传给 discovery scraper,未传给 OE Data/Exec client;同时 `BrowserManager.create_page` 默认 timeout 固定 30s。已补:
  - schema 默认与示例配置改为 120s;
  - dispatcher 将该字段转毫秒传给 `OrbitExchDataClientConfig.page_timeout` / `OrbitExchExecClientConfig.page_timeout`;
  - OE exec `_connect/_login` 对 execution page 设置 default timeout,并在 `goto` 显式传入配置 timeout。

再次 Codex smoke(接上 OE 90s timeout 后,skip=true,PID 10279):
- PASS:OE exec 首页 connect / login / 关弹窗成功;OE data/exec 均 Connected;MarketMatchingActor 与 StrategyEvaluator RUNNING。
- PASS:新增 `Strategy evaluate ...` 锚点生效:
  - `Strategy evaluate scheduled: pair_id=ATP|Flavio Cobolli|Alexander Zverev, ..., event=MatchedPair`;
  - `Strategy evaluate result: ..., arb_hit=False, comp_hit=False`;
  - `Strategy action skipped: ..., reason=no_pending_action`。
- FAIL/待修:OE competition 页 `/customer/sport/2/competition/12803182` 即使 90s 仍 `Page.goto ... waiting until "networkidle"` timeout。本轮没有 OE `OrderBookDeltas`。
- 新发现 bug:新开 competition page 时,代码在 `goto` 成功前就写入 `_comp_pages/_comp_handlers`;一旦 `goto` 超时,后续另一腿会复用一个未加载成功的 stale page,日志看起来像 subscribed,但真实价格 WS 没起来。
- 已修复:新 page 只有 `goto` 成功后才登记;失败时 stop WS handler + close page 并重抛,下一轮订阅可干净重试。新增 `test_open_page_failure_does_not_cache_stale_page`。

用户确认(2026-06-07):OE 页面默认都是等 120s 超时。已进一步统一:
- `ArbConfig.venues.orbitexch.page_load_timeout_sec` 默认 120.0;
- `arb_config.example.json` 为 120;
- `OrbitExch{Data,Exec}ClientConfig.page_timeout` 默认 120000;
- 旧 `config_loader.py` fallback 默认 120000。

再次 Codex smoke(统一 120s 后,skip=true,PID 11716):
- 目标:复验 OE 120s timeout + stale page 清理后,competition page 能否打开并产出 OBD。
- 结果:本轮未进入 discovery/matching/strategy,失败在 OE 首页 connect 阶段:
  - `DataClient-ORBITEXCH: Error running '_connect' Error(Page.goto: net::ERR_CONNECTION_RESET at https://www.orbitexch.com/ ... waiting until "networkidle")`;
  - `ExecClient-ORBITEXCH: Error on '_connect' Error(Page.goto: net::ERR_TIMED_OUT at https://www.orbitexch.com/ ... waiting until "domcontentloaded")`;
  - PM discovery 同时有一次 gamma fetch failed;PM Sports WS 也出现 ping timeout / reset 后重连。
- 120s 后节点状态:`DataEngine.check_connected() == False`, `ExecEngine.check_connected() == False`;actors 不会正式启动,因此**本轮不能判断 competition page / OBD / StrategyEvaluator 闭环**。
- 已停止 PID 11716,残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。

网络恢复后重跑(统一 120s 后,skip=true,PID 12458):
- PASS:OE 首页连接 / login / post-login popup / data connect / exec connect 均通过;MarketMatchingActor 与 StrategyEvaluator RUNNING。
- FAIL/外部阻塞:PM gamma discovery fetch `https://gamma-api.polymarket.com/events` 失败,`ArbPolymarketInstrumentProvider` 加载 0 instruments。
- 结果:无 PM instruments → 无 MatchedPair → 未发 OBD subscribe → 未触发 OE competition page,因此**本轮仍不能判断 competition page / OBD / StrategyEvaluator 闭环**。
- 已停止 PID 12458,残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。

再次重跑(统一 120s 后,skip=true,PID 13293):
- PASS:PM discovery 恢复,加载 184 instruments;OE data/exec connected;MarketMatchingActor/StrategyEvaluator RUNNING。
- PASS:MatchedPair `ATP|Flavio Cobolli|Alexander Zverev` 产出,StrategyEvaluator 发出 PM/OE 四条 OBD 订阅。
- PASS:OE competition page 120s 内打开:
  - `OE competition page opened: 2_12803182 (ws_count=2, ws_types={'orders': 1, 'prices': 1})`;
  - `OE price frame routed: market_id=1.258926623, runners=2, subscribed_selections=2`;
  - `OE OrderBookDeltas published: instrument_id=1-258926623-16570658-None.ORBITEXCH, deltas=7`;
  - `Strategy evaluate scheduled ... event=OrderBookDeltas` 随后出现,说明 OE OBD 已进 StrategyEvaluator 重评路径。
- 结论:**OE 120s competition page + prices WS + OBD publish + StrategyEvaluator OBD-driven 重评已用 NT-node skip=true live 验证通过**。
- 仍未闭环:PM market WS 未产出 `PM OrderBookDeltas published`;本轮出现 `WebSocket connection failed: ... Operation timed out`,进入 5s retry。未观察到 PM OBD,因此**PM+OE 双边 OBD 同场到齐仍未验证**。
- 已停止 PID 13293,残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。

再次重跑(统一 120s 后,skip=true,PID 14513):
- PASS:PM discovery 加载 184 instruments;OE exec/data connected;账户状态回报余额 37.49 GBP;MarketMatchingActor/StrategyEvaluator RUNNING。
- PASS:MatchedPair `ATP|Flavio Cobolli|Alexander Zverev` 产出,四条 OBD 订阅发出。
- PASS:OE competition page 打开并持续产出 OBD;StrategyEvaluator 持续收到 `event=OrderBookDeltas` 并重评,结果均为 `arb_hit=False, comp_hit=False, reason=no_pending_action`。
- FAIL/外部阻塞:PM market WS 多次 `Operation timed out (os error 60)`,每次按 5s retry;本轮未观察到 `PM OrderBookDeltas published`。
- 结论:OE 120s 页面/价格流/策略重评路径在连续 smoke 中稳定;PM WS 的 retry 机制在运行,但本轮 PM 行情通道未连通,**PM+OE 双边 OBD 同场到齐仍未验证**。
- 已停止 PID 14513,TradingNode 完整 STOPPED/DISPOSED;残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。

PM WS 根因补查与修复(2026-06-07 Codex):
- 用户指出不能只把 PM 视作外部网络问题。补做最小诊断:
  - 官方文档确认 PM market channel 仍是 `wss://ws-subscriptions-clob.polymarket.com/ws/market`,订阅消息仍用 `assets_ids`;
  - Python `websockets.connect(...)` 最小握手可连通,且 trace 显示其会自动走系统 proxy;
  - NT pyo3 `WebSocketClient` 显式 `proxy_url=http://127.0.0.1:7890` 后也可连通;
  - 现有 `ArbConfig.venues.polymarket` 无 `proxy_url`,dispatcher 也未把系统 proxy 传给 PM Data/Exec config,导致 pyo3 WS 直连 PM 在当前网络下 `Operation timed out`。
- 已修复:
  - `PolymarketSectionConfig.proxy_url`;
  - loader 在 JSON 未显式给 `proxy_url` 时按 `POLYMARKET_PROXY_URL` → `https_proxy`/`HTTPS_PROXY` → `http_proxy`/`HTTP_PROXY` 注入;
  - dispatcher 透传到 `PolymarketDataClientConfig.proxy_url` / `PolymarketExecClientConfig.proxy_url`;
  - 测试覆盖 loader env 注入、JSON 优先、Data/Exec proxy 透传。

修复后重跑(skip=true,PID 16127):
- PASS:PM discovery 加载 184 instruments;OE data/exec connected;MarketMatchingActor/StrategyEvaluator RUNNING。
- PASS:MatchedPair `ATP|Flavio Cobolli|Alexander Zverev` 产出并发出 PM/OE 四条 OBD 订阅。
- PASS:PM market WS 用代理路径连通:
  - `PolymarketWebSocketClient: ws-client 0: Connected to wss://ws-subscriptions-clob.polymarket.com/ws/market with 2 subscriptions`;
  - `PM OrderBookDeltas published: instrument_id=..., deltas=52`;
  - 随后 StrategyEvaluator 收到 `event=OrderBookDeltas` 并重评。
- PASS:OE 同场也产出:
  - `OE price frame routed: market_id=1.258926623, runners=2, subscribed_selections=2`;
  - `OE OrderBookDeltas published: instrument_id=1-258926623-16570658-None.ORBITEXCH, deltas=7`;
  - StrategyEvaluator 继续按 OBD 事件重评。
- 结论:**PM+OE 双边 OBD 同场到齐 + StrategyEvaluator OBD-driven 重评已用 NT-node skip=true live 验证通过**。本轮策略结果均 `arb_hit=False, comp_hit=False`,未触发 action。
- 已停止 PID 16127,TradingNode 完整 STOPPED/DISPOSED;残留 `launchers.arb_node|src.arbitrage.testing|uvicorn|web_gateway` 检查无匹配。
