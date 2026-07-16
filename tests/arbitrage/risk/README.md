# risk 测试

对应章节: `refactor.md §5.6, §6.9, 修订记录 #23`;详细设计 `architectures/risk/architecture.md`

**Step 6 落地状态(2026-06-22,2026-06-28 share limit 迁出,2026-06-30 SE 接线,2026-07-01 Venue Registry helper,2026-07-02 ArbContext keyed helper,2026-07-14 Q17 accepted 本地预扣)**:`src/arbitrage/risk/{engine,portfolio,config}.py` + `bootstrap.py` 已实现。覆盖:cpdef `_check_order` 覆盖经 `_handle_submit_order` 派发 + 自 emit deny + 不泄漏(risk-6.7.1)、VenueExecutionLiveness(经 common helper 解析 expected_legs required venues,含 SHARPEXCH;无法解析的 expected leg key fail-closed)、单场 profit gates(6.7.2/3/4)、概率/赔率门控(经 Venue Registry 转换 PM probability / OE-SE decimal odds)、余额统一读 account free + 成本按 venue capability 计算(6.3/6.3b/6.3c)、outcome_exposures/outcome_shares/outcome_shares_for_venue 公式(经 Venue Registry 判定 probability/decimal,含 OE/SE venue identity 分离)、导入名替换 + wire(6.9.1)、`ctx_map_get/require/set/get_or_create` keyed map helper,其中 `ctx_map_require` 缺必需 keyed 值会 fail-fast,`ctx_map_get_or_create` 只在缺失时创建并回写共享对象,`prepare_arb_context` 传旧 venue 专属字段会 TypeError。旧 `LegSettledRegistry` gate、way_rebate 接口、Risk share limit gate、旧 PM open-order 自扣余额与全局止盈/止损已退役;share limit 现归 Strategy Action。

> ⚠️ **2026-06-15 设计变更**:上述 `leg_settled` / settled gate 用例是历史状态。新设计为 `VenueExecutionLiveness`:Portfolio 不再读执行健康;Risk 从 opportunity `expected_legs` 推导 required venues,用 `order_alive && position_alive` 做 fail-closed 门控。旧 settled 用例在代码迁移时应删除或改写为新 liveness 用例。

**写测试时抓出两个 production bug(已修)**:① `ArbitragePortfolio` 读不到基类私有 cdef `self._cache`(`Portfolio._cache` 非 readonly)→ 覆盖 `__init__` 自存 `_arb_cache`;② `order.has_price_c()` 是 cdef 不可从 Python 调 → 改 `order.has_price`(property)。两者运行期才暴露,纸面 review 抓不到——印证落地测试价值。

**#34(2026-05-24)pair_id 来源校准**:`_resolve_pair_id` / `_pair_id_for_order` 原读 `info["competition"]` 是错读(`competition` 是联赛名,EPL/NFL...,非 pair_id)。现改读 matching 的 `PairRegistry`(`configure_arb(pair_registry=...)`);测试用例同步加 `PairRegistry.register(pair_id, [instrument_ids])`,见 `test_engine._gate_ctx` / `test_portfolio.test_resolve_pair_id_reads_from_pair_registry`。`_leg_from_position` 同步加 `selection_role`(Q9 标准 key)兼容 `market_type` fallback。

**仍待落 .py / 延后**(需全节点 / discovery / execution 接线):risk-6.1/6.2(全管道透明拦截 + NT min_quantity 自动拒,需起节点)、risk-6.3/6.4(cache 真实持仓 + venue stale 兜底)、risk-6.5/6.6(账户状态维护,属 execution)、risk-6.7.5/6/9 与 6.9.{2全路径,3,5,7,8,10,11}(需 cache 真实 Position 而非 duck/stub)。

## 锁定的关键性约束(2026-05-09 三次修正后)

Risk 层在 NT `submit_order` 管道上**透明拦截**,Strategy **不引用** Risk。账户状态由 ExecutionClient 维护(PM 主动 / OE 被动 WS),Risk 层只读 Cache。**没有独立的 BalanceMonitorActor**(告警让前端自己看,见 §5.7)。

```
ExecutionClient (维护账户)
├── PM: 连接/显式 QueryAccount + accepted 本地预扣 → generate_account_state → Cache
└── OE/SE: 余额真值帧/response + accepted 本地预扣 → generate_account_state → Cache
                ↓
        ArbitrageLiveRiskEngine._check_order  ← 同步读 Cache 做余额检查
                ↓
        WebGatewayActor (§5.7) 订阅 AccountState → 推前端 JSON
                                                    (用户看着判断,无系统层告警 Actor)
```

