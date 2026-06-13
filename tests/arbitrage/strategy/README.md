# strategy 测试

对应章节: `refactor.md §5.4 / Q21`;详细设计 `architectures/strategy/architecture.md`(Q21 锁定 2026-05-24)

**Q21 框架锁定(2026-05-24)**:Strategy 不再是单体决策类,而是 **scope-priority + condition tree + 套利/补救并行** 框架。架构详细设计见 `architectures/strategy/architecture.md`(标准 7 节);旧 `services/strategy/` 3000 行(signals / strategies)语义可作为新框架的 **Check / Action / SignalCollector 类填充**,不再是骨架。

**测试结构**:
- 框架层(本 README §"strategy-4.framework.x"):`SignalStore` / `BoolExpr` / `Condition` 树评估 / `StrategyRegistry` / `StrategyEvaluator` —— 纯逻辑,可全单测
- 实现层(本 README §"strategy-4.{N}.x" 沿用编号):具体策略行为(Q13 全量重算 / 双腿原子 / 补偿撤单 / hook 契约 / 深度缩放 / 概率转换)—— 平移自旧实现,挂在新框架的 Check/Action 上落地

## 锁定的关键性约束(2026-05-09 修正后)

- Strategy **只管决策**(机会值不值得做、按什么参数下单、深度缩放算应该下多少)
- Strategy **不引用 Risk** —— 直接 `submit_order(...)`,NT `ExecutionEngine` 自动经过 `ArbitrageRiskEngine` 拦截
- Strategy **拥有深度缩放**(`_adjust_share_by_liquidity` hook,从 `cache.order_book(...)` 读流动性)
- 订单追踪走 NT `Strategy` 自带回调(`on_order_submitted` / `on_order_filled` / `on_order_denied` / `on_order_rejected`)
- 订单生命周期:RiskEngine 拒绝 = `on_order_denied`;venue 拒绝 = `on_order_rejected`

## Q13 新增约束(2026-05-19,关联 `refactor.md §6.8`)

- Strategy **每一轮循环都必须全量重算意图**,不能假设上一轮发的 submit 还在 execution 队列里排队
- 当 execution 入口检测到残留挂单时,**会退化为 cancel-only session 并丢弃 strategy 当次传入的 submit**(strategy 不会收到错误,只是没下成);下一轮 strategy 重新评估行情后自行重发,可能发的是另一个意图
- 这意味着 strategy 端 loop 周期不能太长,否则有"明明该下却等到下一轮才下"的延迟
- execution 内部 **recovery loop 已移除**;补救 / 撤后再下 / 单腿失败时的另一腿补偿撤单,**全部由 strategy 承担**(显式 defer 到 Step 4 设计)
- 现实风险: 短期会出现"裸单飘着没人管"的窗口(Q-F),strategy 设计时必须有兜底

## Strategy Debug 覆盖契约(Q21 + #39 修订)

