# debug 测试

对应章节: `refactor.md §6.6 / #38`;详细设计 `architectures/_cross-cutting/debug-injection.md`

**Q11 框架基础落地(2026-05-24 #38)**:`DebugConfig` + `DebugArbitrageLiveRiskEngine.skip_check_size` + bootstrap 接线已落,**17 passed**。
- ✅ `test_debug_config.py`(7:default disabled / 双闸 enabled / get_override default fallback / mock 按 category+conditions / mock priority / JSON roundtrip / disabled blocks all)
- ✅ `test_debug_risk_engine.py`(5:skip 未激活走 super / skip 激活只跑应用层 / 余额拒短路 gates 不跑 / gates 拒 / debug.enabled=False 等同生产)
- ✅ `test_bootstrap_integration.py`(5:无 debug 装生产 / disabled 装生产 / enabled 装 Debug 子类 / kernel-injected 包装类闭包绑 cfg / ArbContext.debug_config 字段)

**Q11.A DebugDataClient 落地(2026-05-26 #39)**:行情数据掉包 framework + factory 分支已落,**+10 passed**(累计 **27 passed**)。
- ✅ `test_debug_data_clients.py`(5:默认 passthrough / 子类覆盖 hook 替换 / hook 返 None 退化 passthrough / 子类经 self._debug 读 mock_data 决定替换 / debug_config 访问器)
- ✅ `test_debug_data_factories.py`(5:PM 无 debug / PM disabled / PM enabled 装 Debug 子类 + 传 debug=cfg / OE 无 debug / OE enabled 装 Debug 子类 + 传 debug=cfg)

**Q11.3 SkipExecutionClient 落地(2026-05-26 #40)**:跳真执行 + mock 全成交已落,**+12 passed**(累计 **39 passed**)。
- ✅ `test_debug_execution_clients.py`(8:`_mock_fill` Accepted+Filled 顺序 / PM USDC commission=0 / OE GBP / market 单 0.5 兜底 / limit 用 order.price / `_submit_order` skip 未激活走 super / skip 激活短路 mock fill / debug.enabled=False 走 super)
- ✅ `test_debug_exec_factories.py`(4:PM 无 debug 装 prod / PM enabled 装 Skip + 传 debug=cfg / OE 无 debug / OE enabled 装 Skip + 传 debug=cfg)
- 顺补 latent bug:`arb_factories.py` 漏 import `get_polymarket_instrument_provider`(live 运行会 NameError;Step 6 PM exec 未真接 live 没暴露)

**落地决策修正**(2026-05-26 #39):
- ❌ **撤回** `DebugArbitrageStrategy` 整条 —— Q21 框架下 strategy 参数(min_rebate / price / size)是具体 `Check`/`Action` 的构造参数,**直接配置 debug 版 Strategy 实例**即可,**不需要任何 Strategy 层 Debug 子类**(候选 a/b 都取消)。Q21 拆出 Check/Action 后,参数已经 first-class。
- ❌ "下单价格掉包"**不放 execution** —— execution 一直规划为透明传递层,改 order content 违反语义;由 Strategy 层 Action 参数化(如 `PMSubmitAction(price_override=0.01)`)处理。

**仍待**(后续 slice):
- ⬜ `timeline.py`(Q11.4 NT Clock 状态机;只在 SkipExecution 真要 mock 订单 lifecycle 时才需要 —— 当前"立即全成"足够链路测)

## #66:skip_execution 语义统一 =「真连接 + mock 订单 IO」(取代 #51 的 OE `_connect` no-op)

#51 曾给 `SkipExecutionOrbitExchClient` 加 `_connect/_disconnect` no-op——纯属当时 OE `_connect` 还是 `NotImplementedError` 的权宜之计,且与 PM(skip 下 `_connect` 照常真跑、只 mock `_submit_order`)**不一致**。Gap C `_connect`(#63)落地后,**已删除该 no-op**:skip 下 OE 也**真连接**(登录/page/general WS/初始账户状态),与 PM 对齐,只 mock 订单 IO(`_submit_order` + `_cancel_*` no-op,因 mock 单已终态全成、且不可拿 MOCK id 真撤)。
- **收益**:`skip_execution=true` 本身即「安全验连接路径(登录/WS/账户状态/余额帧/CURRENT_BETS 读侧)而不下真单」的 smoke;曾评估的 `dry_run_execution` 旗标因此**撤回**(多余)。
- **代价**:skip 下 OE 会真登录账户(与 PM 真连 CLOB 一致)。设计见 `architectures/_cross-cutting/debug-injection.md` #66。
- 验收:`skip_execution=true` 跑 `launchers/arb_node.py` → OE/PM Exec 均 Connected、有真账户余额、无真单。

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