**唯一组件**: **`ArbitrageLiveRiskEngine`**(NT `LiveRiskEngine` 子类 —— 实盘 kernel 用 Live 版,非基类 `RiskEngine`)
- NT 父类自动处理最小限额(PM:`instrument.min_quantity=5 shares`;OE/SE:`instrument.min_notional=Money(min_stake * fx, USD)`)
- 子类加 `_check_balance` hook 做余额检查

**删除**:
- `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 常量 + `check_min_size` 函数 + `adjust_share_by_liquidity` Step 2
- `LiquidityRiskActor` / `LiquidityRiskService` 整层
- `BalanceMonitorActor`(告警让前端做,Step 6 不引入此 Actor)

## 预期用例(摘要)

### risk-6.1: ArbitrageLiveRiskEngine 透明拦截 submit_order
- 前置: NT TradingNode 启动,Strategy 准备 submit
- 输入: `Strategy.submit_order(...)` 一笔订单
- 期望: `ArbitrageLiveRiskEngine._check_order` 被调用 → 通过则路由到 ExecutionClient,被拒发 `OrderDenied`
- 验收: Strategy 不需要主动调任何 risk API,完全透明

### risk-6.2: NT 自动处理 venue 最小下单额
- 输入:
  - PM:提交一个 `quantity < instrument.min_quantity(5 shares)` 的订单
  - OE/SE:提交一个 USD stake/notional `< instrument.min_notional(min_stake * fx)` 的订单
- 期望: NT `RiskEngine` 父类自动拒绝,`Strategy.on_order_denied` 触发
- 验收: 应用层无需任何 `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 代码;Provider 元数据由 `tests/arbitrage/adapters/polymarket/test_parsing_min_size.py::test_parse_polymarket_instrument_sets_min_quantity_from_order_min_size` / `tests/arbitrage/discovery/test_orbitexch_provider.py::test_build_legs_sets_orbitexch_min_stake` 锁定,全管道拒单仍待节点级 risk-6.2 集成测。NT core 拒单日志已降为 DEBUG,默认日志不再把预期 min-notional/min-size 拒单刷成 WARN;验收应看 `OrderDenied` / strategy 回调 / barrier deny 事件,不要依赖 WARN 行。

### risk-6.3: 应用层余额检查(统一读可用余额,Q17 修订已落地)
- 前置: ExecutionClient 已写入 cache.account_state
- 输入: 提交一个超出**可用余额**的订单
- 期望: `ArbitrageLiveRiskEngine._check_balance` 拒绝,`Strategy.on_order_denied` 触发
- 验收: 检查依据 = `account.balance_free(currency)`。ExecutionClient 已把 venue 真值余额和 accepted 后本地预扣都写成 `total=free=available, locked=0`;Risk 不再按 venue 自己扣 open orders。

### risk-6.3b: 订单成本按 venue capability 计算,余额来源不按 venue 分支(Q17 修订已落地)
- **probability venue(当前 PM)**
  - 前置: PM cache `free=40`;订单 `quantity=50 shares`,`price=0.9`。
  - 输入: 提交该订单。
  - 期望: `_check_balance` 经 Venue Registry `probability_from_price` 算成本 `50*0.9=45`,因 `free=40` 拒绝。
  - 验收: 成本公式按 `odds_model=probability` 派生;余额只读 `free`,不再 `total - open_orders`。
- **decimal venue(当前 OE/SE)**
  - 前置: OE/SE cache `free=40 USD`;订单 `quantity=50`,`price=2.0`。
  - 输入: 提交该订单。
  - 期望: `_check_balance` 算成本 `50`,因 `free=40` 拒绝。
  - 验收: 成本公式按 `odds_model=decimal` 派生;新增 decimal venue 只需配置 Venue Registry,不改 Risk 分支。
- **decimal LAY**
  - 前置:cache `free=30 USD`;SELL/LAY 订单 `quantity=10`,`price=5.0`。
  - 输入:提交该订单。
  - 期望:`_check_balance` 按 liability `10*(5-1)=40` 拒绝,不能按 stake 10 放行。
  - 验收:`test_balance_decimal_lay_uses_liability_not_stake`。

### risk-6.3c: accepted 本地预扣后 Risk 不双扣 PM open orders(Q17 修订已落地)

