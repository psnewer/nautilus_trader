# launchers 测试

对应章节: `refactor.md #45`(slice 6);详细设计 `architectures/_cross-cutting/configuration.md §5.5`。

## Slice 6 落地(2026-05-28 #45)

`launchers/arb_node.py` —— NT 节点 launcher 骨架。

- ✅ `test_arb_node.py`:`build_trading_node_config` PM+OE × data+exec 4 client config 装配 / `prepare_runtime_state` 返共享件(VenueExecutionLiveness / PairRegistry / PairInFlightGate / DebugConfig 可 None)/ `register_factories` 调 add_*_factory / `bootstrap_and_build` 调用顺序(install_engines → TradingNode → prepare_context → factories → build → wire)/ ArbContext 包含 venue_liveness/pair_registry/debug_config/session/pm_settlement 字段,且不再包含 PM/OE health interval 死接线 / main 调用链路
- ✅ `test_arb_node.py`:NT exec config 保持启动期 `reconciliation=True`;连续 `open_check_interval_secs=300`(#111:驱动 order liveness 恢复);`position_check_interval_secs=300`(#110:驱动 PM merge/redeem + position liveness);默认 in-flight check 开启;`timeout_connection=180s`。
- ✅ `test_arb_node.py`:PolymarketSettlement launcher 接线 —— PM runtime venue disabled、cleanup 关闭或缺 PM 链上凭证时跳过;凭证齐全且 PM enabled 时构造 `PolymarketContractService` 并初始化,成功后按 `cleanup_merge_enabled` / `cleanup_claim_enabled` 注入 `PolymarketSettlement`;初始化失败则不阻塞节点启动。
- ✅ `test_arb_node.py`:venue runtime enablement —— launcher 要求 `venues.*.enabled=true` 的 runtime venue 不少于 2 个;默认 PM+OE;PM+SE 时不注册 OE;OE+SE 时不注册 PM/PMSPORTS;PM+OE+SE 时三个 venue 同时注册。SE 显式 `true` 时加入 SE data+exec config、注册 SE factories、`VenueExecutionLiveness` 可标记 SE,并把 `se_session_timeout_secs` / `se_discovery_config` 注入 ArbContext。

**不在 slice 6 范围**:
- ✅ Aliases → Provider 注入(slice 7A,#46)
- ✅ OE data factory 真接 scraper(slice 7A,#46)
- ✅ InstrumentRefresher × 2 + MarketMatchingActor + StrategyEvaluator 接线(slice 8A,#47)
- ✅ PolymarketSettlement 接线(#110:由 NT 连续 position 对账触发;#110 后不再有 `pm_positions_fetcher`)
- ✅ `is_execution_active` 真接 in-flight 检测(#48)

## Slice 8A 落地(2026-05-29 #47)

`launchers/arb_node.py:add_actors(node, cfg, pair_registry=)` — node.build 后调用,经 ArbContext 取共享 provider 实例,构造 4 个 Actor + `node.trader.add_actor`。

- ✅ `test_arb_node.py` +6:both providers → 4 actors / PM 缺 → 3 actors / 两缺 → 2 actors / StrategyEvaluator portfolio 取自 kernel / Refresher provider 引用 ctx 同一实例 / bootstrap_and_build 调 add_actors

## Slice 8A 修正(2026-05-30 #48):Q19 `is_execution_active` 真接(撤"TODO")

**用户指正**:`is_execution_active = lambda: False` 是漏看 — Q19 机制(`_cross-cutting/synchronization.md`)早就存在,`ArbExecutionSessionMixin` 维护 `_execution_active` ref-count,健康检查已用同一 callable 语义。launcher 应该桥到具体 exec client,**不是新机制**。

落地:`_make_is_execution_active(node)` 遍历 `node.kernel.exec_engine._clients`,`getattr(client, "_execution_active", False)` 兜底任意 client(无 mixin 也不 raise);任一 True → 聚合返 True;StrategyEvaluator deps 改用真 callable(撤 `lambda: False`)。

- ✅ `test_arb_node.py` +4:无 session 在飞 → False / 任一 client True → 聚合 True(切 client 状态后 callable 反映最新)/ 无 `_execution_active` 属性的 client 不 raise / `add_actors` 装的 StrategyEvaluator `_is_execution_active` 是真聚合 callable(不是 `lambda: False`)

## PM live preflight(2026-06-10 #98)

`launchers/arb_node.py --preflight-polymarket --config <cfg>` 是只读入口:加载同一 ArbConfig,用 `to_polymarket_exec_client_config(cfg).proxy_url` 调官方 geoblock endpoint,再用同一路由跑 CLOB `get_server_time()` + authenticated `get_open_orders()` + `get_balance_allowance()`,然后退出;不 build TradingNode、不登录 OE、不下单。JP 属官方 `Frontend UI restricted`,不应因 `blocked=true` 误拦 API;AU/US 等 API-blocked 仍返回 2。余额为 0 或 v2 SDK transport 失败也返回 2 并打印单行 stderr,用于提前暴露 `POLYMARKET_SIGNATURE_TYPE` / funder 配错或代理链路不可用。

- ✅ `test_arb_node.py` +5:preflight CLI 在 build 前退出 / blocked 时返回 2 且打印简洁 stderr / v2 SDK transport 失败返回 2 且不冒 traceback / preflight 使用 Exec config 的 `proxy_url` / 0 余额返回错误
