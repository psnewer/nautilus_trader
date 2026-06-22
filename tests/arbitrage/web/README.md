# web 测试

对应章节: `refactor.md §5.7`;详细设计 `architectures/web/architecture.md`

**落地状态(2026-06-21)**:Step 7 **只读监控 MVP** 已实现(`src/arbitrage/web/actor.py` + `app.py`)+ **live 验证通过**。
`tests/arbitrage/web/test_web_gateway.py` **13 passed**。范围:只读后端 JSON/WS,不碰交易路径;
config-write / OrderBookDelta firehose / request-response 桥**延后**(见 web/architecture.md §7)。

**live 验证(2026-06-21,skip_execution 节点)**:`/health`✓、`/accounts` 返真实余额(PM 65.02 USDC.e / OE 37.49 GBP)✓、
`/positions/global_min_rebate_sum`✓、`/ws` 收到真 `matched_pair` 推送(ATP Fritz vs Tiafoe)✓。
验证中修了两个 live-only bug:① 端口被占(8080 被本机 Java 占用)uvicorn 静默失败 → 加 `_port_bindable` 预检 + serve-done error 回调;
② `create_task` 用了 `add_actors` 期捕获的 loop(非真运行 loop)→ serve 不执行不报错 → 改 `on_start` 内 `asyncio.get_running_loop()`。

## 锁定的关键性约束

- `WebGatewayActor(Actor)` 与 NT TradingNode 同进程同 loop,FastAPI uvicorn 协程在 `on_start` 启动(`_NoSignalServer` 不抢 NT 信号)
- **纯只读**:只调 `cache` 读方法 + `portfolio` pull 纯函数;不发命令、不 publish、不写 cache
- WebSocket 推送:订阅 `events.account.*`(NT Portfolio 发)+ `data.MatchedPair*`(MatchingActor 发)→ 转 JSON 推前端
- **承担余额数据推送**(替代独立 BalanceMonitorActor):浏览器订阅 AccountState 自己显示,余额低/熔断由用户看着判断
- 默认 `web.enabled=false` → launcher 根本不构造该 Actor、不占端口;`host` 默认 `127.0.0.1` 只绑本机
- WS 背压:每 client 一个 `asyncio.Queue(maxsize=256)`,满则丢最旧(监控面允许丢帧,绝不反压交易回调)

## 预期用例(已实现)

### web-7.1: HTTP `GET /matched_pairs` 拿匹配对
- 前置: MatchingActor publish 过 `MatchedPair`,Actor `_on_matched_pair` 已缓存
- 期望: 返回缓存的 pair 列表(pair_id/sport/competition/腿 id/confidence)
- 验收: `test_on_matched_pair_caches_and_broadcasts` + `test_get_matched_pairs`
- 注: MVP 用 Actor 订阅+缓存,**未**走 MessageBus request/response(延后)

### web-7.3: WebSocket / `GET /accounts` 推余额(替代 BalanceAlert)
- 前置: ExecutionClient 写过 AccountState(NT 发 `events.account.*`)
- 期望: WS 推 `{"type":"account","data":<AccountState.to_dict>}`;GET `/accounts` 返 cache 账户快照
- 验收: `test_on_account_state_broadcasts` + `test_account_state_serialization_roundtrip` + `test_get_accounts` + `test_ws_sends_queued_message_then_closes_on_poison`

### web-7.6: HTTP `GET /positions/{pair_id}` → portfolio pull
- 前置: 该 pair 有持仓
- 期望: 返回 `way_rebate` + `way_rebates_by_venue` + `outcome_exposures`(JSON)
- 验收: `test_get_positions_pair`(Q14,§6.9)

### web-7.7: HTTP `GET /positions/global_min_rebate_sum` → 数字
- 期望: `{"global_min_rebate_sum": <float>}`
- 验收: `test_get_global_min_rebate_sum_route_not_shadowed`(并验路由顺序先于 `{pair_id}`,不被路径参数吞)

### web-7.x: 队列背压 / 退订 / 优雅停机
- `test_enqueue_drops_oldest_when_full`(满丢最旧)、`test_unregister_stops_broadcast`、
  `test_ws_sends_queued_message_then_closes_on_poison`(on_stop 毒丸优雅关 WS)、`test_non_matching_event_ignored`

## 控制台(#119,详细设计 web §8)

### web-7.8: TradingState 启停(POST/GET /control/trading_state)
- 前置: 控制台启用,boot 默认 HALTED
- 期望: `POST /control/trading_state {state:ACTIVE|HALTED}` → publish `command.arb.trading_state`(方案乙);非法 state → 400;`GET` 读 risk_engine 当前 state
- 验收: web 端 `test_set_trading_state_publishes_command` / `test_post_trading_state_ok` / `test_post_trading_state_invalid_400` / `test_get_trading_state` / `test_trading_state_reads_risk_engine`;risk 端 end-to-end `tests/arbitrage/risk/test_engine.py::test_trading_state_command_halts_and_resumes` / `test_invalid_trading_state_command_ignored`

### web-7.9: 配置热改(PUT /config/risk,C 混合热段)
- 前置: `arb_config.json` 存在
- 期望: `PUT /config/risk {share,...}` → 写回文件 + publish `command.arb.risk_params` + `applied:live`;risk 引擎只覆盖给定字段
- 验收: `test_update_risk_config_writes_file_and_publishes_command` / `test_put_config_section`;risk 端 `test_risk_params_command_hot_updates_only_given_fields`

### web-7.10: 配置重启段(PUT /config/venues)
- 期望: 只写回文件、不发命令、`applied:on_restart`
- 验收: `test_update_restart_section_writes_file_no_command`

### web-7.11: refresh_interval 热改 + GET /config 快照
- 期望: `PUT /config/matching {refresh_interval_secs}` → publish `command.arb.refresh_interval` + `applied:live`;`GET /config` 返回 file + live(trading_state + risk params)
- 验收: web 端 `test_update_matching_refresh_interval_publishes` / `test_config_snapshot_returns_file_and_live` / `test_get_config`;matching 端 consumer `tests/arbitrage/matching/test_pair_registry.py::test_refresh_interval_command_hot_updates` / `test_refresh_interval_command_rejects_nonpositive`

### web-7.12: boot 默认 HALTED(launcher)
- 期望: `web.enabled && web.start_halted` → launcher build 后 `risk_engine.set_trading_state(HALTED)`;web 关则不动(保持 NT ACTIVE)
- 验收: `tests/arbitrage/launchers/test_arb_node.py::test_boot_halted_when_web_enabled_and_start_halted` / `test_no_boot_halt_when_web_disabled`

### 已知取舍 / 待 live
- HALTED 期间 strategy 仍评估、submit 在 egress 被拒 → churn(用户 2026-06-21 定:不联动 strategy)
- **live 验证待跑**:真节点 boot HALTED → 点 Start 转 ACTIVE → 下单放行;改 risk 参数热生效

## 延后用例(未实现,见 web/architecture.md §7)

- web-7.2: WebSocket 推送 `OrderBookDelta`(行情 firehose,量大需节流)
- web-7.4: HTTP POST 改 `refresh_interval` → MessageBus → Refresher 运行时生效(config-write)
- web-7.5: uvicorn 与 NT loop 共存优雅停机的 **live 验证**(单测已覆盖 on_stop 逻辑;真节点起停待 live)