### risk-6.3d: opportunity 同 venue 整组余额门控(#233)

- `test_balance_uses_opportunity_venue_total_for_each_leg`:单腿 cost 虽小于 free，只要 metadata 中同 venue 整组需求超过 free 即拒绝。
- `test_balance_pm_sell_reduction_requires_no_quote_balance`:probability SELL 减仓不占 quote balance。
- 前置:PM ExecutionClient 已在 `OrderAccepted` 后把账户从 `free=100` 本地预扣为 `free=90`;cache 中同时存在该 open order。
- 输入:再提交成本 `95` 的 PM 订单。
- 期望:`_check_balance` 只读 `free=90`,拒绝;不会再额外扫描 open orders 得到 `80/更低`。
- 验收:旧 `_probability_open_notional` 路径已删除;测试证明 `_check_balance` 的可用余额来源只看 `free`。

### risk-6.4: cache stale 时由 venue 拒绝兜底
- 前置: cache 余额过期,venue 真实余额已不够
- 输入: 提交订单(过 RiskEngine 检查 → 路由到 venue)
- 期望: venue 返回 INSUFFICIENT_BALANCE → `generate_order_rejected` → `Strategy.on_order_rejected`
- 验收: 这是**异常路径**,不是设计层"双兜底",Strategy 应能处理

### risk-6.5: PM ExecutionClient 事件驱动维护账户状态
- 前置: PolymarketExecutionClient 启动
- 输入: 触发任一上游事件(连接时 / `CONFIRMED` 成交确认 `POLYMARKET_FINALIZED_TRADE_STATUSES`)
- 期望: cache.account_state(POLYMARKET) 自动更新
- 验收: 路径完全在 ExecutionClient 内,无独立监控 Actor;**上游无周期 timer、NT 无默认 QueryAccount 轮询、健康检查也不拉余额**(Q17,完全靠事件)

### risk-6.6: OE ExecutionClient 被动维护账户状态(WS)
- 前置: OrbitExchExecutionClient 启动,WS 已连接
- 输入: 模拟 OE WS 推送一条余额变化帧
- 期望: ExecutionClient 解析后乘 `arbitrage.fx`,再调 `generate_account_state` 写 Cache
- 验收: 被动路径,无 timer,完全反应式

---

## 单场 profit gates: match_tp / match_sl(Q16 修订,§5.6 `_check_profit_gates`)

Risk 不再按 `way_rebate` 比率门控,也不再执行全局止盈/止损。`ArbitrageLiveRiskEngine._check_profit_gates` 每次 submit 从 `ArbitragePortfolio.outcome_exposures(pair_id)` 读取所有 outcome 的绝对金额 `net_profit/liability`,用 `ArbitrageParams.share` 计算阈值:止盈阈值 `share*match_tp`,止损阈值 `share*match_sl`。逐 submit deny = 别开新仓。**无 TradingState 翻闸、无监测 Actor、无频率**。概率门控位于 NT 父类检查之后、venue liveness/余额/profit gates 之前;概率转换统一经 Venue Registry helper。

`ArbitrageParams.max_leg_share` 只作为 Web Arbitrage → StrategyEvaluator 的默认规模参数,供 strategy `share_limit` action 未显式配置时读取;RiskEngine 不执行 share-limit 缩放/门控。

### risk-6.7.1: `_check_order` 签名与父类一致 + super 先行(✅ 已 e2e 验证)
- 前置: `ArbitrageLiveRiskEngine` 已装入管道
- 输入: 提交一笔正常订单
- 期望: `_check_order(self, instrument, order)` 两参签名;先调 `super()._check_order(instrument, order)`(NT 仅 price/quantity/GTD),再 `_check_balance`,再 `_check_profit_gates`。**notional/submit_rate/native 余额不在 `_check_order`,在父类 `_check_orders_risk_for_account`(本类不覆盖,管道上随后原样跑)**
- 验收: 任一返回 False 即 `OrderDenied`;签名与 `engine.pyx:571` 一致,**override 被 Cython `_handle_submit_order` 派发到(已用真实 SubmitOrder 跑通:覆盖触发 1 次 + deny 事件发出 + 订单不泄漏到 exec)**。⚠️ 自定义 deny 必须自调 `self._deny_order(order, reason)`,否则订单静默丢弃、`on_order_denied` 不触发

