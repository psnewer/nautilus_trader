# strategy 测试(占位)

待 Step 4 启动时展开。

对应章节: `refactor.md §5.4`

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

## Strategy hook 点契约(P10 设计原则)

为支持 §6.6 Debug 子类化覆盖,生产 Strategy 必须把可变行为拆成 protected hook:

| Hook | 默认实现 | Debug 覆盖意图 |
|---|---|---|
| `_get_min_rebate_rate` | `config.min_rebate_rate` | `overrides.min_rebate_rate` |
| `_get_pm_price(ctx, dir)` | `cache.order_book(...)` 读最优价 | `overrides.polymarket_price` 强制覆盖 |
| `_get_oe_price(ctx, dir)` | 同上 | `overrides.orbitexch_price` |
| `_get_pm_size(share, dir)` | `share`(转换成 PM 标准 size) | `overrides.polymarket_size` |
| `_get_oe_size(share, dir)` | `share / odds / fx`(OE stake) | `overrides.orbitexch_size` |
| `_adjust_share_by_liquidity(ctx, best)` | 读 orderbook 深度按比例缩放 | (Debug 不覆盖此 hook;skip_check_size 在 Risk 层) |

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

## Debug 相关

`DebugArbitrageStrategy` 子类的测试归于 `tests/arbitrage/debug/`(§6.6),覆盖:
- `min_rebate_rate` / `polymarket_price` / `orbitexch_price` / `polymarket_size` / `orbitexch_size` 等 override hook

不在本目录。
