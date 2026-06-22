# web 测试

对应章节: `refactor.md §5.7`;详细设计 `architectures/web/architecture.md §8`

**落地状态(2026-06-21)**:Step 7 **控制台**(TradingState 启停 + 配置编辑)已实现(`src/arbitrage/web/actor.py` + `app.py`)。
`tests/arbitrage/web/test_web_gateway.py` 通过。范围:控制面后端 JSON/WS。

**演进**:#118 只读监控 MVP(余额/matched_pairs/way_rebate)→ #119 加控制台 → **#120 移除只读监控 endpoint**(用户裁定,监控看日志,web 只留控制面)。

## 锁定的关键性约束

- `WebGatewayActor(Actor)` 与 NT TradingNode 同进程同 loop,FastAPI uvicorn 协程在 `on_start` 启动(`_NoSignalServer` 不抢 NT 信号;`get_running_loop` 取真 loop;`_port_bindable` 预检)
- **控制写经 MessageBus 命令**(方案乙),读经 `risk_engine` 引用 + `arb_config.json` 文件;不发交易命令、不订监控/行情 topic
- WebSocket 只推 `TradingStateChanged`(订 `events.risk`)
- 默认 `web.enabled=false` → launcher 根本不构造该 Actor、不占端口;`host` 默认 `127.0.0.1` 只绑本机;`start_halted=true` boot 即 HALTED
- WS 背压:每 client 一个 `asyncio.Queue(maxsize=256)`,满则丢最旧(绝不反压交易回调)

## 控制台用例(#119,详细设计 web §8)

### web-7.8: TradingState 启停(POST/GET /control/trading_state)
- 前置: 控制台启用,boot 默认 HALTED
- 期望: `POST /control/trading_state {state:ACTIVE|HALTED}` → publish `command.arb.trading_state`(方案乙);非法 state → 400;`GET` 读 risk_engine 当前 state;WS 推 `{type:trading_state}`
- 验收: web 端 `test_set_trading_state_publishes_command` / `test_post_trading_state_ok` / `test_post_trading_state_invalid_400` / `test_get_trading_state` / `test_trading_state_reads_risk_engine` / `test_on_risk_event_broadcasts_trading_state`;risk 端 end-to-end `tests/arbitrage/risk/test_engine.py::test_trading_state_command_halts_and_resumes` / `test_invalid_trading_state_command_ignored`

### web-7.9: 配置热改(PUT /config/risk,C 混合热段)
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

### web-7.x: scaffolding(端口预检 / WS 背压 / 退订 / 优雅停机)
- `test_port_bindable_detects_free_and_occupied`、`test_enqueue_drops_oldest_when_full`、`test_unregister_stops_broadcast`、`test_ws_sends_queued_message_then_closes_on_poison`(on_stop 毒丸优雅关 WS)

### 已知取舍 / 待 live
- HALTED 期间 strategy 仍评估、submit 在 egress 被拒 → churn(用户 2026-06-21 定:不联动 strategy)
- **live 验证待跑**:真节点 boot HALTED → 点 Start 转 ACTIVE → 下单放行;改 risk 参数热生效

## 已移除 / 延后(见 web/architecture.md §7)

- **已移除(#120)**:web-7.1(`GET /matched_pairs`)、web-7.3(`/accounts` + 余额 WS)、web-7.6/7.7(`/positions/*`)及 account/MatchedPair 订阅/推送。监控看日志;代码可从 git `ae1d397b18` 找回。
- **延后**:web-7.2 OrderBookDelta firehose;web-7.4 已并入控制台(refresh_interval 热改);静态 HTML/JS 前端。