### risk-6.7.1b: VenueExecutionLiveness gate 顺序与 fail-closed(2026-06-15)
- 前置: `ArbitrageLiveRiskEngine` 注入 `VenueExecutionLiveness`;某 opportunity 的 `expected_legs=("pm:home:0","oe:away:1")` 或 `("pm:home:0","sharpexch:away:1")`;PM `order_alive=true/position_alive=true`,外部 venue `order_alive=false/position_alive=true`。
- 输入: PM leg 或 OE leg 任一 SubmitOrder 进入 `_check_order`。
- 期望: `super()._check_order` 通过后,`_check_required_venues_alive` 发现 required venues 中 OE 不 alive → `_deny_order`。
- 验收:
  - `_check_balance` 和 `_check_profit_gates` 不再继续执行。
  - 原生 `OrderDenied` 与 `risk.opportunity.leg_denied` 都发布。
  - 两条腿无论当前 order 自己 venue 是 PM 还是 OE,结果一致 deny。

### risk-6.7.1c: required venues 从 expected_legs 推导,不是只看当前 venue
- 前置: `expected_legs=("pm:home:0","oe:away:1")`,当前订单是 PM leg;PM alive,OE not alive。
- 输入: PM leg SubmitOrder。
- 期望: 仍 deny,因为 required venues 包含 OE。
- 验收: Risk 使用 `expected_legs` partner 信息解析 required venues;不新增 `required_venues` tag 也能工作。

### risk-6.7.1c2: expected_legs 无法解析时 fail-closed
- 前置: `expected_legs=("pm:home:0","pmsports:event:1")`,当前订单是 PM leg;PM alive。
- 输入: PM leg SubmitOrder。
- 期望: deny;reason 包含 unsupported expected leg sentinel,不能退回只检查 PM。
- 验收:`test_liveness_gate_denies_unparseable_expected_leg_key`。

### risk-6.7.1d: 无 opportunity metadata 时退化为当前 venue liveness
- 前置: 普通非套利订单无 `arb:opportunity_id` / `arb:expected_legs`;当前 `order.instrument_id.venue=POLYMARKET`;PM not alive,OE alive。
- 输入: 普通 PM SubmitOrder。
- 期望: Risk 只检查 PM liveness 并 deny。
- 验收: 非套利订单不被 partner 协议污染。

### risk-6.7.1e: 概率门控拒绝极端概率/赔率
- 前置: `min_probability=0.03`,`max_probability=0.97`。
- 输入: PM order price=0.02/0.98;OE/SE order price=40.0(隐含概率 0.025)/1.02(隐含概率约 0.98)。
- 期望: `_check_probability_gate` deny;price=0.03/0.97 或 OE price=2.0 放行。
- 验收: `test_probability_gate_denies_pm_price_outside_bounds` / `test_probability_gate_allows_inclusive_pm_bounds` / `test_probability_gate_converts_oe_decimal_odds_to_probability` / `test_probability_gate_converts_sharpexch_decimal_odds_to_probability`;转换入口由 `src.arbitrage.common.venues.probability_from_price` 提供,非法热更新区间不 apply(`test_probability_bounds_hot_update_rejects_invalid_interval`)。

### risk-6.7.2: match_tp 触发 deny(止盈,赚够别加仓)
- 前置: pair_id="match_X" 持仓,`share=22.5`,`match_tp=0.05`;所有 outcome 的 `net_profit > 1.125`
- 输入: strategy 对该 pair 再 `submit_order`
- 期望: `_check_profit_gates` 判 match_tp → `_check_order` 返 False → `Strategy.on_order_denied`
- 验收: 必须所有 outcome 都高于阈值才 deny;任一 outcome 未超过阈值则放行;不平仓、不撤单、无其它动作

### risk-6.7.3: match_sl 触发 deny(止损,该场恶化别加仓)
- 前置: pair_id="match_X" 持仓,`share=22.5`,`match_sl=-0.05`;所有 outcome 的 `net_profit < -1.125`
- 输入: 对该 pair 再 submit
- 期望: deny → `on_order_denied`
- 验收: 必须所有 outcome 都低于阈值才 deny;任一 outcome 未跌破阈值则放行

### risk-6.7.4: global_sl 配置字段已删除(全局止盈/止损退役)
- 前置: NT `RiskSectionConfig` 严格 schema。
- 输入: 配置写入旧 `risk.global_sl`。
- 期望: loader schema mismatch,不会进入 runtime。
- 验收: Risk 只保留 `match_tp/match_sl` 单场 profit gates,不读取任何全局 profit/rebate 聚合指标。

