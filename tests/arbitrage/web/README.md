# web 测试

对应章节: `refactor.md §5.7`;详细设计 `architectures/web/architecture.md §8`

**落地状态(2026-06-23)**:Step 7 **完整控制台页面**(忠实照搬 legacy Bootstrap 标签页 + 控制 + 只读监控)已实现(`src/arbitrage/web/{actor,app}.py` + `static/console.html`)。`tests/arbitrage/web/test_web_gateway.py` 通过。

**演进**:#118 只读监控 MVP → #119 控制台 → #120 一度移除监控只留控制面 → **#123 照搬 legacy 完整页面、监控随页面加回**:`GET /`(serve HTML)+ `/accounts`(余额)/`/instruments`(发现)/`/matched_pairs`(匹配)/`/odds`(盘口,按 `odds_model` 换算隐含概率)+ 控制台(启停 + 各 config 段编辑);删 legacy 死面板/死字段。

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

### web-7.9: 配置热改(PUT /config/arbitrage 与 /config/risk,C 混合热段)
- 期望: `PUT /config/arbitrage {share,max_leg_share,fx}` → 写回文件 + publish `command.arb.arbitrage_params` + `applied:live`;RiskEngine / adapter 边界继续读取 live `ArbitrageParams.fx`,StrategyEvaluator 后续评估只读取 `share/max_leg_share` 作为默认规模参数
- 期望: `PUT /config/risk {match_tp,match_sl,min_probability,max_probability,...}` → 写回文件 + publish `command.arb.risk_params` + `applied:live`;risk 引擎只覆盖给定字段,概率上下界由 risk 组件侧校验
- 验收: `test_update_arbitrage_config_writes_file_and_publishes_command` / `test_update_risk_config_writes_file_and_publishes_command` / `test_put_config_section`;risk 端 `test_arbitrage_params_command_hot_updates_only_given_fields` / `test_risk_params_command_hot_updates_only_given_fields` / `test_probability_bounds_hot_update_rejects_invalid_interval`;strategy 端 `test_eval_context_strategy_defaults_read_arbitrage_params`

### web-7.10: 配置重启段(PUT /config/venues)
- 期望: 只写回文件、不发命令、`applied:on_restart`
- 验收: `test_update_restart_section_writes_file_no_command`

### web-7.11: refresh_interval 热改 + GET /config 快照
- 期望: `PUT /config/matching {refresh_interval_secs}` → publish `command.arb.refresh_interval` + `applied:live`;`GET /config` 返回 file + live(trading_state + risk params + arbitrage params)
- 验收: web 端 `test_update_matching_refresh_interval_publishes` / `test_config_snapshot_returns_file_and_live` / `test_get_config`;matching 端 consumer `tests/arbitrage/matching/test_pair_registry.py::test_refresh_interval_command_hot_updates` / `test_refresh_interval_command_rejects_nonpositive`

### web-7.12: boot 默认 HALTED(launcher)
- 期望: `web.enabled && web.start_halted` → launcher build 后 `risk_engine.set_trading_state(HALTED)`;web 关则不动(保持 NT ACTIVE)
- 验收: `tests/arbitrage/launchers/test_arb_node.py::test_boot_halted_when_web_enabled_and_start_halted` / `test_no_boot_halt_when_web_disabled`

### web-7.13: 完整页面 + 只读监控端点(#123,照搬 legacy)
- 期望: `GET /` 返 legacy 风格 HTML 标签页;`/accounts`/`/instruments`/`/matched_pairs`/`/odds` 返各自只读快照;Discovery 页面统计/过滤从 `/instruments` 实际 venue 动态生成;`/odds` 每条 leg 携带 `role`/`odds_model`,前端以 `Match` + `Role` + venue 概率列展示,并按 `decimal → 1/赔率`、`probability → 原样` 换算成统一隐含概率(bid/ask 对 decimal 互换使 bid≤ask)
- 验收: `test_index_serves_html` / `test_get_accounts` / `test_get_instruments` / `test_get_matched_pairs` / `test_get_odds`
- 注: Market Matching 表不再输出 `pm_*` / `oe_*` / `external_*` 旧字段;`/matched_pairs` 的 membership 以 `PairRegistry` 当前注册 pair 为准,`MatchedPair` 事件只保留 `sport`/`competition`/`confidence` 元数据缓存,因此 ended eviction / `unregister_pair()` 后页面同步消失。`venue_instrument_ids` / `tradable_instrument_ids` / `anchor_instrument_ids` / `venue_teams` 暴露当前 registry schema(2026-06-23 简化,2026-07-12 改为 registry 当前态)。`test_on_matched_pair_stores_venue_instrument_ids` 覆盖多 venue 元数据缓存;`test_on_matched_pair_handles_empty_venue_map` / `test_on_matched_pair_uses_explicit_venue_map_only` 覆盖空值与无兜底路径;`test_matched_pairs_exposes_all_venue_teams` 覆盖 PM+OE+SE 聚合展示;`test_matched_pairs_hides_unregistered_pair` 覆盖未 register 不显示、register 后显示、unregister 后消失。
- 注: 控制台页面不再把 `POLYMARKET` 写死为唯一主腿;Matching / Odds 表头按 `config.venues.*.enabled` 中的可交易 venue 动态生成列,列名就是真实 venue id,不展示 PMSPORTS/anchor;Matching 单元格从 `venue_teams[venue]` 取 `home vs away`,且页面不展示 Confidence 列(API 可继续返回 confidence);Odds 表固定展示 Role 列,3-way 在同一 pair 下以 home/draw/away 多行展示,各 venue 单元格展示统一隐含概率 bid/ask。`test_index_serves_html` 覆盖动态 venue 展示文案锚点。
- 注: Discovery Config 以 Polymarket / OrbitExch / SharpExch 标签页分别编辑 `discovery.polymarket.sports` / `discovery.orbitexch.sports` / `discovery.sharpexch.sports`;PMSPORTS 暂不单独展示,默认继承 `discovery.polymarket.sports`。`page_load_timeout_sec` / `staleness_timeout_sec` 是 OE/SE 共用 UI 值,保存时同步写入 `venues.orbitexch` 与 `venues.sharpexch`。`test_index_serves_html` 覆盖 SharpExch sports textarea 与统一 browser discovery 保存锚点。

### web-7.x: scaffolding(端口预检 / WS 背压 / 退订 / 优雅停机)
- `test_port_bindable_detects_free_and_occupied`、`test_enqueue_drops_oldest_when_full`、`test_unregister_stops_broadcast`、`test_ws_sends_queued_message_then_closes_on_poison`(on_stop 毒丸优雅关 WS)

### 已知取舍 / 待 live
- HALTED 期间 strategy 仍评估、submit 在 egress 被拒 → churn(用户 2026-06-21 定:不联动 strategy)
- **live 验证已跑(2026-06-23)**:boot HALTED → POST ACTIVE 翻 ACTIVE → POST HALTED 翻回;PM geoblock 时(#122)节点仍正常起、web 绑;OE odds 换算概率展示

## 删除 / 延后(见 web/architecture.md §7)

- **删除(NT 无对应)**:legacy Run Discovery/Matching、Subscribe Odds、pipeline start/stop;Execution market_order/discount/take_off;Risk global_sl/健康检查间隔/返水率面板。`/positions/{pair_id}` way_rebate 端点不恢复(way_rebate #121 退役)。
- **延后**:OrderBookDelta firehose 实时推(现用 /odds 周期快照);strategy 可视化 Condition 树编辑(现 JSON 原始编辑)。
