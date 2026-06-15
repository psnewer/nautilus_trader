# risk 测试

对应章节: `refactor.md §5.6, §6.9, 修订记录 #23`;详细设计 `architectures/risk/architecture.md`

**Step 6 落地状态(2026-05-22)**:`src/arbitrage/risk/{engine,portfolio,config}.py` + `common/leg_settled.py` + `bootstrap.py` 已实现,**pytest 全绿(29 passed)**:`test_{leg_settled,portfolio,engine,bootstrap}.py`(构造器在 `_factories.py`)。覆盖:cpdef `_check_order` 覆盖经 `_handle_submit_order` 派发 + 自 emit deny + 不泄漏(risk-6.7.1)、三门限 + settled fail-closed(6.7.2/3/4/8)、余额 venue 非对称(6.3b)、way_rebate 公式与 settled gate(6.9.x)、导入名替换 + wire(6.9.1)、LegSettledRegistry 语义(6.9.13)。

> ⚠️ **2026-06-15 设计变更**:上述 `leg_settled` / settled gate 用例是历史状态。新设计为 `VenueExecutionLiveness`:Portfolio 不再读执行健康;Risk 从 opportunity `expected_legs` 推导 required venues,用 `order_alive && position_alive` 做 fail-closed 门控。旧 settled 用例在代码迁移时应删除或改写为新 liveness 用例。

**写测试时抓出两个 production bug(已修)**:① `ArbitragePortfolio` 读不到基类私有 cdef `self._cache`(`Portfolio._cache` 非 readonly)→ 覆盖 `__init__` 自存 `_arb_cache`;② `order.has_price_c()` 是 cdef 不可从 Python 调 → 改 `order.has_price`(property)。两者运行期才暴露,纸面 review 抓不到——印证落地测试价值。

**#34(2026-05-24)pair_id 来源校准**:`_resolve_pair_id` / `_pair_id_for_order` 原读 `info["competition"]` 是错读(`competition` 是联赛名,EPL/NFL...,非 pair_id)。现改读 matching 的 `PairRegistry`(`configure_arb(pair_registry=...)`);测试用例同步加 `PairRegistry.register(pair_id, [instrument_ids])`,见 `test_engine._gate_ctx` / `test_portfolio.test_resolve_pair_id_reads_from_pair_registry`。`_leg_from_position` 同步加 `selection_role`(Q9 标准 key)兼容 `market_type` fallback。

**仍待落 .py / 延后**(需全节点 / discovery / execution 接线):risk-6.1/6.2(全管道透明拦截 + NT min_quantity 自动拒,需起节点)、risk-6.3/6.4(cache 真实持仓 + venue stale 兜底)、risk-6.5/6.6(账户状态维护,属 execution)、risk-6.7.5/6/9 与 6.9.{2全路径,3,5,7,8,10,11}(需 cache 真实 Position 而非 duck/stub)。

## 锁定的关键性约束(2026-05-09 三次修正后)

Risk 层在 NT `submit_order` 管道上**透明拦截**,Strategy **不引用** Risk。账户状态由 ExecutionClient 维护(PM 主动 / OE 被动 WS),Risk 层只读 Cache。**没有独立的 BalanceMonitorActor**(告警让前端自己看,见 §5.7)。

```
ExecutionClient (维护账户)
├── PM: 主动 timer 拉 → generate_account_state → Cache
└── OE: 被动 WS 帧 → generate_account_state → Cache
                ↓
        ArbitrageLiveRiskEngine._check_order  ← 同步读 Cache 做余额检查
                ↓
        WebGatewayActor (§5.7) 订阅 AccountState → 推前端 JSON
                                                    (用户看着判断,无系统层告警 Actor)
```

**唯一组件**: **`ArbitrageLiveRiskEngine`**(NT `LiveRiskEngine` 子类 —— 实盘 kernel 用 Live 版,非基类 `RiskEngine`)
- NT 父类自动处理最小限额(PM:`instrument.min_quantity=5 shares`;OE:`instrument.min_notional=7 GBP stake`)
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
  - OE:提交一个 `stake/notional < instrument.min_notional(7 GBP)` 的订单