### risk-6.7.5: 撤单不受 profit gates 影响(只挡开仓)
- 前置: 任一单场 profit gate 已触发
- 输入: 提交一笔 `CancelOrder`(如补偿撤单)
- 期望: 撤单正常路由到 ExecutionClient,**不被 `_check_profit_gates` 拦**
- 验收: deny 只作用于 `SubmitOrder` 通路;`bug_compensating_cancel_missing` 的补偿撤单照常发出

### risk-6.7.6: recovery intent 跳过 profit gates,但不跳余额
- 前置: Strategy submitter 给补救单写 `Order.tags=["arb:intent=recovery"]`;普通套利会触发 `match_sl`
- 输入: recovery submit
- 期望: `_check_order` 仍先跑 NT 父类基础检查和 `_check_balance`;余额通过时跳过 `_check_profit_gates` 并路由到 ExecutionClient
- 验收: 真实 `SubmitOrder` 管道下,recovery intent 在单场止损场景仍能到 exec;余额不足时仍 `OrderDenied`,不泄漏到 exec

### risk-6.7.6b: recovery intent 不跳过 VenueExecutionLiveness
- 前置: recovery SubmitOrder 带 `arb:intent=recovery`;required venues 中 PM `position_alive=false`。
- 输入: recovery submit。
- 期望: `_check_required_venues_alive` deny;不进入 `_check_balance` / `_check_profit_gates`。
- 验收: recovery 只跳过 profit gates,不跳过 venue liveness;撤单仍因不经 `_check_order` 而不受影响。

### risk-6.7.6: 机会评估与硬停正交(strategy 通过但 risk 仍拦)
- 前置: 某机会通过 strategy 的机会评估,但 Risk live `outcome_exposures` 显示所有 outcome `net_profit > share*match_tp`
- 输入: strategy 评估通过 → submit
- 期望: strategy 不自拦(机会评估正向门槛过),`ArbitrageLiveRiskEngine` 在管道上 deny(tp 硬停)
- 验收: 两层正交;strategy 不引用 risk,deny 经 `on_order_denied` 回传

### risk-6.7.7: settled entry 不存在 → 放行(Q-G1,2026-05-19,已失效)

> ⚠️ **失效**:`leg_settled` gate 退役;用 risk-6.7.1b~1d 覆盖新 liveness 行为。本用例仅记录历史,不作为当前验收。
- 前置: pair_id="match_X" 从没下过单 → `leg_settled` 无此 entry
- 输入: 对 match_X submit,旧 `_check_rebate_gates` 检查 settled
- 期望(旧行为): entry 不存在 = 无结算风险 → 放行
- 验收: absent ≠ false;absent 一律放行,不与 fail-closed 混淆

### risk-6.7.8: global_min_rebate_sum 返回 None → fail-closed deny(2026-05-19,已失效)

> ⚠️ **失效**:`global_min_rebate_sum` 接口已退役;venue 执行健康由 `_check_required_venues_alive` 拦截。本用例仅记录历史,不作为当前验收。
- 前置(旧行为): **别的某个 pair** 有腿 `leg_settled=false`
- 输入: 对**任意** pair(含 settled 干净的 pair)submit
- 期望(旧行为):`_check_rebate_gates` 读到 global `None` → **deny(拦截新开仓)**;全局图景不全时一律挡
- 验收:
  - 与 fail-open 相反:数据不全时挡而非放
  - 不死锁:撤单走另一通路照常(risk-6.7.5),健康检查 reconcile 结算后 global 恢复实数 → 后续 submit 自动放开
  - 实现不得 NoneType 崩:None 必须显式判定为 deny,不进入数值比较

### risk-6.7.9: profit gate 数据源 = ArbitragePortfolio 引用(非 cache 缓存)
- 前置: cache 持仓刚因 fill 更新
- 输入: `_check_profit_gates` 取 outcome exposure
- 期望: 持 `ArbitragePortfolio` 引用调 `outcome_exposures(pair_id)`,即时现算反映最新持仓
- 验收: **cache 不存 profit gate 结果**;`_check_profit_gates` 不读任何"已存 rebate"字段,不重复算法(算法只在 ArbitragePortfolio 一份)