**老的"单体 Strategy + protected hook + Debug 子类"设计已撤回**(refactor.md #39)。Q21 框架下,
strategy 参数(min_rebate / price / size / share_scaler 等)是具体 `Check`/`Action` 的**构造参数**,
属于 first-class config。要 debug 时,**直接配置一份 debug 版 Strategy 实例**(同 scope,
`Check`/`Action` 用极端参数),走 `StrategyRegistry.register_*` 装载:

```python
prod = Strategy(scope_key="sport:Soccer",
                arbitrage_tree=Condition(..., checktion=[RebateCheck(min_rate=0.05)],
                                          action=PMSubmitAction(...)))
dbg  = Strategy(scope_key="sport:Soccer",
                arbitrage_tree=Condition(..., checktion=[RebateCheck(min_rate=-10.0)],
                                          action=PMSubmitAction(price_override=0.01, size_scaler=0.001)))
strategy_registry.register_sport("Soccer", dbg if debug_cfg.enabled else prod)
```

无 `DebugArbitrageStrategy` 类,无 `_get_*` hook,无 `EvalContext.debug_overrides`。原因:Q21
拆出 Check/Action 后,参数已是 first-class,**hook 机制冗余**;且 hook 注入会让生产 Check/Action
带 `if ctx.debug_overrides` 分支,违反 P10。

## 预期用例(摘要)

### strategy-4.1: ArbitrageStrategy 订阅 MatchedPair → 自动调 subscribe_order_book_deltas
### strategy-4.2: 套利决策算法等价于原 orchestrator 的逻辑
### strategy-4.3: 深度缩放 `_adjust_share_by_liquidity` 从 cache.order_book 读
### strategy-4.4: 流动性太差时返回 None,Strategy 放弃机会(不发订单,不算被风控拒)
### strategy-4.5: 双腿订单原子下单
### strategy-4.6: NT 订单回调 `on_order_submitted` / `on_order_filled` / `on_order_denied` / `on_order_rejected` 全部正常触发
### strategy-4.7: 单腿成交另一腿 denied 时的补偿撤单(关联 `bug_compensating_cancel_missing`)
### strategy-4.8: hook 点契约(默认实现 + 子类可覆盖)

### strategy-4.9: 每轮全量重算,不缓存待下单(Q13)

**前置**: strategy 在循环 N 发出 submit A;execution 因残留挂单走 cancel-only session,丢弃 A
**输入**: 进入循环 N+1
**期望**:
- strategy 重新读 cache.order_book / matched_pair / leg_settled
- 重新计算意图(可能是 B,不是 A)
- 重新调 submit
**验收**: strategy 内部无"上一轮 A 在等"的状态机;每轮独立决策

### strategy-4.10: 残留挂单导致 submit 被丢弃的可观测性(Q13)

**前置**: strategy 发 submit,execution 检测到残留挂单
**输入**: cancel-only session 启动并丢弃 submit
**期望**:
- strategy 端能从 cache / event 知道"上次发的没下成"(具体机制 Step 4 设计时定: 是不触发 `on_order_submitted` / 还是通过 leg_settled / 还是新的事件)
- 不出现"strategy 以为下了实际没下"的盲区
**验收**: 设计阶段必须确定 strategy 的感知途径

### strategy-4.11: execution recovery 已移除 → strategy 端补救路径(Q13,defer)

**占位**: 单腿失败 / 裸单飘着 / 撤后再下等场景的状态机,等 Step 4 启动时展开
**关联**: `bug_compensating_cancel_missing`,Q-F 裸单窗口

### strategy-4.12: Strategy 调 `portfolio.min_way_rebate(pair_id)` 判断机会(Q14,§6.9)

**前置**: `ArbitragePortfolio` 已 swap 到 `kernel._portfolio`;某 MatchedPair 事件到达
**输入**: Strategy.on_data(matched_pair) → **先 settled pre-check**(见 4.14)→ 通过则调 `min_way_rebate(pair_id)`
**期望**:
- 拿到当前所有 pair 持仓的最低方向 rebate
- 与 `self._get_min_rebate_rate()` hook(`config.min_rebate_rate` 或 Debug override)比较
- 不满足阈值 → 放弃机会(不发订单);满足 → 继续决策流
**验收**:
- Strategy 通过 NT 标准 `self.portfolio` 接口拿数据,不引用其它组件
- way_rebate 计算是 pull-based,Strategy 调用即重算

### strategy-4.14: Strategy 调用 way_rebate 前必须做 settled pre-check(Q-G,2026-05-19)

**前置**: MatchedPair 事件到达,strategy 准备评估机会
**输入**: strategy 内部按顺序:
1. 从 cache 读 `leg_settled[pair_id]`(具体 cache 访问 API 待 Step 5 实施时定)
2. 判断:
   - entry **不存在**(从未对此 pair 发起过 execution) → **通过 pre-check**,继续调 `portfolio.min_way_rebate`
   - entry 存在 且 **所有方向 true** → **通过 pre-check**,继续
   - entry 存在 且 **任一方向 false** → **放弃机会**,不调 way_rebate,不发订单
**期望**: 满足上述三分支
**验收**:
- strategy 在 way_rebate 之前显式有 pre-check 代码路径
- pre-check 失败时:不调用 portfolio API,不调用 submit_order,early return
- 与 §6.9.3 way_rebate 内部 gate 双重防御(Portfolio 内部 gate 兜底其它调用方如 WebGateway)
**注释**: pre-check 失败的原因(submit 没到 venue / WS 死 / 页面挂)留给健康检查兜底(§6.8.3 / §6.8.4),strategy 只需 abort,不做自救

### strategy-4.13: Strategy 不缓存 way_rebate(Q14)

**前置**: 同 4.12
**输入**: 同一轮 loop 中多次调用 `portfolio.way_rebate(pair_id)`
**期望**: 每次调用都即时算,Strategy 端无中间 cache
**验收**: Strategy 代码不持有 `_way_rebate_cache` / `_last_rebate` 等字段

### strategy-4.15: 健康检查互斥 pre-check —— 健检期间放弃机会(Q19,2026-05-21)

**前置**: 任一 venue 健康检查 tick 正在跑(strategy 已收到 `health_check.started`,本地 `_health_check_active=true`)
**输入**: 一个满足阈值的套利机会到达,strategy 进入决策流
**期望**:
- submit 前 pre-check 命中 `_health_check_active` → **放弃机会,early return,不 submit**(与 settled pre-check 并列;全局粒度,不分 venue/competition)
- strategy 不发 `execution.started`
- 下一轮(健康检查 `health_check.finished` 后)重新评估行情,若机会仍在则正常 submit
**验收**:
- strategy 订阅 `health_check.*` 维护 `_health_check_active` 镜像;**OE/PM 各发各的消息,strategy 用 ref-count 并成一个全局状态**(count>0 即放弃,容许两 venue 健康检查并存 count=2,§6.10)
- pre-check 在**首个 await 前同步**判定(单 loop 无锁,§6.10)
- 反向:strategy 决定 submit 时发 `execution.started`、terminal/timeout 发 `execution.finished`(供健康检查让路)

### strategy-4.16: 执行在飞时健康检查让路(Q19,反向验证)

**前置**: strategy 已 submit、`execution.started` 已发、execution session 进行中
**输入**: 健康检查 alert 在此期间 fire
**期望**: 健康检查 tick 看到 `_execution_active` 跳过(见 pm-adapter-5.health.5 / oe-adapter 对应用例);执行不被打断,track 到 terminal/timeout
**验收**: 执行全程不被健康检查干扰;`execution.finished` 后健康检查恢复

### strategy-4.17: 机会快照隔离 —— 开跑时冻数据,全程用拷贝(Q20,2026-05-21)

**前置**: MatchedPair 事件到达,strategy 准备评估某 pair 的机会
**输入**: strategy 在评估开跑时取快照,随后规划 + submit + tracking
**期望**:
- 取 per-pair 快照(该 competition 所有腿):冻 **订单簿所需值 + 持仓 + way_rebate(取快照那刻调一次 `portfolio.way_rebate(pair)` 冻结果)**
- 规划 / `_adjust_share_by_liquidity` / 后续(含 deferred 的补偿逻辑)**全读快照**,不读 live cache
- 期间 live cache 被新成交/新 tick 更新(包括本次执行自己的腿成交),**机会计算/执行不受影响**
- 该次套利结束(双腿 terminal/timeout/放弃)→ 丢弃快照;下一轮重新取**新鲜**快照
**验收**:
- way_rebate **不从 cache 读**(cache 没有),是快照里冻结的预算结果
- **安全闸走 live 不走快照**:settled pre-check(4.14)/ Q19 健康检查互斥(4.15)/ RiskEngine 余额检查 都读最新 live 状态
- 快照不跨轮持久(与 4.9 每轮重算一致);strategy 不持有跨轮快照字段
- 实现自建(NT 无原生读隔离快照):持仓 `pickle` 深拷贝,订单簿冻取所需值;不依赖 `Cache.snapshot_position`(那是 netting 归档,非读隔离)

### strategy-4.18: 快照期间本腿成交不扰动机会计算(Q20 关键场景)

**前置**: strategy 已取快照、submit 双腿;PM 腿先成交 → live cache 持仓变化 → live way_rebate 会变
**输入**: 在该次套利仍进行中,strategy 若有 deferred 的补偿/再评估逻辑触发
**期望**: 补偿/再评估读**快照**的持仓 + way_rebate(开跑那刻的值),**不**因本腿刚成交而用变化后的 live 值
**验收**: 验证快照"全程一致"语义;live 持仓的变化由下一轮新快照才纳入

### strategy-4.19: 快照回收 —— 所有出口确定性释放,无泄漏(Q20)

**前置**: strategy 取快照机制已实现
**输入**: 分别走四条出口 —— (a) 正常双腿 terminal;(b) §6.8.5 tracking timeout;(c) 规划阶段放弃(rebate 不够/流动性差);(d) 执行中抛异常
**期望**: 四条出口**都**在 `finally` 释放快照;`pre-check 放弃`(settled/健康检查不过)路径**根本没取过快照**(取快照在 cheap live pre-check 之后)
**验收**:
- 跑 N 轮(含大量 pre-check 放弃 + 规划放弃 + 正常完成混合)后,strategy 持有的快照数**回落到 ≤1**(Q19 全局互斥 → 同时最多 1 次执行在飞 → 最多 1 份长命快照)
- 快照绑 per-opportunity 上下文,**不**进长存 `self._snapshots` 字典(静态检查无此累积字典,或有则每出口 `del`)
- 异常路径不漏快照(`finally` 覆盖)
- 内存监测:长时间运行快照占用不单调增长

## Slice 9 落地(2026-05-31 #49):mean_rebate 策略 + 框架小改

详设见 `architectures/strategy/architecture.md §3.8`(slice 9 落地段)+ `_cross-cutting/configuration.md §10`(slice 9 ✅)。

**框架改 3 件**(per-pair 隔离 + per-eval scratch):
- ✅ `test_signal_store_view.py`(6):view writes 写 namespaced / view reads 只看 namespace / per-pair 不污染 / transient get 消费仅在 namespace / has namespaced / clear_persistent / root+view 共存
- ✅ `test_snapshot.py` +3:in_play False when no leg marks / in_play True when any leg marks / 缺 cache.instrument 不 raise
- ✅ `test_evaluator.py`(5 旧 test 改用 `store.view(pair_id).set_persistent(...)`,符合 view idiom)

**用户域 Check/Action**(slice 9 #49):
- ✅ `test_check_pre_match.py`(3):not in_play → True / in_play → False / snapshot=None → True 不阻塞
- ✅ `test_check_mean_rebate.py`(4):3-way 套利 > 阈值 → True 写 legs / rate < 阈值 → False 不写 / 缺方向 → False / 2-way 也支持
- ✅ `test_check_mean_rebate_recovery.py`(6):已有单边持仓 → 生成缺口 outcome recovery leg 到最大实际 share / 修复后最差 rebate 低于阈值不触发 / 无缺口不触发 / OE 缺口 qty 按 gross payout 反算 / typed `InstrumentId` info map 兼容 / 既有持仓 `avg_px_open=0` 时不触发 recovery
- ✅ `test_action_place_bets.py`(11):PM size=share / OE size=share/odds / OE 无效价 → 0 / 无 legs scratch 不 raise / log 每 leg / recovery Check 预写 `leg["qty"]` 时复用该 qty / venue-keyed `price_overrides`+`qty_overrides` 只改 submit spec(用于 live probe 不成交挂单,且 qty override 优先于 leg qty) / compensation tree 可用 `intent="recovery"` 标记补救单

**OE inplay 写入**:
- ✅ `tests/arbitrage/adapters/orbitexch/test_data_client_inplay_writeback.py`(4):present True/False 写 info / cache 缺 instrument 不 raise / info=None 不 raise

## Slice 10e(2026-06-04 #61):OBD per-iid 订阅 + OBD-driven 重评

`StrategyEvaluator` 收 `MatchedPair` → `_ensure_obd_subscribed`:两边各腿首次见到时
`subscribe_order_book_deltas(InstrumentId)`(去重 `_obd_subscribed`)→ PM CLOB / OE WS 把真实赔率
流进 cache → `build_snapshot` 读到非空 `order_book` → mean_rebate 能算机会。订阅的 OBD 由 NT 投到
`on_order_book_deltas` → `_route_eval`(经 `instrument_id→PairRegistry→pair_id` 评估,OBD-driven 重评)。
- ✅ `test_evaluator.py` +1 `test_matched_pair_subscribes_obd_deduped`:MatchedPair → 两边各腿订 OBD,同 pair 再来去重。
- **live smoke15 验**:MatchedPair mensik-zverev → 4 个 `SubscribeOrderBook`(2 PM token + 2 OE selection)→ 两 data client `Subscribed ... order book deltas`,0 ERROR。

## Slice 10e 观测增强(2026-06-07):`log_evaluations` 评估锚点

`StrategyEvaluatorConfig.log_evaluations=True` 时,`StrategyEvaluator` 对 schedule / skip /
result / fire 分支输出低噪声日志,用于 skip=true NT-node smoke 判断 OBD 是否真的触发 strategy evaluate。
默认值仍为 `False`,不改变生产运行行为。

- ✅ `test_evaluator.py` +2:启用 `log_evaluations` 后套利优先语义不变;无策略 / execution_active skip 路径保持 no-op。

## Slice 10d(2026-05-31 #52):msgbus 直订替代 subscribe_data

`StrategyEvaluator.on_start` 改用 `self._msgbus.subscribe(topic=f"data.{MatchedPair.__name__}", handler=self.on_data)` 替代 `subscribe_data(DataType(MatchedPair))`。**原因**:NT `subscribe_data` 强制走 SubscribeData cmd 路由经 DataEngine,需 client_id/instrument_id;MatchedPair 是 Actor-to-Actor 事件无 venue/instrument 归属。同时**MVP 不预订 OrderBookDeltas**(原 `subscribe_data(DataType(OrderBookDeltas))` 同样需要 instrument_id),需 OBD-driven 重评时 MatchedPair fire 后 per-iid 调 `subscribe_order_book_deltas(iid)`。**slice 10d live smoke 验:strategy 端 0 ERROR**。

## Slice 10a 落地(2026-05-31 #50):`EvalContext.submitter` + 真出单链路

- ✅ `test_submitter.py`(5):`make_submitter` 构 LimitOrder + SubmitOrder cmd send 到 `ExecEngine.execute` / SELL 侧 / cache.instrument None → skip 不 raise / 策略 spec 的字符串 `instrument_id` 在 submitter 边界转 NT `InstrumentId` / `spec["intent"]` 写入 `Order.tags=["arb:intent=<intent>"]`
- ✅ `test_action_place_bets.py` +6:submitter 注入 → Action 调 submitter 2 次 spec 正确 + log mode "[submit]" 无 "would submit" / submitter=None → log-only fallback 不 raise / `leg["qty"]` 可由 compensation Check 写入并被 `place_bets` 复用 / `price_overrides={"ORBITEXCH": 1000}` + `qty_overrides={"ORBITEXCH": 7}` 可把 OE live probe 单变成不易成交 limit,同时不影响 Check 用真实 OBD 算机会 / 显式 `qty_overrides` 优先于 `leg["qty"]` / `intent="recovery"` 标记补救单
- ✅ `test_evaluator.py` +1:evaluator 构造 ctx 时 `submitter=self._make_submitter()` 已注入 — Action 拿到的 ctx.submitter 是 callable
- ✅ NT-node skip smoke(2026-06-08):临时强制 `mean_rebate.min_rate=-10.0` + `pre_match` 关闭后,真实 PM/OE 盘口触发 `PlaceBetsAction` → `ExecClient-ORBITEXCH: Submit LimitOrder(...)` → SkipExecution mock fill → portfolio position 更新。该 smoke 只验证安全 submit/mock-fill 链路;`skip_execution=true` 会立即全成,不会留下 open order,因此不验证真实撤单。
- ⚠️ 同次 smoke 暴露 OBD 高频下同一机会会重复 fire/重复 mock submit;需后续以 strategy 执行保护/节流单独处理,不混入 recovery 状态机。

**Slice 9.5 in-process e2e smoke**(`test_mean_rebate_e2e.py`,4 tests):
- ✅ 完整 e2e:JSON config → JSON loader → Strategy(Check/Action registry)→ `evaluate_tree` 命中(rate=0.25,3-way 套利) → `PlaceBetsAction.execute` log 3 leg(`would submit: ... qty=5.6250 price=4.0` × 3)
- ✅ 门控 smoke:snapshot.in_play=True → PreMatchCheck False → checktion AND 短路 → MeanRebateCheck 不跑 → 无 fire
- ✅ 阈值 smoke:rate=0.20 但 min_rate=0.30 → 不命中
- ✅ recovery config smoke:`compensation_tree` 引用 `mean_rebate_recovery` + `place_bets(intent="recovery")` 可经 JSON loader 构建
- **不依赖** PM enricher / NT TradingNode / Cache — 验证 framework + JSON 配置 + 3 个用户域 Check/Action 实际打通

## Slice 5 落地(2026-05-28 #44):Check/Action registry + JSON loader

`src/arbitrage/strategy/check_action_registry.py` + `src/arbitrage/strategy/json_loader.py`(框架层,具体 Check/Action 子类由用户后落)。

- ✅ `test_check_action_registry.py`(8):register + build + 默认 params / 未知 type / 缺 type / 同名同类幂等 / 同名异类 raise
- ✅ `test_json_loader.py`(27):BoolExpr 5 形态(signal/AND/OR/NOT/嵌套)+ None 默认真值 + 多 key/未知 key/类型错 raise;Condition 全空默认 pass + self_hits False 短路 + checktion 短路 + 递归 sub_conditions 互斥 + 未知 check/action raise;Strategy 两树 + 缺 compensation_tree 永 False + 缺 arbitrage_tree raise;StrategyRegistry pair/competition/sport 三层挂载锁定 + `pair_id:` 别名 + 未知 strategy_id / 错 scope kind / 错 scope 格式 raise + 空 bindings → 空 registry + `strategy.enabled=false` 时即使有 bindings 也返回空 registry(保留 OBD 订阅桥,禁用 Action)

## Debug 相关

**无 strategy 层 Debug 子类**(refactor.md #39 撤回 `DebugArbitrageStrategy` 整条)。
Strategy 的 debug 是**配置 vs 配置**(prod Strategy / dbg Strategy 同 scope,
`Check`/`Action` 用不同构造参数),不是**类 vs 类**。

其余 Debug 件(数据流 `Debug{PM,OE}DataClient` / Risk `DebugArbitrageLiveRiskEngine.skip_check_size` /
未来 `SkipExecution{PM,OE}ExecutionClient`)的测试在 `tests/arbitrage/debug/`,**不在本目录**。

---

## strategy-4.framework.x:Q21 新框架层用例(2026-05-24)

新框架的纯逻辑件,可全单测。落地顺序见 `architectures/strategy/architecture.md §7`。

### strategy-4.framework.store.{1-4}:SignalStore 双状态读写
- **.1**:`set_persistent` 后多次 `peek`/`get` 都拿到值(写后保留)
- **.2**:`set_transient` 后 `peek` 拿到值不消费;`get` 拿到值后再 `get` 返 None(用后即清)
- **.3**:`clear_persistent` 删除该 key
- **.4**:同 key 同时存在 persistent + transient 时,`get` 优先消费 transient(避免 stale)

### strategy-4.framework.expr.{1-5}:BoolExpr (AND/OR/NOT) + SignalRef 求值
- **.1**:`SignalRef("live")` 缺 → False;`set_persistent("live", True)` → True
- **.2**:`AndExpr(a, b)` 全 True 才 True;任一 False → False
- **.3**:`OrExpr(a, b)` 任一 True 即 True
- **.4**:`NotExpr(a)` 取反
- **.5**:嵌套 `AND(a, OR(b, NOT(c)))` 求值正确
- **.6**:`BoolExpr.eval` 经 `SignalStore.peek` 不消费 transient

### strategy-4.framework.cond.{1-6}:Condition 树评估(EvalResult)
- **.1**:self_hits=False → `EvalResult(hit=False, action=None)`
- **.2**:self_hits=True、有 sub_conditions、第一个 sub 命中 → 返该 sub 的 EvalResult(后续不跑)
- **.3**:self_hits=True、有 sub_conditions、全没命中 → `EvalResult(hit=False)`
- **.4**:叶子节点(sub_conditions 空)、checktion 全过、action 非 None → `EvalResult(hit=True, pending_action=action)`(不执行 action)
- **.5**:叶子、checktion 空 list → 默认通过
- **.6**:叶子、action=None → 仍 `hit=True`(`pending_action=None` 上层无事可 fire)

### strategy-4.framework.reg.{1-4}:StrategyRegistry scope 优先级 + 挂载锁定
- **.1**:只挂 sport → 找该 sport 下任意 pair 都返该策略
- **.2**:挂 sport + comp → comp 内 pair 返 comp 策略;comp 外的 pair 返 sport 策略
- **.3**:挂 sport + comp + 具体 pair → 该 pair 返 pair 策略,**即使没命中也不下放**(Q21-a 挂载存在锁定)
- **.4**:都没挂 → 返 None

### strategy-4.framework.eval.{1-5}:StrategyEvaluator 评估器
- **.1**:`on_data` 收 `OrderBookDeltas` → 查 PairRegistry 拿 pair_id → 查 StrategyRegistry 拿 strategy → 触发 `_evaluate_strategy`
- **.2**:strategy 为 None(无挂载)→ no-op,无 fire
- **.3**:Q19:`_execution_active` True 时 evaluate 跳过(让路)
- **.4**:Q21 套利优先:arb.hit=True + comp.hit=True → fire arb.action,**不** fire comp.action
- **.5**:Q21 补救兜底:arb.hit=False + comp.hit=True → fire comp.action(等 arb evaluate 完成确认未命中后才 fire)

### strategy-4.framework.eval.{15-16}:per-pair 串行闸(§6.10 §7,#84)
- **.15**(`test_same_pair_concurrent_eval_fires_once`):同 pair 两次 `on_data`(drain 前,模拟同突发并发)→ 第一次 `_route_eval` 同步 `try_enter` 成功派发评估,第二次 gate busy → **不派发**(`loop.tasks` 仅 1)→ drain 后只 fire 一次;fire 后 gate 仍 in-flight(交执行清)。
- **.16**(`test_different_pairs_not_blocked`):不同 pair 各自 `try_enter` 成功 → 各派发各 fire(per-pair 不互相阻塞)。
- gate 自身单测见 `tests/arbitrage/common/test_pair_inflight.py`(并发放弃 / 不同 pair 独立 / 未 fire 释放 / fire→执行交接持有到 session 归 0 / max-hold 自愈 / 负计数防御 / **`clear_all` 清 inflight+exec_count**)。设计 = synchronization.md §7。

### strategy-4.framework.eval.{17-19}:健康检查互斥 + 兜底 clear_all(§6.10 §7.6,#88)
- **.17**(`test_health_check_active_skips_fire`):`_on_health_check_started` 置 `_hc_running` 后 `on_data` → `_route_eval` 见 `_hc_running` 非空 → **不派发评估**(`loop.tasks` 空,arb_action 0 次)。补 §6.10 缺失的 strategy⊥健康检查互斥。
- **.18**(`test_health_check_finished_clears_gate_when_all_settled`):gate 有泄漏残留(`LEAKED` in-flight + 脏 exec_count)。OE/PM 两健检都 started;OE `finished` 时 PM 仍在跑 → **不清**;PM 也 `finished`(`_hc_running` 空)+ `leg_settled` 全 true → `clear_all` → 残留清掉。
- **.19**(`test_health_check_finished_keeps_gate_when_leg_unsettled`):`leg.arm("P","leg-1")` 造未结腿;health_check `finished` 时 `has_any_unsettled()` 真 → **不 clear**(保护真在飞 arb)。

### strategy-4.framework.snap.{1-3}:OpportunitySnapshot(Q20)
- **.1**:evaluate 开跑时取一次 snapshot,整轮 condition 树评估都用同一份
- **.2**:期间 cache 被新事件更新,evaluate 内读到的还是 snapshot 旧值(隔离)
- **.3**:evaluate 结束 + fire 结束后,snapshot 可被 GC(绑 per-evaluation 上下文,无长存 dict)