- 期望: NT `RiskEngine` 父类自动拒绝,`Strategy.on_order_denied` 触发
- 验收: 应用层无需任何 `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 代码;Provider 元数据由 `tests/arbitrage/adapters/polymarket/test_parsing_min_size.py::test_parse_polymarket_instrument_sets_min_quantity_from_order_min_size` / `tests/arbitrage/discovery/test_orbitexch_provider.py::test_build_legs_sets_orbitexch_min_stake` 锁定,全管道拒单仍待节点级 risk-6.2 集成测

### risk-6.3: 应用层余额检查(自算可用余额,扣在途挂单)
- 前置: ExecutionClient 已写入 cache.account_state
- 输入: 提交一个超出**可用余额**的订单
- 期望: `ArbitrageLiveRiskEngine._check_balance` 拒绝,`Strategy.on_order_denied` 触发
- 验收: 检查依据 = `balance_total − Σ(cache.orders_open 在途名义额)`,**不直接信 `account.balance_free()`**(Q17,2026-05-19)

### risk-6.3b: 可用余额按 venue 非对称(Q17,2026-05-19)
- **PM —— 自扣在途挂单(free=total 陷阱)**
  - 前置: PM `total=100`,一笔未成交挂单占用 `60`;cache 上报 `reported=True/locked=0/free=100`(`CashAccount.apply` 已清空 NT 自算 locked)
  - 输入: 再提交名义额 `50` 的开仓单
  - 期望: `_check_balance` 算可用 = `100 − 60 = 40 < 50` → **拒绝**(误读 `free=100` 则误放行)
  - 验收: PM 可用 = `total − Σ(PM cache.orders_open 在途名义额)`,**不依赖 `balance_free()`**
- **OE —— 直接信 WS 余额(已含占用,不再减)**
  - 前置: OE WS 上报余额 `40`(**已扣**挂单占用);该值即可用
  - 输入: 提交名义额 `50` 的单
  - 期望: `_check_balance` 用 `40 < 50` → 拒绝;**不再额外扣挂单**(否则双重扣减低估)
  - 验收: OE 可用 = cache 余额直接用;`_check_balance` 内按 `order.instrument_id.venue` 分支,PM/OE 不同处理

### risk-6.4: cache stale 时由 venue 拒绝兜底
- 前置: cache 余额过期,venue 真实余额已不够
- 输入: 提交订单(过 RiskEngine 检查 → 路由到 venue)
- 期望: venue 返回 INSUFFICIENT_BALANCE → `generate_order_rejected` → `Strategy.on_order_rejected`
- 验收: 这是**异常路径**,不是设计层"双兜底",Strategy 应能处理

### risk-6.5: PM ExecutionClient 事件驱动维护账户状态
- 前置: PolymarketExecutionClient 启动
- 输入: 触发任一上游事件(连接时 / 链上成交确认 `POLYMARKET_FINALIZED_TRADE_STATUSES`)
- 期望: cache.account_state(POLYMARKET) 自动更新
- 验收: 路径完全在 ExecutionClient 内,无独立监控 Actor;**上游无周期 timer、NT 无默认 QueryAccount 轮询、健康检查也不拉余额**(Q17,完全靠事件)

### risk-6.6: OE ExecutionClient 被动维护账户状态(WS)
- 前置: OrbitExchExecutionClient 启动,WS 已连接
- 输入: 模拟 OE WS 推送一条余额变化帧
- 期望: ExecutionClient 解析后调 `generate_account_state` 写 Cache
- 验收: 被动路径,无 timer,完全反应式

---

## 组合级硬停门限: tp / sl / global_sl(Q16,§5.6 `_check_rebate_gates`)

三门限平移自旧 `services/risk/service.py:check_risk`,全在 `ArbitrageLiveRiskEngine._check_rebate_gates` 内,逐 submit deny = 别开新仓。**无 TradingState 翻闸、无监测 Actor、无频率**。Venue liveness 是另一道 Risk gate,位于 NT 父类检查之后、余额/rebate gates 之前。

### risk-6.7.1: `_check_order` 签名与父类一致 + super 先行(✅ 已 e2e 验证)
- 前置: `ArbitrageLiveRiskEngine` 已装入管道
- 输入: 提交一笔正常订单
- 期望: `_check_order(self, instrument, order)` 两参签名;先调 `super()._check_order(instrument, order)`(NT 仅 price/quantity/GTD),再 `_check_balance`,再 `_check_rebate_gates`。**notional/submit_rate/native 余额不在 `_check_order`,在父类 `_check_orders_risk_for_account`(本类不覆盖,管道上随后原样跑)**
- 验收: 任一返回 False 即 `OrderDenied`;签名与 `engine.pyx:571` 一致,**override 被 Cython `_handle_submit_order` 派发到(已用真实 SubmitOrder 跑通:覆盖触发 1 次 + deny 事件发出 + 订单不泄漏到 exec)**。⚠️ 自定义 deny 必须自调 `self._deny_order(order, reason)`,否则订单静默丢弃、`on_order_denied` 不触发

### risk-6.7.1b: VenueExecutionLiveness gate 顺序与 fail-closed(2026-06-15)
- 前置: `ArbitrageLiveRiskEngine` 注入 `VenueExecutionLiveness`;某 opportunity 的 `expected_legs=("pm:home:0","oe:away:1")`;PM `order_alive=true/position_alive=true`,OE `order_alive=false/position_alive=true`。
- 输入: PM leg 或 OE leg 任一 SubmitOrder 进入 `_check_order`。
- 期望: `super()._check_order` 通过后,`_check_required_venues_alive` 发现 required venues 中 OE 不 alive → `_deny_order`。
- 验收:
  - `_check_balance` 和 `_check_rebate_gates` 不再继续执行。
  - 原生 `OrderDenied` 与 `risk.opportunity.leg_denied` 都发布。
  - 两条腿无论当前 order 自己 venue 是 PM 还是 OE,结果一致 deny。

### risk-6.7.1c: required venues 从 expected_legs 推导,不是只看当前 venue
- 前置: `expected_legs=("pm:home:0","oe:away:1")`,当前订单是 PM leg;PM alive,OE not alive。
- 输入: PM leg SubmitOrder。
- 期望: 仍 deny,因为 required venues 包含 OE。
- 验收: Risk 使用 `expected_legs` partner 信息解析 required venues;不新增 `required_venues` tag 也能工作。

### risk-6.7.1d: 无 opportunity metadata 时退化为当前 venue liveness
- 前置: 普通非套利订单无 `arb:opportunity_id` / `arb:expected_legs`;当前 `order.instrument_id.venue=POLYMARKET`;PM not alive,OE alive。
- 输入: 普通 PM SubmitOrder。
- 期望: Risk 只检查 PM liveness 并 deny。
- 验收: 非套利订单不被 partner 协议污染。

### risk-6.7.2: match_tp 触发 deny(止盈,赚够别加仓)
- 前置: pair_id="match_X" 持仓,所有方向 `way_rebate ≥ config.match_tp`;`leg_settled` 全 true
- 输入: strategy 对该 pair 再 `submit_order`
- 期望: `_check_rebate_gates` 判 match_tp → `_check_order` 返 False → `Strategy.on_order_denied`
- 验收: 触线一律 deny;不平仓、不撤单、无其它动作

### risk-6.7.3: match_sl 触发 deny(止损,该场恶化别加仓)
- 前置: `min_way_rebate("match_X") < config.get_match_sl("match_X")`
- 输入: 对该 pair 再 submit
- 期望: deny → `on_order_denied`
- 验收: 与旧 `match_blocked` 行为等价

### risk-6.7.4: global_sl 触发 deny(全局累计止损 / 循环熔断)
- 前置: `portfolio.global_min_rebate_sum() < config.global_sl`
- 输入: 对**任意** pair submit
- 期望: deny → `on_order_denied`
- 验收: 全局熔断 = 逐 submit deny;**断言无 `set_trading_state` 调用、无独立熔断 Actor**(静态搜索 + 运行时 TradingState 始终 ACTIVE)

### risk-6.7.5: 撤单不受三门限影响(只挡开仓)
- 前置: 任一门限已触发(如 global_sl 跌破)
- 输入: 提交一笔 `CancelOrder`(如补偿撤单)
- 期望: 撤单正常路由到 ExecutionClient,**不被 `_check_rebate_gates` 拦**
- 验收: deny 只作用于 `SubmitOrder` 通路;`bug_compensating_cancel_missing` 的补偿撤单照常发出

### risk-6.7.6: recovery intent 跳过 rebate gates,但不跳余额
- 前置: Strategy submitter 给补救单写 `Order.tags=["arb:intent=recovery"]`;`global_min_rebate_sum()` 返回 `None` 或 `match_sl` 已触发
- 输入: recovery submit
- 期望: `_check_order` 仍先跑 NT 父类基础检查和 `_check_balance`;余额通过时跳过 `_check_rebate_gates` 并路由到 ExecutionClient
- 验收: 真实 `SubmitOrder` 管道下,recovery intent 在 settled/global fail-closed 场景仍能到 exec;余额不足时仍 `OrderDenied`,不泄漏到 exec

### risk-6.7.6b: recovery intent 不跳过 VenueExecutionLiveness
- 前置: recovery SubmitOrder 带 `arb:intent=recovery`;required venues 中 PM `position_alive=false`。
- 输入: recovery submit。
- 期望: `_check_required_venues_alive` deny;不进入 `_check_balance` / `_check_rebate_gates`。
- 验收: recovery 只跳过 rebate gates,不跳过 venue liveness;撤单仍因不经 `_check_order` 而不受影响。

### risk-6.7.6: 机会评估与硬停正交(strategy 通过但 risk 仍拦)
- 前置: 某机会 `min_way_rebate ≥ strategy.min_rebate_rate`(strategy 认为值得做)但同时所有方向 `≥ match_tp`
- 输入: strategy 评估通过 → submit
- 期望: strategy 不自拦(机会评估正向门槛过),`ArbitrageLiveRiskEngine` 在管道上 deny(tp 硬停)
- 验收: 两层正交;strategy 不引用 risk,deny 经 `on_order_denied` 回传

### risk-6.7.7: settled entry 不存在 → 放行(Q-G1,2026-05-19,已失效)

> ⚠️ **失效**:`leg_settled` gate 退役;用 risk-6.7.1b~1d 覆盖新 liveness 行为。
- 前置: pair_id="match_X" 从没下过单 → `leg_settled` 无此 entry
- 输入: 对 match_X submit,`_check_rebate_gates` 检查 settled
- 期望: entry 不存在 = 无结算风险 → **本 pair 的 tp/sl 门限放行**(way_rebate 本就 `{}`,tp/sl 无从触发)
- 验收: absent ≠ false;absent 一律放行,不与 fail-closed 混淆

### risk-6.7.8: global_min_rebate_sum 返回 None → fail-closed deny(2026-05-19,已失效)

> ⚠️ **失效**:`global_min_rebate_sum` 不再承载执行健康 fail-closed;venue 执行健康由 `_check_required_venues_alive` 拦截。
- 前置: **别的某个 pair** 有腿 `leg_settled=false` → `portfolio.global_min_rebate_sum()` 返回 `None`
- 输入: 对**任意** pair(含 settled 干净的 pair)submit
- 期望: `_check_rebate_gates` 读到 global `None` → **deny(拦截新开仓)**;全局图景不全时一律挡
- 验收:
  - 与 fail-open 相反:数据不全时挡而非放
  - 不死锁:撤单走另一通路照常(risk-6.7.5),健康检查 reconcile 结算后 global 恢复实数 → 后续 submit 自动放开
  - 实现不得 NoneType 崩:None 必须显式判定为 deny,不进入数值比较

### risk-6.7.9: rebate 数据源 = ArbitragePortfolio 引用(非 cache 缓存)
- 前置: cache 持仓刚因 fill 更新
- 输入: `_check_rebate_gates` 取 rebate
- 期望: 持 `ArbitragePortfolio` 引用调 `way_rebate / min_way_rebate / global_min_rebate_sum`,即时现算反映最新持仓
- 验收: **cache 不存 rebate**;`_check_rebate_gates` 不读任何"已存 rebate"字段,不重复算法(算法只在 ArbitragePortfolio 一份)

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

---

## ArbitragePortfolio: way_rebate 等领域指标(Q14,§6.9)

子类化 `Portfolio` 加 4 个 Python 方法,与 NT `unrealized_pnl` 并列扩展。算法来源于 `services/risk/position.py`,分母口径已按 NT `mean_rebate` 下单语义校准为最大实际腿 share。

### risk-6.9.1: 导入名替换 → kernel 原生构造 ArbitragePortfolio + ArbitrageLiveRiskEngine(✅ 部分已验证)

- 前置: 构造 `TradingNode` **之前**调 `bootstrap.install_arbitrage_engines()`(替换 `nautilus_trader.system.kernel.Portfolio` / `.LiveRiskEngine`);构造后调 `wire_arbitrage_runtime(node, params=, leg_settled=)`
- 期望:
  - `node.kernel.portfolio` 实例类型 = `ArbitragePortfolio`,`node.kernel.risk_engine` = `ArbitrageLiveRiskEngine`(kernel 原生构造,**非构造后 swap**)
  - 三个 msgbus endpoint(`Portfolio.update_account` / `update_order` / `update_position`)+ RiskEngine 的 `RiskEngine.execute`/`process` + `events.order/position.*` 订阅均由各自 `__init__` 原生注册(无摘除/重注册)
  - `configure_arb` 注入 fx/leg_settled(portfolio)与三门限 params(engine);share 保留兼容注入,不作 `way_rebate` 分母
- 验收:
  - **已验证(冒烟)**:`install_arbitrage_engines()` 后 `kernel.Portfolio is ArbitragePortfolio`、`kernel.LiveRiskEngine is ArbitrageLiveRiskEngine`;子类关系成立
  - **待 .py**:全节点启动后 endpoint handler 指向正确实例;原 Portfolio API(`unrealized_pnl` 等)行为不变;`wire_*` 在非套利节点上抛 RuntimeError(install 漏调的早失败)

### risk-6.9.2: way_rebate 算法使用最大实际腿 share 归一化

- 前置: cache 中 PM token A `BUY 100 @ 0.4`,OE selection X `BACK 50 @ 2.5`;两者属于 `pair_id="match_1"`;PM 实际 share=100,OE 实际 share=`50*2.5=125`
- 输入: `portfolio.way_rebate("match_1")`
- 期望: `{"home": 0.08, "away": 0.28}`(net payoff 分别为 10/35,统一除以最大腿 share=125)
- 验收: 不使用配置里的固定 share 做分母;`mean_rebate` 正常下单产生的等 share 腿仍按共同 share 归一化

### risk-6.9.3: way_rebates_by_venue 按 venue 拆分

- 前置: 同 6.9.2
- 输入: `portfolio.way_rebates_by_venue("match_1")`
- 期望: `{POLYMARKET: {...}, ORBITEXCH: {...}}`,每个 venue 单独算
- 验收: 与 `MatchPosition.calculate_way_rebate_by_venue()` 输出一致

### risk-6.9.4: min_way_rebate

- 前置: 同 6.9.2
- 输入: `portfolio.min_way_rebate("match_1")`
- 期望: `0.10`(home/away 中较小者)
- 验收: 等价于 `min(way_rebate.values())`

### risk-6.9.5: global_min_rebate_sum

- 前置: cache 中两场比赛都有持仓,min_way_rebate 分别 0.10 / 0.05
- 输入: `portfolio.global_min_rebate_sum()`
- 期望: `0.15`
- 验收: 等价于现 `PositionManager.get_global_min_rebate_sum()`;**只遍历有 open position 的 pair**(对应旧 `position.py:382` 遍历 `self._positions.values()`)

### risk-6.9.5b: 未交易比赛不进遍历,不触发 None

- 前置: cache 中 match_X 有持仓且 settled 全 true(min_rebate=0.10);同时存在一场 match_Z **从没下过单 / 无持仓**(仅日程表/instrument 存在)
- 输入: `portfolio.global_min_rebate_sum()`
- 期望: 返回 `0.10`(只计 match_X);**match_Z 不进遍历、不致 None**
- 验收: 扫描范围 = active pair(有持仓);"无持仓比赛 → None" 是**错误**实现(否则日程表有未交易比赛就恒 None,系统焊死)。None 仅来自"有持仓 pair 的腿 false"(见 risk-6.9.12)

### risk-6.9.6: pair_id 解析(instrument.info["competition"])

- 前置: 一笔持仓的 instrument 缺 `info["competition"]` 字段
- 输入: `portfolio.way_rebate(pair_id)` 内部解析时遇到该 instrument
- 期望: 该 leg 被跳过,不进入聚合;不抛异常
- 验收: 防御性,缺字段 instrument 不污染聚合结果

### risk-6.9.7: 空 pair / 无持仓

- 前置: 一个未持仓的 `pair_id`
- 输入: `portfolio.way_rebate("unknown_pair")`
- 期望: 返回 `{}`(不返回 None / 不抛异常)
- 验收: 与 NT `unrealized_pnls(...)` 空账户时返回 `{}` 一致

### risk-6.9.8: 不订阅事件,纯 pull(无独立触发器)

- 前置: cache 中持仓刚因 fill 更新过
- 输入: 立即调 `portfolio.way_rebate(pair_id)`
- 期望: 算出的结果反映最新 fill;无中间缓存层延迟
- 验收: 静态搜索 ArbitragePortfolio 代码无 `msgbus.subscribe` 调用;纯函数式

---

### risk-6.9.9: settled gate — entry 不存在通过(Q-G1,已失效)

> ⚠️ **失效**:`ArbitragePortfolio` 不再读取 `LegSettledRegistry`;本用例迁移时删除。

- 前置: pair_id="match_X" 从未发起过 execution → `leg_settled` entry 不存在;但 cache 中有该 pair 的非 execution 触发持仓(如历史导入)
- 输入: `portfolio.way_rebate("match_X")`
- 期望: 正常计算返回 dict(无 execution-staleness 风险时不阻塞)
- 验收: gate 失败时 entry 必须存在;不存在 entry 不触发 gate

### risk-6.9.10: settled gate — entry 全 true 通过(已失效)

> ⚠️ **失效**:`ArbitragePortfolio` 不再读取 `LegSettledRegistry`;本用例迁移时删除。

- 前置: `leg_settled["match_X"] = [true, true]`
- 输入: `portfolio.way_rebate("match_X")` / `min_way_rebate("match_X")` / `way_rebates_by_venue("match_X")`
- 期望: 三个方法都正常返回结果
- 验收: 与现有 risk-6.9.{2,3,4} 行为一致

### risk-6.9.11: settled gate — entry 任一 false 阻塞(Q-G2,已失效)

> ⚠️ **失效**:`ArbitragePortfolio` 不再读取 `LegSettledRegistry`;执行健康阻塞由 Risk `VenueExecutionLiveness` gate 承担。

- 前置: `leg_settled["match_X"] = [true, false]`(away 方向 settled=false)
- 输入:
  - `portfolio.way_rebate("match_X")`
  - `portfolio.min_way_rebate("match_X")`
  - `portfolio.way_rebates_by_venue("match_X")`
- 期望:
  - `way_rebate` 返回 `{}`
  - `min_way_rebate` 返回 `None`
  - `way_rebates_by_venue` 返回 `{}`
- 验收:
  - 即使 cache 中有持仓数据,只要 gate 失败就阻塞计算
  - 不抛异常,不返回 sentinel,只返回空值让调用方自然处理

### risk-6.9.12: global_min_rebate_sum fail-closed(Q-G3,已失效)

> ⚠️ **失效**:`global_min_rebate_sum` 不再因执行健康返回 `None`;本用例迁移时删除或改为纯持仓聚合测试。

- 前置: 多场比赛,X / Y 都有持仓;`leg_settled["match_X"] = [true, true]`,`leg_settled["match_Y"] = [false, true]`
- 输入: `portfolio.global_min_rebate_sum()`
- 期望: 返回 `None`(整个全局判断作废,**不返回 X 的部分和**)
- 验收: fail-closed —— 任何 pair 任何方向 false 就让全局判断作废返回 `None`;**消费方 `_check_rebate_gates` 读到 `None` → deny(拦截新开仓)**(2026-05-19 锁定,见 risk-6.7.8)。区分两层:`global_min_rebate_sum` 方法返回 `None`(数据语义),熔断门限把 `None` 解释为"挡新单"(消费语义)

### risk-6.9.13: LegSettledRegistry 共享对象语义(✅ 已验证;已失效)

> ⚠️ **失效**:`LegSettledRegistry` 退役;新增共享契约见 `VenueExecutionLiveness`。

- 前置: `LegSettledRegistry`(`src/arbitrage/common/leg_settled.py`),execution 写、portfolio/risk/strategy 读。**腿键 = instrument_id**(一个 instrument = 一条腿,不需 方向→下标 映射)
- 输入/期望:
  - 新建 → `has_entry(p)` False、`any_unsettled(p)` False(entry 不存在不触发 gate)
  - `reset(p, ["A.PM","B.OE"])` → `any_unsettled(p)` True(全 false)
  - `mark(p,"A.PM")` + `mark(p,"B.OE")` → `any_unsettled(p)` False 且 `all_settled(p)` True
  - `mark` 命中不存在的 entry / 不在本轮腿集合的 instrument → 忽略(非 execution 触发不创建,未知腿不崩)
  - **`has_any_unsettled()`(#70 新增,全局)**:无 entry → False;`reset(p,...)` 后 → True;某 pair 全 mark 后若无其它未结算 entry → False;另一 pair `reset` → True(`test_has_any_unsettled_global`)
  - **`mark_venue(venue_value)`(#105 新增)**:某 venue 一次完整 order 真值 → 该 venue 所有 armed 腿置 true(腿键 `instrument_id.venue.value` 命中;缺席快照腿=已澄清没成功亦置;他 venue/已 true/无 `.venue` 的 str 键跳过)→ 返回新置位数。OE `_on_current_bets` 调它(只挂 order 真值,position 解耦;execution §4.3bis(5))(`test_mark_venue_marks_all_that_venue_legs` / `_ignores_string_keys_without_venue`)
- 验收: ArbitragePortfolio 的 settled gate 经 `any_unsettled` 读此对象;**OE 健康检查状态维度(#70,execution §4.3 Phase 2)经 `has_any_unsettled()` 读**;registry 为空(execution 未启动)时 gate 不误触发,优雅降级。**已 pytest 验证上述全部语义**

### risk-6.9.14: ArbitragePortfolio 不读取执行健康状态(2026-06-15)

- 前置: cache 中存在可计算的 PM/OE positions;`VenueExecutionLiveness` 中某 venue not alive。
- 输入: 调 `portfolio.way_rebate(pair_id)` / `portfolio.global_min_rebate_sum()`。
- 期望: Portfolio 按 positions 正常计算;不因 liveness 返回 `{}` 或 `None`。
- 验收:
  - `ArbitragePortfolio.configure_arb` 不接受 `leg_settled`。
  - `portfolio.py` 不 import `LegSettledRegistry` / `VenueExecutionLiveness`。
  - 执行健康 fail-closed 只在 `ArbitrageLiveRiskEngine._check_required_venues_alive`。

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