### risk-6.7.10: opportunity leg deny 领域消息(已落地代码,待 live 验证)
- 前置: `SubmitOrder.order.tags` 包含 `arb:opportunity_id` / `arb:pair_id` / `arb:leg_key` / `arb:expected_legs`;Risk 某门控触发 deny。
- 输入: 该订单经 `ArbitrageLiveRiskEngine._handle_submit_order`。
- 步骤: 订阅 `events.order.*` 与 `risk.opportunity.leg_denied`。
- 期望:
  - 原生 `OrderDenied` 仍发出并进入 NT order/cache 标准链路。
  - 额外发布 `risk.opportunity.leg_denied`,payload 至少含 `opportunity_id/pair_id/leg_key/client_order_id/reason`。
- 验收: 领域消息是补充信号,不能替代 `OrderDenied`;Risk 不等待其它 legs、不释放 `pair_inflight`。
- 状态:✅ `test_engine.py::test_opportunity_deny_publishes_domain_message`

### risk-6.7.11: 无 opportunity metadata 的 deny 不发布领域消息
- 前置: 普通非套利订单或旧路径订单无 `arb:opportunity_id` tag。
- 输入: Risk deny。
- 期望: 只发 NT 原生 `OrderDenied`,不发 `risk.opportunity.leg_denied`。
- 验收: 非套利/外部订单不被 opportunity barrier 协议污染。
- 状态:✅ `test_engine.py::test_non_opportunity_deny_does_not_publish_domain_message`

### risk-6.7.12~15: share limit gate 已迁至 Strategy Action(2026-06-28)
- 状态:已失效。Risk 不再在 `_handle_submit_order` 中缩量,不再维护 share-limit gate。
- 新归属: `src/arbitrage/strategy/actions/share_limit.py`,测试见 `tests/arbitrage/strategy/test_action_share_limit.py`。

---

## ArbitragePortfolio: outcome_exposures / outcome_shares 领域指标(Q14,§6.9)

子类化 `Portfolio` 加 Python 方法,与 NT `unrealized_pnl` 并列扩展。Risk 门控读取 `outcome_exposures(pair_id)` 的绝对金额 `net_profit/liability`;Strategy `share_limit` action 读取 `outcome_shares_for_venue(pair_id, venue)` 按 venue 分开计算当前 outcome share。`way_rebate` / `min_way_rebate` / `way_rebates_by_venue` / `global_min_rebate_sum` 已退役。OE/SE position quantity 由 adapter 入站时归一为 USD stake,Portfolio/Risk 不再乘 fx。

### risk-6.9.1: 导入名替换 → kernel 原生构造 ArbitragePortfolio + ArbitrageLiveRiskEngine(✅ 部分已验证)

