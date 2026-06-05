# launchers 测试

对应章节: `refactor.md #45`(slice 6);详细设计 `architectures/_cross-cutting/configuration.md §5.5`。

## Slice 6 落地(2026-05-28 #45)

`launchers/arb_node.py` —— NT 节点 launcher 骨架(无 Actors,留 slice 8 完善)。

- ✅ `test_arb_node.py`:`build_trading_node_config` PM+OE × data+exec 4 client config 装配 / `prepare_runtime_state` 返 3 件(LegSettledRegistry / PairRegistry / DebugConfig 可 None)/ `register_factories` 调 4 次 add_*_factory / `bootstrap_and_build` 调用顺序(install_engines → TradingNode → prepare_context → factories → build → wire)/ ArbContext 包含 leg_settled/pair_registry/debug_config/session/health 字段 / main 调用链路

**不在 slice 6 范围**:
- ✅ Aliases → Provider 注入(slice 7A,#46)
- ✅ OE data factory 真接 scraper(slice 7A,#46)
- ✅ InstrumentRefresher × 2 + MarketMatchingActor + StrategyEvaluator 接线(slice 8A,#47)
- ⬜ PolymarketSettlement / positions_fetcher 接线(slice 8B 推迟,`ctx.pm_settlement=None`,`pm_positions_fetcher=None`)
- ⬜ `is_execution_active` 真接 in-flight 检测(当前 `lambda: False`,slice 8B/9)

## Slice 8A 落地(2026-05-29 #47)

`launchers/arb_node.py:add_actors(node, cfg, pair_registry=)` — node.build 后调用,经 ArbContext 取共享 provider 实例,构造 4 个 Actor + `node.trader.add_actor`。

- ✅ `test_arb_node.py` +6:both providers → 4 actors / PM 缺 → 3 actors / 两缺 → 2 actors / StrategyEvaluator portfolio 取自 kernel / Refresher provider 引用 ctx 同一实例 / bootstrap_and_build 调 add_actors

## Slice 8A 修正(2026-05-30 #48):Q19 `is_execution_active` 真接(撤"TODO")

**用户指正**:`is_execution_active = lambda: False` 是漏看 — Q19 机制(`_cross-cutting/synchronization.md`)早就存在,`ArbExecutionSessionMixin` 维护 `_execution_active` ref-count,健康检查已用同一 callable 语义。launcher 应该桥到具体 exec client,**不是新机制**。

落地:`_make_is_execution_active(node)` 遍历 `node.kernel.exec_engine._clients`,`getattr(client, "_execution_active", False)` 兜底任意 client(无 mixin 也不 raise);任一 True → 聚合返 True;StrategyEvaluator deps 改用真 callable(撤 `lambda: False`)。

- ✅ `test_arb_node.py` +4:无 session 在飞 → False / 任一 client True → 聚合 True(切 client 状态后 callable 反映最新)/ 无 `_execution_active` 属性的 client 不 raise / `add_actors` 装的 StrategyEvaluator `_is_execution_active` 是真聚合 callable(不是 `lambda: False`)
