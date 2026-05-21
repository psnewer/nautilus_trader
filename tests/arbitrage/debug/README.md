# debug 测试(占位)

待 Q11 各子项实施时填实。

对应章节: `refactor.md §6.6`(Debug 注入框架完整设计)

## 锁定的关键性约束(P10)

**所有 debug 行为变化通过子类化 + 工厂层选择实现,生产代码零 `if self._debug` 分支**。

子类对应表:
| 类别 | 生产类(干净) | Debug 子类 |
|---|---|---|
| 数据流替换 | `PolymarketDataClient` / `OrbitExchDataClient` | `DebugPolymarketDataClient` / `DebugOrbitExchDataClient` |
| 客户端选择 | `PolymarketExecutionClient` / `OrbitExchExecutionClient` | `MockPolymarketExecutionClient` / `SkipExecutionPolymarketClient` 等 |
| Strategy 参数 | `ArbitrageStrategy` | `DebugArbitrageStrategy` |
| Risk 校验 | `ArbitrageRiskEngine`(NT RiskEngine 子类) | `DebugArbitrageRiskEngine`(skip_check_size 覆盖) |
| 余额 mock | (生产: `ExecutionClient` 写 Cache) | mock 余额值时通过 `mock_data: positions` 类别 + ExecutionClient 子类拦截 `generate_account_state` 注入 mock(无独立 BalanceMonitor) |

## 预期用例(摘要)

### Q11.1 - 配置加载
- debug-1.1: `DebugConfig.load("debug_config.json")` 解析 enabled / overrides / mock_data 三层
- debug-1.2: 配置文件不存在时返回 None(生产路径不受影响)
- debug-1.3: 不实现热重载;改 config 后必须重启进程才生效

### Q11.x - 数据流替换
- debug-A.1: `DebugPolymarketDataClient` 拦截 `_handle_data`,按 `mock_odds_*` 替换 OrderBookDelta
- debug-A.2: `mock_data.conditions` 按 `instrument.info` 字段匹配,符合的替换不符的透传
- debug-A.3: 多个 mock 同时启用,按规则依次匹配

### Q11.x - 客户端选择
- debug-B.1: `use_mock_exchange=true` 时 factory 返回 `MockPolymarketExecutionClient`
- debug-B.2: `skip_execution=true` 时 factory 返回 `SkipExecutionPolymarketClient`,`_submit_order` 直接 generate_order_filled mock
- debug-B.3: 两者都未启用时 factory 返回原始上游 client

### Q11.x - Strategy 参数(C 类)
- debug-C.1: `DebugArbitrageStrategy._get_min_rebate_rate` 在 override 启用时返回 override 值
- debug-C.2: `_get_pm_price` / `_get_oe_price` / `_get_pm_size` / `_get_oe_size` 同理
- debug-C.3: override 未启用时透传父类默认实现(super().<hook>())

### Q11.2 - Risk 跳过(D 类)
- debug-D.1: `DebugArbitrageRiskEngine._check_order` 在 `skip_check_size=true` 时跳过 NT 父类的最小限额检查(让小单通过用于测试链路)
- debug-D.2: `skip_check_size=false` 时透传父类(包括 NT 自动检查的 `instrument.min_quantity`)

### Q11.4 - Mock timeline 引擎
- debug-T.1: `MockPolymarketExecutionClient` 通过 `Clock.set_timer` 触发订单状态流转(live → partially_filled → filled)
- debug-T.2: 拒绝场景(timeline 配为 reject)→ generate_order_rejected 触发
- debug-T.3: 撤单场景

### Q11.5 - DebugConfig 不进 Cache
- debug-Z.1: 验证 DebugConfig 通过工厂 DI 传递,Cache 中无相关 entry(YAGNI)

## 文件迁移

`tests/arbitrage/_helpers/debug_configs/debug_config_size_test.json` 是从 `services/integration/` 平移来的,Q11 实施时作为 fixture 用。
