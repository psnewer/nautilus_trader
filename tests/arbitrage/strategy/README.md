# strategy 测试

对应章节: `refactor.md §5.4 / Q21`;详细设计 `architectures/strategy/architecture.md`(Q21 锁定 2026-05-24)

**Q21 框架锁定(2026-05-24)**:Strategy 不再是单体决策类,而是 **scope-priority + condition tree + 套利/补救并行** 框架。架构详细设计见 `architectures/strategy/architecture.md`(标准 7 节)；所需策略语义已落到当前 Check / Action，迁移前 services 实现已删除。

**测试结构**:
- 框架层(本 README §"strategy-4.framework.x"):`StateQuery` / `BoolExpr` / `Condition` 树评估 / `StrategyRegistry` / `StrategyEvaluator` —— 纯逻辑,可全单测
- 实现层(本 README §"strategy-4.{N}.x" 沿用编号):具体策略行为(Q13 全量重算 / 双腿原子 / 补偿撤单 / hook 契约 / 深度缩放 / 概率转换)—— 已挂在新框架的 Check/Action 上落地

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
                arbitrage_tree=Condition(..., checktion=AndCheckExpr(RebateCheck(min_rate=0.05)),
                                          action=PMSubmitAction(...)))
dbg  = Strategy(scope_key="sport:Soccer",
                arbitrage_tree=Condition(..., checktion=AndCheckExpr(RebateCheck(min_rate=-10.0)),
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
- strategy 重新读 cache.order_book / matched_pair(#108:不再读 `leg_settled`;执行健康由 Risk liveness gate 统一管,strategy 不读)
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

**延后**: 单腿失败 / 裸单飘着 / 撤后再下等专门 recovery 状态机仍待专项设计;当前主流程靠 residual cancel-only、opportunity barrier、下一轮全量重算和 `mean_rebate_recovery` 覆盖已落地路径。
**关联**: `bug_compensating_cancel_missing`,Q-F 裸单窗口

### strategy-4.12: Strategy 不再调 Portfolio way_rebate 系列接口(2026-06-22)

**前置**: `ArbitragePortfolio` 已 swap 到 `kernel._portfolio`;某 MatchedPair/OBD 事件到达
**输入**: Strategy 评估当前 `mean_rebate`/`mean_rebate_recovery` 树
**期望**:
- `mean_rebate` 主策略只从 live Cache 订单簿计算机会 rate,不读 `portfolio.way_rebate/min_way_rebate`
- `mean_rebate_recovery` 从 live Cache 持仓 + instrument_info + 订单簿计算补齐后的 outcome return rate,不读 Portfolio way_rebate
**验收**:
- `src/arbitrage/strategy/**` 无 `portfolio.way_rebate` / `portfolio.min_way_rebate` 调用
- Strategy 不再构建 `OpportunitySnapshot`,也不维护 strategy 级 `way_rebate`

### strategy-4.14: Strategy 调用 way_rebate 前的 settled pre-check(已失效,2026-06-15)

> ⚠️ **失效**:`leg_settled` 已由 `VenueExecutionLiveness` 取代。Strategy 不再做 settled/liveness pre-check;执行健康由 Risk 统一门控。旧用例保留为历史,不作为现状验收。

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

### strategy-4.14b: Strategy 不读取 VenueExecutionLiveness(2026-06-15)

**前置**: 某 venue 的 `venue_order_alive=false` 或 `venue_position_alive=false`;MatchedPair/OBD 仍触发 Strategy 评估。
**输入**: Strategy 计算机会并生成下单 specs。
**期望**:
- Strategy 不查询 `VenueExecutionLiveness`、不查询 `leg_settled`。
- Strategy 若机会满足自身条件,仍生成带 `arb:opportunity_id` / `arb:pair_id` / `arb:leg_key` / `arb:expected_legs` 的 SubmitOrder。
- 是否拦截由 Risk 的 required venues liveness gate 决定。
**验收**:
- Strategy deps 中无 `leg_settled` / `venue_liveness` 注入。
- 测试可用 fake Risk 接收路径验证 submitter 仍写完整 opportunity metadata。

### strategy-4.13: Strategy 不缓存 Portfolio 持仓收益指标(Q14,2026-06-22 修订)

**前置**: 同 4.12
**输入**: 同一轮 loop 中多次 evaluate
**期望**: Strategy 不缓存 Portfolio 派生收益指标;机会值来自当前 live Cache 订单簿/持仓计算
**验收**: Strategy 代码不持有 `_way_rebate_cache` / `_last_rebate` 等字段,也不从 Portfolio 拉 way_rebate

### ~~strategy-4.15: 健康检查互斥 pre-check~~ —— 已退役(#108,2026-06-16)

> ⚠️ **失效(#108)**:strategy⊥健康检查互斥退役。strategy 不再订 `health_check.*`、无 `_hc_running`、无 pre-check。
> 原因:执行页 reload(撞下单的真正理由)已迁 NT reconciliation;剩余 competition 页 reload 在另一张页、OE 下单
> `page.evaluate` 与焦点无关,不冲突。详见 synchronization §8.6 / refactor #108。以下为迁移前记录。

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

### strategy-4.17: live state + pair open-order baseline(#266)

**前置**:MatchedPair 已注册可交易 instruments，Strategy 准备评估。
**输入**:Evaluator 开始一次 evaluation。
**期望**:
- 只记录当前 pair open orders 的不可变 digest；
- Check/Action 对 order book、instrument info、position、constraints 均在使用点读 live Cache；
- `PlaceBetsAction` 把同一 digest 写入每条真实腿 spec。
**验收**:`tests/arbitrage/common/test_open_orders.py` 覆盖摘要稳定性与字段/集合变化；
`test_evaluator.py::test_submitter_wired_into_eval_context` 覆盖 evaluator 注入；
`test_action_place_bets.py::test_action_calls_submitter_when_present` 覆盖所有腿透传。

## Slice 9 落地(2026-05-31 #49):mean_rebate 策略 + 框架小改

详设见 `architectures/strategy/architecture.md §3.8`(slice 9 落地段)+ `_cross-cutting/configuration.md §10`(slice 9 ✅)。

**当前框架边界**:
- ✅ `test_bool_expr.py` / `test_json_loader.py`:self_hits 由无状态 `StateQuery` 与 AND/OR/NOT 组成，直接读取 `EvalContext`
- ✅ `test_evaluator.py`:Evaluator 注入 live cache、PMS `sports_store` 与 position digest（#317:open_orders_digest 已删）
- ✅ `test_eval_context_strategy_defaults_read_arbitrage_params`:每轮从 live `ArbitrageParams` 读取 `share/max_leg_share`；`fx` 不进入 Strategy defaults

**用户域 Check/Action**(slice 9 #49):
- ✅ `test_check_mean_rebate.py`:3-way 套利 > 阈值 → True 写带 `share_if_wins` 的 legs / rate < 阈值 → False 不写 / 缺方向 → False / 2-way 也支持 / 从 NT `InstrumentId.venue` 或兼容字符串提真实 venue / SE 作为 registry decimal odds venue 可触发 / 同概率 tie-break 经 Venue Registry `venue_preference_rank` 稳定排序 / strategy params.share 覆盖 Web 默认 share
- ✅ `test_check_one_side_rebate.py`:binary pair 的 `[yes,no]` 多 venue同 outcome 全部参与笛卡尔积枚举 + target 阈值过滤；缺 live state、缺 claim、缺 order book、非正价格、非正 share 均 fail-fast
- ✅ `test_check_neg_rebate.py` + `scenarios/one_side_rebate`:one_side_rebate 生成 candidates 后读取当前 Portfolio outcome 净利润/share，以最大 outcome share 为共同分母；按 candidate 的 `target_role` 只保留当前 rebate `<= max_rate` 的方向。覆盖默认阈值 0、等号边界、单方向筛选、全部淘汰后的 scratch 回滚、空仓按 0、非法 target、outcome 不完整、缺 Portfolio/candidates 与经济投影异常
- ✅ `test_check_cross_venue.py`:套利树 checktion 过滤全同 venue 的 `legs`;对 `candidates` 数组删除全同 venue candidate,剩余为空则拒绝;补偿树不使用该 check
- ✅ `test_check_mean_rebate_recovery.py`:已有单边持仓 → 生成缺口 outcome recovery leg 到最大实际 share / 修复后最差 rebate 低于阈值不触发 / 无缺口不触发 / OE/SE 缺口 qty 与实际 share 经 Venue Registry 按 USD stake gross payout 反算(`missing/odds`,不乘 fx) / 同概率 tie-break 经 Venue Registry `venue_preference_rank` / typed `InstrumentId` info map 兼容 / 既有持仓 `avg_px_open=0` 时不触发 recovery / `venue_select=True` 时即便 OE 赔率更优也只选 PM 补救腿、缺口 outcome 无 PM 报价则 fail-closed 不补 / **#321 费率分母 = 配置的意向 share**(判别性:同一失衡仓位 `share=1`→触发补救、`share=20`→前置门判已达标不补,证明分母取配置 share 非 max 在场 share;补单目标位仍 max 在场 share=10)/ 配置 share 缺失或 ≤0 时 fail-closed 不补
- ✅ `test_action_place_bets.py`:基础 size/override/spread/fail-closed 行为；PM 互斥仓位和 constraints 从 live Cache 读取；Strategy 始终保留计划价，`market=true` 只写订单 metadata，市价转换留给 Execution adapter 的最终提交边界
- ✅ `test_action_share_limit.py`:单一 `legs` 在 share_limit 内直接缩放 USD 口径 `qty/share_if_wins` / remaining 与 qty 公式按 Venue Registry `odds_model` 分支 / probability venue 用真实 venue查 Portfolio share / candidate 数组逐个缩放并输出 `adjusted_share` / 无 remaining 或缺 `qty/share_if_wins` 的 candidate 被移除 / 单一 legs 缺 `qty/share_if_wins` 时清空 / 未配 max_leg_share 时使用 Web 默认 / strategy params.max_leg_share 覆盖 Web 默认 / 不再用 action share 兜底
- ✅ `test_action_venue_replace.py`:`legs/candidates/selected_candidate`(candidate 即包了元数据的 legs 数组,三种输入都支持)中的非 PM 腿按同 outcome 替换为 PM 路由腿;逐腿 `share_if_wins` 不变,**定价由 `pm_price` 决定**(#330):`test_default_uses_pm_live_price` 默认/不设 → 用 PM 实时 ask(0.55、cost=share×PM 价);`test_pm_price_false_keeps_original_order_prob` `pm_price=False` → 保留原 order prob(0.50、cost=share×原 prob);`test_invalid_pm_price_param_raises` 非法值 ValueError。PM `qty=share` 不随价变,合成 decimal NO 执行字段不残留;已有 PM 腿不变,缺 PM 对应报价时 fail-closed,撤单计划不改写;`venue_replace -> share_limit` 时额度查询落到 PM venue
- ✅ `fx` 边界收口:Strategy Check/Action params 不再接收无效 `fx`;`fx` 只保留在顶层 `ArbitrageParams` 和 adapter 入站/出站换汇边界。
- ✅ `test_action_candi_select.py`:只在本树 candidate 中做最小下注门控和 max-share 选择；覆盖 `min_quantity/min_notional/min_buy_notional`、整 candidate 淘汰及 legs-only 包装，不承担树间优先级
- ✅ `test_action_dash_gate.py`:`candi_select` 后按对应 `start_price` 的 50% 严格过滤 BUY 腿；覆盖低于删除、等于保留、SELL 保留、`claim/role` outcome 映射、默认 0.6 开赛价、缺概率/outcome/state 不误删，以及 candidate 元数据与两份 legs 视图同步
- ✅ `test_action_place_bets.py` +2:提交 intent 优先读本树 `selected_candidate["intent"]`；
  无标记时回退 Action 配置值
- ✅ `test_evaluator.py`(#301):arb/comp 两条 Action 链分别生成 plan；两者都有 plan 时只分发补偿计划且补偿不继承套利 spread；补偿无 plan 时回退套利计划

**OE inplay 写入**:
- ✅ `tests/arbitrage/adapters/orbitexch/test_data_client_inplay_writeback.py`(4):present True/False 写 info / cache 缺 instrument 不 raise / info=None 不 raise

## Slice 10e(2026-06-04 #61):OBD per-iid 订阅 + OBD-driven 重评

`StrategyEvaluator` 收 `MatchedPair` → `_ensure_obd_subscribed`:按 `tradable_instrument_ids`
订各可交易腿,首次见到时
`subscribe_order_book_deltas(InstrumentId, managed=not event.order_books_managed)`(去重
`_obd_subscribed`)→ PM CLOB / OE WS 把真实赔率
流进 cache → Check 从 live Cache 读到非空 `order_book` → mean_rebate 能算机会。订阅的 OBD 由 NT 投到
`on_order_book_deltas` → `_route_eval`(经 `instrument_id→PairRegistry→pair_id` 评估,OBD-driven 重评)。
- 概率校验已建立 managed books 时，Strategy 只注册自己的 handler，不重建 DataEngine OrderBook；
  Matching 在同步 publish 全组后才统一退订，完整交接契约见 matching architecture §4.2.1。
- ✅ `test_evaluator.py` `test_matched_pair_subscribes_obd_deduped`:MatchedPair → 两边各腿以
  `managed=False` 订 OBD，同 pair 再来去重。
- ✅ `test_evaluator.py` `test_matched_pair_without_managed_books_creates_strategy_books`:关闭概率校验时
  事件不携带 managed feed，Strategy 使用 `managed=True` 建 book。
- ✅ `test_evaluator.py` +1 `test_matched_pair_obd_subscription_uses_tradable_ids_not_anchor_ids`:PMSPORTS anchor id 不触发 OBD 订阅。
- ✅ 旧 PM/OE projection 字段已从 `MatchedPair` schema 删除;Strategy 当前只消费 `tradable_instrument_ids`,不再有 projection fallback 分支需要覆盖。

**OBD 触发(2026-07-30 #297):所有 tradable venue 均可驱动评估**(设计见 strategy architecture §2 要点)。PM/OE/SE 的订阅与 book 更新路径不变，OBD 到达后统一按 instrument 路由到 pair。
- ✅ `test_evaluator.py` `test_obd_from_decimal_venue_triggers_eval`:OE/SE(decimal)的 OBD → 评估 fire 1 次。
- ✅ `test_evaluator.py` `test_obd_from_probability_venue_triggers_eval`:PM(probability)的 OBD → 评估 fire 1 次。

**PMSPORTS event anchor 部分落地(#127/#129)**:当 MatchedPair 包含 `.PMSPORTS` anchor ids 时,
`_ensure_obd_subscribed` 已只消费 `tradable_instrument_ids`,跳过 `.PMSPORTS`。Strategy live
state 读取经 `PairRegistry.instrument_ids_for_pair()` 默认只取可交易腿。
`PlaceBetsAction` 已有 fail-closed 兜底:若上游误传 `tradable=false` / `anchor=true` leg,不提交任何 spec。仍需端到端 smoke 验证 `.PMSPORTS` 不进入正常 `ctx.scratch["legs"]`。详细设计见
`docs/arbitrage/architectures/_cross-cutting/sports-event-anchor.md`。

- ✅ `test_matched_pair_obd_subscription_uses_tradable_ids_not_anchor_ids`:anchor id 不触发 `subscribe_order_book_deltas`。
- ✅ `PairRegistry.instrument_ids_for_pair()` 的既有测试保证默认只返回 tradable ids。
- ✅ `test_action_aborts_when_leg_is_non_tradable_anchor`:若上游误传 `tradable=false` / `anchor=true` leg,action 不提交。
- **live smoke15 验**:MatchedPair mensik-zverev → 4 个 `SubscribeOrderBook`(2 PM token + 2 OE selection)→ 两 data client `Subscribed ... order book deltas`,0 ERROR。

## Slice 10e 观测增强(2026-06-07):`log_evaluations` 评估锚点

`StrategyEvaluatorConfig.log_evaluations=True` 时,`StrategyEvaluator` 对 schedule / skip /
result / fire 分支输出 INFO 级低噪声日志,用于 skip=true NT-node smoke 判断 OBD 是否真的触发 strategy evaluate。
默认值仍为 `False`,不改变生产运行行为。

- ✅ `test_evaluator.py` +2:启用 `log_evaluations` 后评估/选择语义不变;无策略 /
  execution_active skip 路径保持 no-op。
- ✅ `test_evaluator.py::test_running_loop_task_dispatch_uses_current_loop`:已注册 NT executor 且回调处于同一
  running loop 内时,`StrategyEvaluator` 必须把 evaluate/action task 派发到 `register_executor` 注入的 loop;
  注入 fake loop 只作为未注册 executor 的单测 fallback。验收:触发 `MatchedPair` 后 fake loop 无挂起 task,
  action 在注册 loop 执行 1 次。
- ✅ `test_evaluator.py::test_registered_executor_loop_used_without_running_loop`:NT msgbus 同步回调无 running loop
  时,`StrategyEvaluator` 必须通过注册 loop 的 `call_soon_threadsafe` 投递 task,不能落到 launcher/deps fake
  loop。验收:触发 `MatchedPair` 后 fake loop 无挂起 task,action 在注册 loop 执行 1 次。

## Slice 10d(2026-05-31 #52):msgbus 直订替代 subscribe_data

`StrategyEvaluator.on_start` 改用 `self._msgbus.subscribe(topic=f"data.{MatchedPair.__name__}", handler=self.on_data)` 替代 `subscribe_data(DataType(MatchedPair))`。**原因**:NT `subscribe_data` 强制走 SubscribeData cmd 路由经 DataEngine,需 client_id/instrument_id;MatchedPair 是组件间事件，无 venue/instrument 归属。同时**MVP 不预订 OrderBookDeltas**(原 `subscribe_data(DataType(OrderBookDeltas))` 同样需要 instrument_id),需 OBD-driven 重评时 MatchedPair fire 后 per-iid 调 `subscribe_order_book_deltas(iid)`。**slice 10d live smoke 验:strategy 端 0 ERROR**。

## Slice 10a 落地(2026-05-31 #50):`EvalContext.submitter` + 真出单链路

- ✅ `test_submitter.py`(6):`make_submitter` 经 NT `OrderFactory` 构 LimitOrder 并委托
  `Strategy.submit_order` / SELL 侧 / cache.instrument None → skip 不 raise / 策略 spec 的字符串
  `instrument_id` 在 submitter 边界转 NT `InstrumentId` / `spec["intent"]` 与 opportunity metadata 写入 tags。
- ✅ `test_action_place_bets.py`:Action 只生成 execution plan，不直接调用 submitter/canceler；统一 dispatcher 并发提交 specs。另覆盖 leg qty、price/qty override、spread、intent 与 log-only fallback
- ✅ `test_evaluator.py`:evaluator 构造 ctx 时注入 callable submitter；
  `test_submitter_uses_native_strategy_submit_order_path` 锁定原生路径先把 Order 写入 NT Cache，再向
  `RiskEngine.execute` 路由 `SubmitOrder`，且 strategy ID 为 `ARB-EVAL-001`。
- ✅ **#105(2026-06-13)多腿并发提交**:并发 `gather` 已迁到统一
  `dispatch_execution_plan`；同页操作仍由 OE/SE ExecClient 页锁串行兜底，跨 venue 腿并发。
- ✅ NT-node skip smoke(2026-06-08):临时强制 `mean_rebate.min_rate=-10.0` 后,真实 PM/OE 盘口触发 `PlaceBetsAction` → `ExecClient-ORBITEXCH: Submit LimitOrder(...)` → SkipExecution mock fill → portfolio position 更新。该 smoke 只验证安全 submit/mock-fill 链路;`skip_execution=true` 会立即全成,不会留下 open order,因此不验证真实撤单。
- ⚠️ 同次 smoke 暴露 OBD 高频下同一机会会重复 fire/重复 mock submit;需后续以 strategy 执行保护/节流单独处理,不混入 recovery 状态机。

## Opportunity barrier strategy 用例(已落地代码,待 live 验证,2026-06-14)

对应设计:`architectures/_cross-cutting/synchronization.md §8.4bis` + strategy §3.9。

### strategy-4.23: submitter 入口走 RiskEngine
- 前置:已注册 `StrategyEvaluator`;cache 有 instrument;注册测试 RiskEngine endpoint。
- 输入: 调 `submit(spec)`。
- 步骤:检查 NT Cache 与捕获的 `SubmitOrder`。
- 期望:订单先写入 Cache，再由 NT `Strategy.submit_order` 路由 `RiskEngine.execute`，不是直接进 ExecEngine。
- 期望:新订单仍为 `INITIALIZED`，不出现在 `cache.orders_open(instrument_id)`，不污染 barrier baseline。
- 验收: Strategy 下单路径不绕过 `ArbitrageLiveRiskEngine`。
- 状态:✅ `test_evaluator.py::test_submitter_uses_native_strategy_submit_order_path`

### strategy-4.24: 同一次 PlaceBetsAction 给所有真实腿写同一 opportunity metadata
- 前置: `ctx.pair_id="P1"`,scratch 中有 PM/OE 两条真实 legs,submitter 记录 spec。
- 输入: 执行 `PlaceBetsAction.execute(ctx)`。
- 步骤: 检查两次 submit spec。
- 期望: 两条 spec 拥有相同 `opportunity_id` / `pair_id=P1`;各自 `leg_key` 不同;`expected_legs` 包含两条真实腿且包含自己。
- 期望:两条 spec 同时携带本轮相同的 `positions_digest`（#317:open_orders_digest 已删）。
- 验收: 不发送 0 qty 空单;没有真实下单的 outcome 不进入 `expected_legs`。
- 状态:✅ `test_action_place_bets.py::test_action_calls_submitter_when_present`

### strategy-4.25: submitter 将 opportunity metadata 写入 Order.tags
- 前置: spec 带 `opportunity_id/pair_id/leg_key/expected_legs/intent`。
- 输入: 调 `submit(spec)`。
- 步骤: 捕获生成的 `SubmitOrder.order.tags`。
- 期望: tags 包含 `arb:opportunity_id` / `arb:pair_id` / `arb:leg_key` / `arb:expected_legs` / `arb:intent`。
- 验收: Risk deny 时只拿到 order 也能发布结构化 opportunity deny 消息。
- 状态:✅ `test_submitter.py::test_submit_writes_opportunity_metadata_tags`

### strategy-4.31: PlaceBetsAction 可禁用 ACK 后的 timeout tracking
- 前置:`PlaceBetsAction(enable_timeout=false)` 生成多腿 submit spec。
- 期望:每条真实腿都携带 `enable_timeout=false`；缺省 Action 不增加该 spec/tag 字段；
  非 boolean 配置 fail-fast。
- 期望:submitter 将假值编码为 `arb:enable_timeout=false`，metadata 解码后保持假值。
- 期望:该字段只属于 submit metadata；cancel plan/metadata 不携带也不读取它。执行语义见 execution README
  `execution-4.2.5`。
- 验收:✅ `test_action_place_bets.py::test_action_disabled_timeout_is_written_to_every_submit_spec` /
  `test_action_rejects_non_boolean_enable_timeout`，
  `test_submitter.py::test_submit_writes_opportunity_metadata_tags`，
  `tests/arbitrage/common/test_opportunity.py::test_opportunity_meta_round_trips_enable_timeout`。

**Slice 9.5 in-process e2e smoke**(`test_mean_rebate_e2e.py`):
- ✅ 完整 e2e:JSON config → JSON loader → Strategy(Check/Action registry)→ `evaluate_tree` 命中(rate=0.25,3-way 套利) → `PlaceBetsAction.execute` log 3 leg(`would submit: ... qty=5.6250 price=4.0` × 3)

### strategy-4.29:PlaceBetsAction spread 限价偏移
- **.1**:`spread=0.02` 时最终订单先转成 YES 隐含概率，BUY 概率减 0.02、SELL 概率加
  0.02，再反算为 venue 价格；PM 价格因此直接加减，OE/SE decimal 赔率按倒数关系变化，
  qty 不变
- **.2**:PM 越界价格裁剪到 `[tick, 1-tick]`；decimal 越界裁剪到 `[1.01, 1000]`
- **.3**:负数、`>=1`、NaN、Infinity 配置 fail-fast
- **.4**:PM 互斥库存拆为 SELL+BUY 后再应用 spread，两条子单 qty 保持原 sizing 结果
- **.5**:per-venue `venue_required_balance` 使用 spread 后的最终价格
- **.6**:Action 保留 spread 反算后的计划价格，不重复执行 OE/SE 分段赔率量化；合法档位只由
  Execution adapter 在最终 `placeBets` payload 边界保证

- ✅ 阈值 smoke:rate=0.20 但 min_rate=0.30 → 不命中
- ✅ recovery config smoke:`compensation_tree` 引用 `mean_rebate_recovery` + `place_bets(intent="recovery")` 可经 JSON loader 构建
- ✅ `arb_config.example.json`: `mean_rebate` 默认包含 `compensation_tree` recovery 链
- **不依赖** PM enricher / NT TradingNode / Cache — 验证 framework + JSON 配置 + 3 个用户域 Check/Action 实际打通

### strategy-4.32: PlaceBetsAction 订单级市价意图
- 前置:`PlaceBetsAction(market=true)` 生成多腿 submit spec。
- 期望:每条真实腿都携带 `market=true`，submitter 编码为 `arb:market=true`；Action 保留原
  price/qty，不读取盘口深度、不执行 venue-specific 改价。缺失或 `false` 均按限价提交；
  非 boolean 配置 fail-fast。
- 验收:✅ `test_action_place_bets.py::test_action_market_is_written_to_every_submit_spec` /
  `test_action_rejects_non_boolean_market`，`test_submitter.py::test_submit_writes_opportunity_metadata_tags`，
  `test_mean_rebate_e2e.py::test_place_bets_market_param_loads_from_strategy_json`，
  `tests/arbitrage/common/test_opportunity.py::test_opportunity_meta_round_trips_market`。

## pre_rebate 策略(#326,设计 · 组件未落地 · live-unvalidated)

设计见 strategy §3.10。用例待 `pre_move` / `in_game` / `mean_rebate_recovery.force` 落地后补实现。

### strategy-4.pre_rebate.1: in_game StateQuery 判态
- sports_store `live=True, ended=False` → `in_game` True;`NOT in_game` False。
- sports_store `None`(真赛前 / 未订上)→ `in_game` False;`NOT in_game` True。
- sports_store `live=False`(赛前有帧)→ `in_game` False。
- sports_store `ended=True` → `in_game` False(落入赛前支,已知边界)。
- 缺 game_id / 缺 sports_store → `in_game` False(fail-closed)。

### strategy-4.pre_rebate.2: pre_move 追腿命中/不命中
- first_price `{yes:0.5, no:0.5}`、现价 `{yes:0.38, no:0.62}`、`move_threshold=0.1` → yes 跌 0.12 ≥ 阈 → 写单条 PM `BUY yes` leg,`qty=qty_from_share(PM, share, 0.38)`。
- 同上但 `move_threshold=0.15` → 最大跌幅 0.12 < 0.15 → False,不写 leg。
- 等于阈值(跌幅==阈)→ 命中(`>=`)。
- first_price 空(`{}`)→ False(无基准,安全 no-op)。
- PM 盘口缺腿 / 概率非法 / outcome 键不匹配 → False。

### strategy-4.pre_rebate.3: 赛前门只赛前追腿
- `NOT in_game` 为真(赛前)且 pre_move 命中 → arb 树出 submit plan。
- `in_game` 为真(赛中)→ arb 树 self_hits False → 不追腿(即便 first_price 有 mover)。

### strategy-4.pre_rebate.4: 赛前按率补 vs 开赛强补
- 赛前失衡持仓,`mean_rebate_recovery(min_repaired_rebate)`:补后率 ≥ 阈 → B2 支命中补救;补后率 < 阈 → 不补。
- 赛中同一失衡持仓,`force=true` → **无条件**补到 `target_share`,不看当前/补后率。判别性:同一仓位赛前前置门判"已达标不补",赛中 force 仍补。
- 缺口补平后 force 支不再命中(自终止)。
- **`pnl=false` 排除 realized(#327,防即买即卖)**:同一失衡持仓 + banked 正 realizedPNL(经 `RealizedPnlLedger`/Portfolio)→ `pnl=true`(默认)时补后率被 banked 抬过阈值 → recovery 触发;`pnl=false` 时补后率只按当轮开仓投影 → 低于阈值 → 不触发。判别性证明 realized 已被排除。`target_share`/denom 两种 pnl 下相同。
- `outcome_exposures(pair_id, include_realized_pnl=False)` 返回不含 realized 的开仓投影;`True`(默认)与 Risk profit gates 口径一致(`test_portfolio` 补该 kwarg 用例)。

### strategy-4.pre_rebate.5: 链路与树间取舍
- 两条链均 `[place_bets]`,place_bets 直接消费 `legs`(无 candi_select 也能出 plan)。
- 赛前 B1(arb)与 B2(comp)同轮命中 → comp_plan 优先(先补救)。
- 低于最小下注额的腿 → 由 Risk 兜底拒(无 candi_select 早筛)。

## 价格趋势 #trend(#328,plumbing 已落地 · 消费件未定)

设计见 strategy §3.8.3。`test_price_trend.py` 覆盖 `StrategyEvaluator._update_price_trend`:

### strategy-4.trend.1: 每帧 Δprob + 跨帧携带
- 首帧只 seed `_price_last`,无趋势条目(无 prev)。
- 第二帧起 `_price_trend[iid] = new - prev`;概率变大→正、变小→负;`_price_last` 滚动到当前。
- 判别性:若"读 last 再比当前"会恒为 0,本用例证明趋势在覆盖前已算好存下。

### strategy-4.trend.2: 分 venue / 分 leg
- key = `str(instrument_id)`(含 venue+outcome);PM yes / PM no / OE 各腿趋势互相独立。

### strategy-4.trend.3: 边界
- 缺 book / best_ask ≤0 / 缺 instrument_id → 跳过,不覆盖 last、不写 trend。
- **仅深度帧不冲趋势**(#trend 补,2026-08-10):`new_best == prev`(价未变、只深度变)→ 直接返回,趋势保留上次真实移动(非 0);下次真实移动仍从上次真实价算 Δ(`test_unchanged_price_keeps_prior_trend_not_flat`)。首帧后紧跟同价帧不造出 0 趋势(`test_unchanged_price_on_first_delta_seeds_no_trend`)。
- (概率空间可比:best_ask 已是隐含概率 #256,OE/SE 与 PM 同向,不二次转换——由 §3.8.3 契约保证,消费件落地时补断言。)

### strategy-4.trend.4: trend_gate Action(#329,消费件;跨 venue/outcome 一致)
`test_action_trend_gate.py`。**筛选 = 符合的留、不符合的删;不符合一致性 = 无腿符合 = 全删**。
判据:某 outcome 各 venue 都 up/flat、互斥 outcome 各 venue 都 down/flat、至少一处严格移动 → 该 outcome 干净上升。
- 默认 `up`:各 venue yes 涨/平、no 跌/平 → 只留 yes 腿;元数据不变。
- `trend="down"`:留下降 outcome(no)的腿。
- **跨 venue 不一致**(OE 的 yes 跌 vs PM 的 yes 涨)→ 无干净趋势 → **全删**。
- **缺 venue 数据当 flat**:OE 无数据、PM yes 涨 no 跌 → 仍判 yes 上升、留 yes 删 no。
- **全平**(含缺数据)→ 无严格移动 → **全删**。
- `price_trend` None/{}(未预热)→ **全删**;无 `selected_candidate` / 撤单 candidate → no-op / 跳过。
- `trend` 非法值 → 构造即 `ValueError`。

## 策略内组合场景

目录约定:`tests/arbitrage/strategy/scenarios/<strategy_name>/`。这里放“单个策略内部”的组合场景测试:
真实 Check/Action 可以串起来跑,但不启动 TradingNode,不进入 Risk/Execution/barrier,也不验证全链路真钱行为。

`scenarios/one_side_rebate/test_one_side_rebate_scenarios.py`:
- ✅ 已有仓位且 one_side 套利树与 mean_rebate_recovery 补偿树同轮命中时,补偿树优先触发；覆盖实盘数值 `NO 5.99@0.33 + YES ask 0.57142857`，补偿腿应为 `YES qty=5.99`。
- ✅ 已有仓位、某腿需要补偿且 one_side 套利未达阈值时,补偿树触发。
- ✅ 已有仓位时,one_side candidates 先经 `ShareLimitModification` 按剩余 share 缩放,再由 `CandiSelectAction` 选择缩放后最大 candidate。

## Slice 5 落地(2026-05-28 #44):Check/Action registry + JSON loader

`src/arbitrage/strategy/check_action_registry.py` + `src/arbitrage/strategy/json_loader.py`(框架层,具体 Check/Action 子类由用户后落)。

- ✅ `test_check_action_registry.py`:StateQuery/Check/Action 的 register + build、未知类型、参数错误和重复注册
- ✅ `test_json_loader.py`:StateQuery/AND/OR/NOT/嵌套、空 self_hits 默认真值、旧 signal 叶子拒绝；Condition/Strategy/Registry 装配与错误路径

## Debug 相关

**无 strategy 层 Debug 子类**(refactor.md #39 撤回 `DebugArbitrageStrategy` 整条)。
Strategy 的 debug 是**配置 vs 配置**(prod Strategy / dbg Strategy 同 scope,
`Check`/`Action` 用不同构造参数),不是**类 vs 类**。

其余 Debug 件(数据流 `Debug{PM,OE}DataClient` / Risk `DebugArbitrageLiveRiskEngine.skip_check_size` /
未来 `SkipExecution{PM,OE}ExecutionClient`)的测试在 `tests/arbitrage/debug/`,**不在本目录**。

---

## strategy-4.framework.x:Q21 新框架层用例(2026-05-24)

新框架的纯逻辑件,可全单测。落地顺序见 `architectures/strategy/architecture.md §7`。

### strategy-4.framework.expr.{1-6}:BoolExpr (AND/OR/NOT) + StateQuery 求值
- **.1**:`StateQuery.matches(ctx)` 读取当前 `EvalContext`，同一 query 对不同上下文得到对应结果
- **.2**:`AndExpr(a, b)` 全 True 才 True;任一 False → False
- **.3**:`OrExpr(a, b)` 任一 True 即 True
- **.4**:`NotExpr(a)` 取反
- **.5**:嵌套 `AND(a, OR(b, NOT(c)))` 求值正确
- **.6**:空 AND=True，空 OR=False

### strategy-4.framework.cond.{1-9}:Condition 树评估(EvalResult)
- **.1**:self_hits=False → `EvalResult(hit=False, action=None)`
- **.2**:self_hits=True、有 sub_conditions、第一个 sub 命中 → 返该 sub 的 EvalResult(后续不跑)
- **.3**:self_hits=True、有 sub_conditions、全没命中 → `EvalResult(hit=False)`
- **.4**:叶子节点(sub_conditions 空)、checktion AND 全过、actions 非空 → `EvalResult(hit=True, pending_actions=actions)`(不执行 action)
- **.5**:叶子、checktion 空 AND → 默认通过
- **.6**:叶子、actions 为空 → 仍 `hit=True`(`pending_actions=[]` 上层无事可 fire)
- **.7**:checktion OR 按配置顺序短路，只提交首个成功分支的 `scratch`
- **.8**:checktion AND 后项失败，回滚前项已写入的 `scratch`
- **.9**:checktion NOT 只反转结果，不提交子表达式的 `scratch`

### strategy-4.framework.check-expr.{1-5}:CheckExpr JSON
- **.1**:单个 `{"type": ...}` 解析为 Check 叶子
- **.2**:`AND(Check, OR(Check, NOT(Check)))` 可递归解析并正确求值
- **.3**:缺失 / `null` / `{}` 解析为空 AND=True
- **.4**:旧数组格式 fail-fast，不保留第二套配置 schema
- **.5**:未知操作符、错误 value 类型或叶子未知字段抛 `StrategyConfigError`

### strategy-4.framework.reg.{1-4}:StrategyRegistry scope 优先级 + 挂载锁定
- **.1**:只挂 sport → 找该 sport 下任意 pair 都返该策略
- **.2**:挂 sport + comp → comp 内 pair 返 comp 策略;comp 外的 pair 返 sport 策略
- **.3**:挂 sport + comp + 具体 pair → 该 pair 返 pair 策略,**即使没命中也不下放**(Q21-a 挂载存在锁定)
- **.4**:都没挂 → 返 None

### strategy-4.framework.eval.{1-5}:StrategyEvaluator 评估器
- **.1**:`on_data` 收 `OrderBookDeltas` → 查 PairRegistry 拿 pair_id → 查 StrategyRegistry 拿 strategy → 触发 `_evaluate_strategy`
- **.2**:strategy 为 None(无挂载)→ no-op,无 fire
- **.3**:Q19:`_execution_active` True 时 evaluate 跳过(让路)
- **.4**:arb.hit=True + comp.hit=True 时两条 Action 链都在独立 scratch 内生成 plan；
  Evaluator 只分发 compensation plan
- **.4a**(`test_arb_and_comp_evaluation_scratch_is_isolated`):两树 scratch 不共享，禁止跨树
  candidate 注入；补偿计划不得继承套利参数
- **.5**:补偿树没有生成有效 plan 时，同轮可回退并分发套利 plan

### strategy-4.framework.eval.{15-16}:per-pair 串行闸(§6.10 §7,#84)
- **.15**(`test_same_pair_concurrent_eval_fires_once`):同 pair 两次 `on_data`(drain 前,模拟同突发并发)→ 第一次 `_dispatch_eval` 同步 `try_enter` 成功派发评估,第二次 gate busy → **不派发**(`loop.tasks` 仅 1)→ drain 后只 fire 一次。**#260 起断言 gate 已释放**(该用例的 `_RecordingAction` 不提交任何订单 → 所有权未交出);旧断言是「fire 后仍 in-flight」,那正是泄漏本身 —— action 空转也永久占闸,该 pair 再不被评估。
- **.16**(`test_different_pairs_not_blocked`):不同 pair 各自 `try_enter` 成功 → 各派发各 fire(per-pair 不互相阻塞)。

### strategy-4.framework.eval.gate.{1-4}:pair 闸出口(#260 引入,#261 收窄)
> 设计 = synchronization §7.3(#261 后闸只作用于 strategy,出口只剩 `_on_eval_done` 一处无条件释放)。
> **全局 ≤1 执行不在本组** —— 由 barrier 保证,见 `tests/arbitrage/execution/test_engine_barrier.py`。

- **.gate.1**(`test_gate_released_even_when_actions_submit`):action 提交了单 → 闸**照样释放**。
  #261 取消了「已 fire 则持有」的交接判据(判据会漏,是 #260 泄漏的根源)。
- **.gate.2**(`test_gate_released_when_actions_submit_nothing`):action 零提交(上游清 legs / abort)→ 释放。
- **.gate.3**(`test_gate_released_when_action_raises`):action 抛异常 → task 以 exception 完成 → 释放。
  (`_on_eval_done` 的 error 日志不断言:NT `Logger` 只读 Cython、`init_logging` 每进程仅一次,拦不住。)
- **.gate.4**(`test_gate_released_when_task_scheduling_fails`):`_create_task` 抛 → 协程从未排程 → 释放且异常上抛。
- **.gate.5**(`test_released_gate_allows_reevaluation`):释放后同 pair 下一轮能再评估。

- gate 自身单测见 `tests/arbitrage/common/test_pair_inflight.py`(并发放弃 / 不同 pair 独立 / **无条件释放** / 重复释放幂等 / 未 acquire 就释放安全 / **执行段 API 必须真的消失**的守卫)。**#105 ②:无 max-hold、无 `clear_all`;#261:无 exec 记账**。设计 = synchronization.md §7。

### ~~strategy-4.framework.eval.17:健康检查互斥~~ —— 已删除(#108,2026-06-16)
> 测试 `test_health_check_active_skips_fire` 已删除;strategy⊥健康检查互斥(`_hc_running` + `health_check.*`)退役。详见 synchronization §8.6。

> **已删除(#105 ②,2026-06-15,用户"都撤")**:旧 eval.18/.19(健检 `finished`→`clear_all` 兜底)与 eval.20-22(`try_enter` desync A5 兜底)随 max-hold / clear_all / A5 一并退役。in-flight 出口改由 opportunity barrier 出口 + session `exec_started`↔watchdog 原子保证(synchronization.md §7.3),单测见 `test_session.py`(watchdog 原子 / 出口对称)+ `test_engine_barrier.py`(barrier deny/timeout `release_eval`)。

### strategy-4.framework.open-orders.{1-3}(#266)
- **.1**:evaluate 开跑时对 pair open orders 取稳定 digest
- **.2**:Cache 返回顺序/对象身份变化不改变 digest；订单关键字段或集合变化会改变 digest
- **.3**:行情、持仓、instrument info/constraints 不冻结，Strategy 在使用点读 live Cache

## #228:checks 换 outcome 分组键 + no 腿执行透传(2026-07-15)

- `test_check_mean_rebate.py::test_3way_split_pair_yes_no_arb_triggers_above_threshold` / `test_3way_split_pair_decimal_no_leg_carries_lay_and_exec_redirect`:[yes,no] pair 按 `claim or selection_role` 分组,合法集 = `snap.outcomes`(旧 home/draw/away 三 role 分支退役);decimal no 腿 prob=1−1/lay,选中时 leg 带 `claim/lay_price/exec_instrument_id`。
- `test_check_one_side_rebate.py::test_3way_split_pair_yes_no_generates_candidates`:candidate legs 透传 claim 执行字段。
- `test_action_place_bets.py::test_decimal_no_claim_redirects_to_exec_instrument`:合成 no 腿真单重定向到同 selection 的 yes instrument(SELL@lay,qty=share/lay)。

## #233:canonical outcome + 下单前等价拆单

- 2-way/3-way pair 的经济 outcome 统一为 `yes/no`;真实 decimal NO 仍 BUY/BACK，只有带 `exec_instrument_id` 的合成 NO 转 SELL/LAY。
- `test_action_place_bets.py` 覆盖 PM 目标 BUY 100、有互斥仓位 60 且 SELL 限价不高于当前 best bid 时拆为 SELL 60 + BUY 40；SELL spec 携带该 LONG 的 `position_id`。多个 LONG Position 时按 ID 拆成多条 SELL，各自绑定对应 Position，避免 NT 为减仓另开 SHORT。缺 best bid、SELL 限价高于 best bid、应用 spread 后不再交叉、缺 Position ID 或拆分后不满足单笔最小数量时回退原 BUY；等于 best bid可转换。仓位 97 时按最小 5 调成 SELL 95 + BUY 5；仓位 3 时回退原 BUY；仓位 100 时只 SELL。这里只校验价格可立即成交，不按 bid 深度缩量。
- `test_submitter.py::test_submit_passes_inventory_position_id_to_native_strategy_submit` 与 `test_evaluator.py::test_submitter_binds_inventory_sell_to_existing_position_id` 分别验证 submitter 参数和真实 `RiskEngine.execute` 命令/cache 映射均保留既有 Position ID。
- 同 venue 子单共享实际 `expected_legs` 与 `venue_required_balance`；PM SELL 对资金需求贡献 0。
- `test_mean_rebate_e2e.py`:e2e fixture 改为 [yes,no] 拆分 pair(2 腿)。
- mean_rebate_recovery 已按 #230 支持 `[yes,no]` pair,策略内用例覆盖:
  - PM NO LONG 已持仓 → 正确归 no,缺 yes 时生成 BUY yes recovery leg。
  - decimal LAY SHORT 已持仓 → 正确归 no,缺 yes 时生成 BACK/BUY recovery leg。
  - 已有 yes 持仓、no 缺口 → 可选择 PM NO BUY 或 decimal SELL@lay,并透传 `claim/lay_price/exec_instrument_id`。
  - repaired rebate 低于阈值仍拒绝;现有 2-way recovery 行为不回归。

## #234:PM 拆单的 BUY-only 最小金额

- Snapshot 冻结 `min_quantity/min_buy_notional/size_increment`，不泄漏 live Instrument。
- `test_probability_split_keeps_minimum_buy_notional_at_low_price`:目标 BUY 100 @ 0.02、互斥 LONG 97 时，BUY 子单至少 50 shares，最终拆为 SELL 50 + BUY 50；SELL 子单不应用 1 USD 下限。

## #235:共享候选腿与持仓异常 fail-closed

- mean_rebate、one_side_rebate、mean_rebate_recovery 的行情候选腿统一由
  `checks/quote_legs.py::quote_legs_by_outcome` 构造；现有三组 check 用例共同覆盖 best ask、
  `quote_claim` 概率与 lay 执行字段透传，避免三份构造逻辑漂移。
- PlaceBets 的 `venue_required_balance` 汇总逐单调用 Venue Registry `order_required_balance`。
- ShareLimit 经严格 Portfolio 遇到缺 claim 或 probability SHORT 时清空本轮输出；Recovery 遇到
  probability SHORT 等经济投影不变量错误时停止，不按零敞口继续。直接验收用例为
  `test_single_legs_are_cleared_when_portfolio_invariant_is_broken` 与
  `test_recovery_rejects_probability_short_position`。

## #282:Recovery 使用包含 realized PnL 的 Portfolio 净利润

- `test_recovery_uses_portfolio_net_profit_including_realized_pnl`:已有单边持仓时，Portfolio
  返回的 Data API 对账 SELL/merge realized PnL 会进入当前 rebate 前置门；已达阈值则不再
  产生补单。
- target share 仍只取 open positions，不用 realized PnL 虚增 share。

## #250/#322:PMSPORTS 状态触发 Strategy(已落地,`test_evaluator.py`)

> #322:strategy 只用 `ended` → 订 **`phase` 通道**(`sports_data_type(gid, SPORTS_CHANNEL_PHASE)`);
> 下列"per-game topic"均指该场 `phase` 通道 topic。比分/钟表帧不再噪声唤醒评估。见 data §3.4.2。

### strategy-4.sports.1:MatchedPair 按场订阅 + per-(game,phase) topic 触发评估

**用例**:`test_matched_pair_subscribes_per_game_topic_and_routes_events`。
**期望/验收**:MatchedPair 到达时经 `game_id_for_pair` 反查并订阅该场 `phase` 通道;该 topic 发布
经 NT 路由到 `on_data` 并恰好触发一次评估。

### strategy-4.sports.2:同 game 的全部 pair 均被触发

**用例**:`test_sports_update_fans_out_to_all_registered_pairs_for_game` /
`test_sports_fanout_respects_pair_inflight_gate`。
**期望/验收**:`pair_ids_for_game` 扇出全部注册 pair,各 pair 独立过 `PairInFlightGate`;
不得沿用单值反查只触发第一个。

### strategy-4.sports.3:未注册 game no-op

**用例**:`test_sports_update_unregistered_game_is_noop`。
**期望/验收**:不创建评估 task,不报错。

### strategy-4.sports.4:sports store 注入 EvalContext

**用例**:`test_evaluator_injects_sports_store_into_eval_context`(`test_evaluator.py`)。
**期望/验收**:Evaluator 把 Cache-backed `SportsGameStateStore` 注入 `EvalContext`；
状态查询可按需读取，不派生 signal。

### strategy-4.sports.5:SportsGameUpdate 只负责触发和定位

**用例**:`test_sports_update_fans_out_to_all_registered_pairs_for_game`。
**期望/验收**:事件到达前状态已写入 Store；Evaluator 不复制 sports 状态，也不保存暂态/持久态 signal。

### strategy-4.sports.6:ended 释放本场全部订阅

**用例**:`test_ended_releases_sports_and_obd_subscriptions`。
**期望/验收**:ended 扇出分发完毕后,退订本场 sports 与自记的各 pair 腿 OBD;与 matching 侧
退订汇合归零 → NT 收尾 + 内存回收(Store 条目、managed book)。

### strategy-4.sports.7:PM first/start price Cache

**用例**:`test_pair_prices.py`、`test_evaluator.py::test_first_price_*`、
`test_start_price_not_captured_without_witnessed_first_price`、
`test_start_price_captures_in_play_after_first_price_witnessed`、
`test_ended_deletes_pair_prices_after_last_evaluation_finishes`。

**期望/验收**:
- MatchedPair 按 outcomes 幂等初始化 `first_price={}`、`start_price={outcome:0.6}`；
- 只有赛前 PM OBD 的完整 ask 向量且概率和在 `[0.95,1.05]` 内才首次写 first price；
  非 PM OBD与不干净向量不写；
- IN_PLAY phase **仅当该 pair 已采到 `first_price`(见证过赛前)**才对完整 PM 向量首次写
  start price 且不做概率和校验；中途接入(采不到 first_price)不写、保持默认 0.6
  (#322 修订的 late-join 护栏,避免盘中赔率误当开赛价污染 dash_gate 阈值);
- ended 调度后的异步评估运行期间记录仍存在，最后一个评估 task 完成后才删除 pair 记录和
  game 索引。

## strategy-4.30:one_side_rebate 近价挂单撤单补偿

- `test_check_spread_cancel_recovery.py`:无挂单或概率差未达阈值不命中；PM BUY 挂单与当前
  ask 的 outcome exposure probability 差满足严格 `< spread` 时写标准 legs + 显式 pair
  撤单意图；decimal 合成 NO 的真实 `SELL@lay` 按执行 instrument/side 转成补集概率后
  比较（即使原始赔率差大于 spread 也可因概率差命中）；非法 spread fail-fast。
- `test_action_place_bets.py::test_action_cancel_request_cancels_pair_without_submitting`:
  `PlaceBetsAction` 先生成 cancel plan，统一 dispatcher 才调用 pair canceler，且不调用 submitter。
- `test_action_place_bets.py::test_action_cancels_when_selected_recovery_candidate_carries_request`:
  补偿树选择出的 spread cancel candidate 生成撤单计划而非下单计划。
- `test_evaluator.py::test_pair_order_canceler_reloads_and_cancels_all_pair_open_orders`:
  dispatcher 执行 cancel plan 时重新读取目标 pair 全部 open orders；逐单发 NT CancelOrder，并为所有命令
  写入相同 `opportunity_id/expected_cancels`，由 Execution grouped cancel barrier 收齐后
  跨 venue 统一 release。
- `test_evaluator.py::test_spread_cancel_recovery_completes_comp_tree_then_wins_dispatch`:同轮两树
  命中时，spread cancel 完整经过补偿树生成 cancel plan；统一分发选择补偿，只执行 grouped cancel。
- `test_mean_rebate_e2e.py::test_spread_cancel_and_mean_recovery_build_as_or_expression`:
  JSON loader 可将 `spread_cancel_recovery OR mean_rebate_recovery` 装配为补偿 CheckExpr。
### strategy-action-place-bets-log: 实际策略汇总日志

- **前置**:`PlaceBetsAction` 分别消费 mean_rebate legs-only 候选和带
  `strategy=one_side_rebate`、`rate` 的 selected candidate。
- **期望**:汇总日志统一输出 `strategy/rate`；one_side_rebate 不再打印历史字段
  `mean_rebate_rate=None`。逐腿计划 `price/qty` 日志保持不变。
- **验收**:`test_action_place_bets.py::test_action_logs_each_leg`、
  `test_action_place_bets.py::test_action_summary_logs_selected_one_side_rebate_candidate`。