- 前置: 构造 `TradingNode` **之前**调 `bootstrap.install_arbitrage_engines()`(替换 `nautilus_trader.system.kernel.Portfolio` / `.LiveRiskEngine`);构造后调 `wire_arbitrage_runtime(node, params=, venue_liveness=)`(#108:原 `leg_settled=` 参数退役)
- 期望:
  - `node.kernel.portfolio` 实例类型 = `ArbitragePortfolio`,`node.kernel.risk_engine` = `ArbitrageLiveRiskEngine`(kernel 原生构造,**非构造后 swap**)
  - 三个 msgbus endpoint(`Portfolio.update_account` / `update_order` / `update_position`)+ RiskEngine 的 `RiskEngine.execute`/`process` + `events.order/position.*` 订阅均由各自 `__init__` 原生注册(无摘除/重注册)
  - `configure_arb` 注入 `ArbitrageParams.share`(portfolio)与 profit gate params + `venue_liveness`(engine);share 是 Risk 绝对金额阈值基数(#108:portfolio 不再注入 `leg_settled`;2026-06-30:fx 只在 OE/SE adapter 边界使用)
- 验收:
  - **已验证(冒烟)**:`install_arbitrage_engines()` 后 `kernel.Portfolio is ArbitragePortfolio`、`kernel.LiveRiskEngine is ArbitrageLiveRiskEngine`;子类关系成立
  - **待 .py**:全节点启动后 endpoint handler 指向正确实例;原 Portfolio API(`unrealized_pnl` 等)行为不变;`wire_*` 在非套利节点上抛 RuntimeError(install 漏调的早失败)

### risk-6.9.2b: outcome_exposures 返回每个 outcome 的 net_profit/liability

- 前置: cache 中 PM token home `BUY 100 @ 0.4`,OE selection away `BACK 40 @ 2.5`;两者属于 `pair_id="match_1"`
- 输入: `portfolio.outcome_exposures("match_1")`
- 期望:
  - home: `net_profit=20`, `liability=40`
  - away: `net_profit=20`, `liability=40`
- 验收: Risk 只用该接口做 match_tp/match_sl 门控;三元盘有 draw 腿时返回 `home/draw/away`,空持仓返回 `{}`

### risk-6.9.2c: outcome_exposures 用 PairRegistry 注册腿补齐无持仓 outcome

- 前置: PairRegistry 注册 home/draw/away 三个 instrument,但当前持仓只有 home/away 两条腿
- 输入: `portfolio.outcome_exposures("match_1")`
- 期望: 返回 key 仍包含 `draw`,且 draw 的 `net_profit` 为其它腿全输时的负 liability
- 验收: 三元盘某 outcome 暂无持仓时,不会被 Risk 的“所有 outcome”判断漏掉

### risk-6.9.2d: outcome_shares 按 outcome 聚合当前持仓 share

- 前置:同一 outcome 同时有 PM/OE/SE 持仓,例如 home 有 PM 5 share + OE gross 6 share,away 有 SE gross 10 share。
- 输入:`portfolio.outcome_shares("match_1")`
- 期望:返回 `home=11, away=10`。
- 验收:Strategy share_limit action 用该接口计算每个 outcome 的剩余额度,而不是逐 leg 单独看。

### risk-6.9.13: LegSettledRegistry 共享对象语义(✅ 已验证;已失效)

> ⚠️ **失效**:`LegSettledRegistry` 退役;新增共享契约见 `VenueExecutionLiveness`。

> ⚠️ **失效**:`LegSettledRegistry` 已删除。当前共享状态见 `VenueExecutionLiveness`。

- 前置: 历史 `LegSettledRegistry`,execution 写、portfolio/risk/strategy 读。**腿键 = instrument_id**(一个 instrument = 一条腿,不需 方向→下标 映射)
- 输入/期望:
  - 新建 → `has_entry(p)` False、`any_unsettled(p)` False(entry 不存在不触发 gate)
  - `reset(p, ["A.PM","B.OE"])` → `any_unsettled(p)` True(全 false)
  - `mark(p,"A.PM")` + `mark(p,"B.OE")` → `any_unsettled(p)` False 且 `all_settled(p)` True
  - `mark` 命中不存在的 entry / 不在本轮腿集合的 instrument → 忽略(非 execution 触发不创建,未知腿不崩)
  - **`has_any_unsettled()`(#70 新增,全局)**:无 entry → False;`reset(p,...)` 后 → True;某 pair 全 mark 后若无其它未结算 entry → False;另一 pair `reset` → True(`test_has_any_unsettled_global`)
  - **`mark_venue(venue_value)`(#105 新增)**:某 venue 一次完整 order 真值 → 该 venue 所有 armed 腿置 true(腿键 `instrument_id.venue.value` 命中;缺席快照腿=已澄清没成功亦置;他 venue/已 true/无 `.venue` 的 str 键跳过)→ 返回新置位数。OE `_on_current_bets` 调它(只挂 order 真值,position 解耦;execution §4.3bis(5))(`test_mark_venue_marks_all_that_venue_legs` / `_ignores_string_keys_without_venue`)
- 验收:历史用例已删除,不再作为当前验收。

### risk-6.9.14: ArbitragePortfolio 不读取执行健康状态(2026-06-15 / 2026-06-22 更新)

- 前置: cache 中存在可计算的 PM/OE/SE positions;`VenueExecutionLiveness` 中某 venue not alive。
- 输入: 调 `portfolio.outcome_exposures(pair_id)` / `portfolio.outcome_shares(pair_id)`。
- 期望: Portfolio 按 positions 正常计算;不因 liveness 返回空或 `None`。
- 验收:
  - `ArbitragePortfolio.configure_arb` 不接受 `leg_settled`。
  - `portfolio.py` 不 import `LegSettledRegistry` / `VenueExecutionLiveness`。
  - 执行健康 fail-closed 只在 `ArbitrageLiveRiskEngine._check_required_venues_alive`。

### risk-6.9.15: VenueExecutionLiveness gate(2026-06-15)

- 前置:order 带 `arb:expected_legs=pm:home:0,oe:away:1`;共享 `VenueExecutionLiveness` 中 PM alive、OE not alive。
- 输入:Risk 检查 PM leg。
- 期望:Risk 通过 `common.venues.venue_id_from_leg_key` 从 `expected_legs` 推导 required venues `{POLYMARKET, ORBITEXCH}`,因 OE not alive 拒绝整次机会;`sharpexch:...` / `se:...` 同样解析为 `SHARPEXCH`。
- 验收:
  - `tests/arbitrage/common/test_venue_liveness.py`
  - `tests/arbitrage/common/test_venues.py::test_venue_id_from_leg_key_accepts_full_names_and_legacy_aliases`
  - `tests/arbitrage/risk/test_engine.py::test_liveness_gate_checks_all_expected_leg_venues`
  - `tests/arbitrage/risk/test_engine.py::test_liveness_gate_passes_when_required_venues_alive`

### risk-pmsports-anchor.1:PMSPORTS 不进入 required venues(已落地,#127/#191)

- 前置:PairRegistry / MatchedPair 后续区分 anchor ids 与 tradable ids;某 pair 含
  `5843495.PMSPORTS` anchor id 和 PM/OE/SE tradable ids。
- 输入:Strategy 生成普通套利 SubmitOrder,`arb:expected_legs` 只来自 tradable legs。
- 期望:Risk `_check_required_venues_alive` 推导 required venues 时不包含 `PMSPORTS`;若 tag 污染带入 `pmsports:*`,Risk fail-closed。
- 验收:
  - `VenueExecutionLiveness` 初始化不包含 `PMSPORTS`。
  - `.PMSPORTS` 不触发余额、概率、profit gate 或 liveness gate。
  - 若误把 `pmsports:*` 写进 `expected_legs`,Risk 明确拒绝 unsupported non-tradable venue,不能当交易 venue 放行;验收 `test_liveness_gate_denies_unparseable_expected_leg_key`。

---

## 没有的用例

- ~~周期轮询余额~~ —— 删除(归 ExecutionClient PM 主动)
- ~~阈值告警 → publish BalanceAlert~~ —— 删除(用户看前端自己判断)
- ~~BalanceMonitorActor~~ —— 不引入此 Actor

如未来需要系统层告警(熔断 / Slack 推送等),按时再加,不超前实现(P7)。

## Debug 相关

`DebugArbitrageLiveRiskEngine` 子类的测试归于 `tests/arbitrage/debug/`(§6.6),特别是:
- `skip_check_size` 跳过 NT 父类的最小限额检查(测试小单可下)

不在本目录。

## 控制台命令 consumer(#119)
- `command.arb.trading_state` / `command.arb.risk_params`:`ArbitrageLiveRiskEngine.configure_arb` 内 subscribe → `set_trading_state` / 热改 `_arb_params`。用例 `test_engine.py::test_trading_state_command_halts_and_resumes` / `test_invalid_trading_state_command_ignored` / `test_risk_params_command_hot_updates_only_given_fields`。契约 + 完整控制台用例见 web §8 / `tests/arbitrage/web/README.md` web-7.8~7.12。

## #228:概率门控 lay 单补集概率(2026-07-15)

- `test_engine.py::test_probability_gate_lay_order_uses_complement_probability`:decimal venue 的 SELL(lay)单隐含概率 = 1−1/price(判别子 = order.side,非 instrument claim——no 腿执行已重定向回真 selection);lay@1.02 / lay@40 被拒,lay@2.0 通过。
- profit gate 的 claim/side 感知持仓归属已按 #230 落地(risk §4.1 / venues §4.1),覆盖:
  - PM NO token LONG 归 `no`;OE/SE 真 selection SHORT(LAY)也归 `no`,但使用 lay 的 profit/loss。
  - `[yes,no]` 混合持仓的 `outcome_exposures` 同时返回两侧正确 `net_profit/liability`。
  - `outcome_shares` / `outcome_shares_for_venue` 将 LAY gross share=`qty*odds` 计入 complement outcome,使 share limit 同步正确。
  - 2-way 外部 SHORT 映射到另一个 role;既有 LONG/BACK 和 probability LONG 用例不回归。

## #234:PM BUY-only 1 USD 门控

- `test_pm_buy_below_minimum_notional_is_denied`:BUY 5 @ 0.10 的 0.50 USD 订单被拒。
- `test_pm_buy_at_minimum_notional_passes`:BUY 5 @ 0.20 恰好 1 USD 放行。
- `test_pm_sell_does_not_apply_buy_notional_minimum`:SELL 5 @ 0.10 不应用 BUY 金额下限；最小 5 shares 仍由 NT `min_quantity` 处理。
- `test_pm_buy_minimum_notional_denies_on_real_submit_path`:真实 `SubmitOrder → RiskEngine` 派发产生 deny，且订单不泄漏到 ExecutionEngine。
