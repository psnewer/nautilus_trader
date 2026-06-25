# web 测试

对应章节: `refactor.md §5.7`;详细设计 `architectures/web/architecture.md §8`

**落地状态(2026-06-23)**:Step 7 **完整控制台页面**(忠实照搬 legacy Bootstrap 标签页 + 控制 + 只读监控)已实现(`src/arbitrage/web/{actor,app}.py` + `static/console.html`)。`tests/arbitrage/web/test_web_gateway.py` 通过。

**演进**:#118 只读监控 MVP → #119 控制台 → #120 一度移除监控只留控制面 → **#123 照搬 legacy 完整页面、监控随页面加回**:`GET /`(serve HTML)+ `/accounts`(余额)/`/instruments`(发现)/`/matched_pairs`(匹配)/`/odds`(盘口,OE 1/odds 换算隐含概率)+ 控制台(启停 + 各 config 段编辑);删 legacy 死面板/死字段。

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

### web-7.13: 完整页面 + 只读监控端点(#123,照搬 legacy)
- 期望: `GET /` 返 legacy 风格 HTML 标签页;`/accounts`/`/instruments`/`/matched_pairs`/`/odds` 返各自只读快照;`/odds` 的 OE 腿前端按 `1/赔率` 换算成隐含概率(bid/ask 互换使 bid≤ask)与 PM 统一,原赔率括号留存
- 验收: `test_index_serves_html` / `test_get_accounts` / `test_get_instruments` / `test_get_matched_pairs` / `test_get_odds`
- 注: Market Matching 表 PM/OE 列显示腿数(二元盘=2 legs=home+away)

### web-7.x: scaffolding(端口预检 / WS 背压 / 退订 / 优雅停机)
- `test_port_bindable_detects_free_and_occupied`、`test_enqueue_drops_oldest_when_full`、`test_unregister_stops_broadcast`、`test_ws_sends_queued_message_then_closes_on_poison`(on_stop 毒丸优雅关 WS)

### 已知取舍 / 待 live
- HALTED 期间 strategy 仍评估、submit 在 egress 被拒 → churn(用户 2026-06-21 定:不联动 strategy)
- **live 验证已跑(2026-06-23)**:boot HALTED → POST ACTIVE 翻 ACTIVE → POST HALTED 翻回;PM geoblock 时(#122)节点仍正常起、web 绑;OE odds 换算概率展示

## 删除 / 延后(见 web/architecture.md §7)

- **删除(NT 无对应)**:legacy Run Discovery/Matching、Subscribe Odds、pipeline start/stop;Execution market_order/discount/take_off;Risk global_sl/健康检查间隔/返水率面板。`/positions/{pair_id}` way_rebate 端点不恢复(way_rebate #121 退役)。
- **延后**:OrderBookDelta firehose 实时推(现用 /odds 周期快照);strategy 可视化 Condition 树编辑(现 JSON 原始编辑)。
