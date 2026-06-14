# 横切:组件间同步(健康检查 ⊥ 执行)详细设计

> **定位**:详细设计。理由/历史见初设 `refactor.md §6.10`(Q19)。
> 冲突时:**有把握 → 以本文为准并回写 `refactor.md` 修订记录;没把握 → 讨论**。
> **这是横切协议**(P11:无单一归属)—— 由 **4 个组件共同实现同一契约**:Strategy、OE 健康检查(`OrbitExchDataClient`)、PM 健康检查(PM ExecClient 子类)、execution session。任一实现者以本文为准。

> ⚠️ **失效指针(#105,2026-06-13,设计/待落地)**:§1–7 描述的是**自写 HealthCheckLoop 时代**的同步设计。**#105 决定把健康检查迁移到 NT 原生 reconciliation**,据此:
> - **`health_check.*` 消息 + Strategy `_hc_running` + strategy⊥健康检查互斥(§1 第二条、§2.1 `_hc_running`、§3 Strategy 行、§7.6)→ 退役**(由 OE ExecClient 页锁取代其存在理由);
> - **健康检查⊥执行 per-venue 互斥(§1 第一条、§2.1 `_execution_active`、§3 健康检查行)→ 退役**(NT 不串行化 reconciliation⊥execution,改由 OE 页锁在资源层串行);
> - **pair_inflight 兜底从 `health_check→clear_all`(§7.6)+ max-hold(§7.3 防泄漏 2)→ 改为 watchdog 主防线 + `try_enter` 检 execution-alive(§8)**。
> 读 §1–7 时务必先读 **§8**(迁移后的现状真理源);§1–7 保留为迁移前设计记录。`leg_settled`(§7 per-pair 闸的 `_begin_session` arm + venue-confirm mark)与 per-pair `PairInFlightGate` 机制本体**保留**,仅兜底触发改变。

---

## 1. 要解决的问题

健康检查(OE 页面 reload / PM report 拉取 + merge/redeem)与订单执行(submit + tracking)若并发会互相破坏:
- OE reload 冲掉执行页面;
- merge/redeem 改链上持仓时执行在飞;
- report reconcile 与 tracking 抢 `leg_settled`。

**锁定方案 = 互斥**(#89 修正为 **per-venue**,非全局):
- **健康检查 ⊥ 执行 = per-venue**:每 venue 的健康检查只在**该 venue** 有执行在飞时推迟 —— OE reload 只跟 OE 下单冲突,PM merge/redeem 只跟 PM 执行冲突,互不干涉。
- **Strategy ⊥ 健康检查 = 任一**:**任一** venue 健康检查在跑 → Strategy 放弃机会(strategy fire 跨 PM+OE 双腿,任一 venue 健检在跑都可能撞下单)。

---

## 2. 状态与消息

### 2.1 两个状态

| 状态 | 消费方 | 含义 | 维护 |
|---|---|---|---|
| `_hc_running`(健康检查在跑) | **Strategy** | **任一** venue 健康检查 tick 进行中 | strategy 订 `health_check.*`,维护**在跑 source 集合**(per-venue 信号位,**非 ref-count**;`started`→add / `finished`→discard;非空即活) |
| `_execution_active`(本 venue 执行在飞) | **每 venue 健康检查** | **本 venue** 一条执行 session 从 submit 到 terminal/timeout | **per-venue**(#89):PM 直接读 `self._execution_active`(自己的 session);OE DataClient 订 `execution.*` 但**按 msg instrument venue 过滤、只数 OE 自己的腿** |

> **per-venue(#89)**:`execution.*` 是全局 topic(PM/OE 共发),但消费方**各只数自己 venue 的腿** —— OE 健检数 OE 腿、PM 健检数 PM 腿,互不并入。Strategy 侧 `_hc_running` 则是「任一健检在跑」(per-venue 信号位集合,非空即活),因 strategy fire 跨双腿。

### 2.2 msgbus 消息(前后都发)

```mermaid
flowchart TB
  subgraph 健康检查["健康检查(OE DataClient / PM ExecClient)"]
    HCs["tick 开始(首个 await 前): publish health_check.started"]
    HCf["finally: publish health_check.finished"]
  end
  subgraph 执行["execution session"]
    EXs["submit 时: publish execution.started"]
    EXf["terminal/timeout(finally): publish execution.finished"]
  end
  HCs & HCf -->|订阅| STR["Strategy._hc_running 镜像(per-venue source 集合,任一非空即活)"]
  EXs & EXf -->|订阅(按 venue 过滤)| HCsub["每 venue 健康检查._execution_active(只数本 venue 腿,#89)"]
```

| 消息 topic | 发布者 | 订阅者 |
|---|---|---|
| `health_check.started` / `.finished` | OE 健康检查、PM 健康检查(各发各的) | Strategy(维护 `_health_check_active`) |
| `execution.started` / `.finished` | execution session | OE 健康检查、PM 健康检查(维护 `_execution_active`) |

---

## 3. 协议(各组件实现什么)

| 组件 | 实现 |
|---|---|
| **Strategy** | **执行同步前置**:决策点 pre-check `if _hc_running(任一 venue 健检在跑): 放弃机会, early return`(与 settled / per-pair pre-check 并列)。**健康检查期间根本不开新执行**。submit 时发 `execution.started`,session 到 terminal/timeout 发 `execution.finished`。**收 `health_check.finished` 且全不在跑 + leg_settled 全 true → `pair_inflight.clear_all()`**(§7.6) |
| **OE / PM 健康检查** | tick callback 开头 `if _execution_active(**本 venue**): 跳过本 tick`(`finally` 照常重排下次 alert);否则首个 await 前 publish `health_check.started`、`finally` publish `health_check.finished`。**OE 订 `execution.*` 按 venue 过滤只数 OE 腿(#89)**;PM 直接读自己 session |
| **execution session** | 见 execution 文档 §3.4(发 `execution.*`) |

```mermaid
sequenceDiagram
  participant ST as Strategy
  participant HC as 健康检查
  Note over ST,HC: 单 asyncio loop 串行 → 无需锁
  alt 健康检查先占
    HC->>HC: publish health_check.started (await 前同步)
    ST->>ST: pre-check 命中 _health_check_active → 放弃机会
    HC->>HC: finally: health_check.finished
    ST->>ST: 下一轮重评(若机会仍在则 submit)
  else 执行先占
    ST->>ST: publish execution.started
    HC->>HC: tick fire → 见 _execution_active → 跳过 + 重排
    ST->>ST: terminal/timeout: execution.finished (finally)
  end
```

---

## 4. 为什么单 loop 无需锁(P1,NT 原语)

NT `LiveClock` 回调、Actor handler、`msgbus` 派发**都在同一 asyncio event loop 串行**。只要"**检查 + 置位 + publish 在首个 `await` 之前同步完成**",就不存在交错——Strategy 的 submit 决策回调与健康检查回调不会真正并行;`msgbus.publish` 同步派发,`publish health_check.started` 返回时 Strategy 镜像已置位。

**实施纪律(硬约束)**:
- 置位必须在任何 `await` 之前同步做;
- 清位放 `finally`(terminal **和** timeout 两条路径都要清,否则 `_execution_active` 泄漏 → 健康检查永久饿死)。

---

## 5. 代价(已接受)

- **本 venue** 执行在飞时该 venue 健康检查暂停 → staleness 检测延迟 += 执行时长(上界 = execution tracking timeout)。
- 任一健康检查跑时 Strategy 放弃机会 → 下一轮(alert 重排后)重评。
- per-venue 粒度:OE/PM 健康检查互不阻塞(OE 不再因 PM 执行而多等,#89)。

**「≤1 执行」的来源(per-venue 后不再是本互斥的红利)**:"同时在飞的套利 ≤ 1" 现由 **Strategy 全局 `is_execution_active`(聚合所有 exec client)+ per-pair 闸(§7)** 保证,不再是 health-check⊥execution 互斥的连带红利(后者已 per-venue,PM/OE 各自独立)。Q20 机会快照并发数 ≤1 仍成立(靠 strategy 侧那道)。

---

## 6. 落地清单

- [ ] 定义 msgbus topic 常量:`health_check.started/finished`、`execution.started/finished`
- [ ] Strategy:订 `health_check.*` 维护 ref-count 镜像 + submit pre-check + 发 `execution.*`
- [ ] OE/PM 健康检查:订 `execution.*` 维护 ref-count 镜像 + tick 开头跳过判断 + 发 `health_check.*`
- [ ] 置位在 await 前、清位在 finally(代码审查硬检查)
- [ ] 对应测试:`strategy-4.{15,16}`、`pm-adapter-5.health.5`、`oe-adapter-2.health.5`

---

## 7. per-pair 机会串行闸(§6.10 的 per-pair 维度,#84)

### 7.1 为什么 §1-6 的全局互斥不够

§1-6 的 `_execution_active` / `execution.*` 是**全局**态(针对所有 pair),且**发布点在 `_begin_session`** —— 那是 NT **异步** `_submit_order` 任务的末端,在「OBD 回调 → 评估决策 → `submit_order` → RiskEngine(同步)」这条同步链的**下游**。后果:

- strategy 评估是 `create_task` **并发**派发(`actor.py:_route_eval`),`leg_settled`(`_begin_session` arm)/ `execution.started`(`_begin_session` publish)都在异步执行链才置;
- **同一 OBD 突发的多个评估,在任何在飞信号置位之前就已各自决策并下单** → 同一 pair 重复 fire(实测:一批 OBD → 4 笔 OE 单同毫秒,见 refactor.md #82 复盘);
- settled gate(Portfolio way_rebate / RiskEngine,§6.8.2)、cancel-only(§6.8.5)同样读这些下游信号 → 对同毫秒并发**全部漏过**(串行轮次有效,并发突发无效)。

**结论**:全局互斥 + settled gate 解决「健康检查 ⊥ 执行」「跨轮重入」,但**结构上拦不住同一 pair 同一瞬间的并发评估/fire**(信号永远在异步下游)。需要一道**同步**的 per-pair 闸。

### 7.2 不变量

- **per-pair:同一 pair 同时只有 1 笔套利在「评估 → 执行」生命周期内**(不同 pair 互不影响,可并发);
- 推论:per-pair 不同时评估多个机会、不同时执行多个机会。

### 7.3 机制 —— 同步 per-pair 闸(`PairInFlightGate`)

共享 registry(launcher 经 `ArbContext` 注入给 **StrategyEvaluator + execution session**,同 `leg_settled` 套路)。**所有置位/清位都在首个 `await` 前同步做**(复用 §4 单 loop 无锁纪律)。

| 时机 | 组件 | 动作 |
|---|---|---|
| OBD 回调 `_route_eval`(**同步**,`create_task` 之前) | Strategy | `try_enter(pair)`:已在飞 → **直接放弃**(`return`,不 create_task);否则置位 |
| `_evaluate_and_fire` 收尾(finally) | Strategy | **未 fire**(无机会/abort/异常)→ `release_eval(pair)`;**已 fire** → 不释放(所有权交执行) |
| `_begin_session`(per-leg,同步段) | execution | `exec_started(pair)`:per-pair execution session 计数 ++ |
| `_end_session`(terminal/timeout) | execution | `exec_finished(pair)`:计数 --;**归 0 → 释放 in-flight** |

**交接(strategy → execution)无空窗**:strategy fire 后**不释放**(in-flight 持续置位),异步 `_submit_order` 到 `_begin_session` 才 `exec_started` —— 这中间 in-flight 一直由 strategy 持有,并发 OBD 进来即被 `try_enter` 挡掉。执行全部结束(双腿 session 计数归 0)由 execution 释放。

**防泄漏(异常路径让 in-flight / exec_count 卡死,两层兜底)**:
泄漏场景:`_end_session` 在 `exec_finished` 前抛异常 / fire 了但一腿 session 都没起(全 deny / cancel-only 丢弃)/ 超时 alert 丢失 → `_inflight` 或 `_exec_count` 卡住,该 pair 之后被永久挡住。

1. **健康检查触发 `clear_all`(主,#85,见 §7.6)**:strategy 收到 `health_check.finished` 且**全部健康检查不在跑** 且 **`leg_settled` 全 true**(无腿「已发未确认」=确无 arb 在飞)→ `clear_all()`(清 `_inflight` **+ `_exec_count`**)。主动、且连脏计数一起清。
2. **`try_enter` 的 max-hold 陈旧自愈(辅)**:in-flight 带时间戳,超过 `max_hold`(`≥2×tracking_timeout`)即视为空闲可重入。被动、只清 `_inflight`(清不到 `_exec_count`),作健检也卡住时的最后防线。
- `release_eval` 只在 `exec_count==0` 时清(fire 后 exec_count>0 则是 no-op,交执行清)。

### 7.4 与既有机制的关系

- **正交于全局互斥(§1-6)**:全局闸管「执行 ⊥ 健康检查」「≤1 全局执行」;per-pair 闸管「同 pair 不并发评估/执行」。两者并存,前置 pre-check 并列(`_health_check_active` / 全局 `_execution_active` / **per-pair in-flight** / settled / RiskEngine)。
- **补 settled gate / cancel-only 的并发洞**:它们读异步下游信号,对同毫秒并发无效;per-pair 闸在 OBD 回调同步置位,正好堵这个洞。

### 7.6 健康检查触发的兜底 `clear_all`(#85)

**strategy 订 `health_check.*`**(本次才落地 —— §1-6 设计有此互斥但代码一直没接):
- `on_start` 订 `health_check.started`/`finished`;维护**在跑的 source 集合** `_hc_running`(`started`→add、`finished`→discard)。**用 per-venue 信号位集合,不是 ref-count**(用户拍板):OE/PM 各是各的 source、幂等、`set` 非空即「有健康检查在跑」。
- **strategy ⊥ 健康检查互斥**:`_route_eval` pre-check `if _hc_running: 放弃 fire`(补上 §3 Strategy 行那条一直没实现的方向 —— 避免在 OE 健检 reload 页面期间下单撞页)。
- **兜底 `clear_all`**:收到任一 `finished` → 移除该 source 后,**若 `_hc_running` 空(全不在跑)且 `leg_settled.has_any_unsettled()==False`** → `pair_inflight.clear_all()`。

**为什么判据用 `leg_settled` 全 true 而非 `is_execution_active`**(用户拍板):`leg_settled[leg]=false` = 「该腿已发未确认 = 正在执行」,**arb 级**、和 per-pair 闸同粒度。`leg_settled` 全 true = 确无腿处于「已发未确认」→ 残留闸都是泄漏可清。`accept→terminal` 窗口(腿已 accept、session 还活)即便清掉也无害 —— 全局 `is_execution_active` 兜新 fire,故**不再叠加 `is_execution_active False` 条件**。

> 注:「执行 ⊥ 健康检查」已是 **per-venue(#89,见 §2.1/§1)** —— OE DataClient 订 `execution.*` 按 msg instrument venue 过滤、只数 OE 自己的腿;PM 直接读自己 session。clear_all 判据用 `leg_settled` 不是 execution-active,与此正交。

### 7.5 落地清单

- [x] `src/arbitrage/common/pair_inflight.py`:`PairInFlightGate`(`try_enter` / `release_eval` / `exec_started` / `exec_finished` / **`clear_all`**,带 max-hold 自愈)
- [x] `ArbContext` + launcher 注入(StrategyEvaluator deps + execution session `_init_arb_session`)
- [x] Strategy `_route_eval` 同步 `try_enter`(create_task 前)+ `_evaluate_and_fire` finally `release_eval`(未 fire)
- [x] execution `_begin_session` `exec_started` / `_end_session` `exec_finished`
- [x] **(#85)Strategy 订 `health_check.*` → `_hc_running` per-venue 集合 + `_route_eval` 互斥 pre-check + `finished` 时(全不在跑 + leg_settled 全 true)`clear_all`;注入 `leg_settled`**
- [x] 测试:`test_pair_inflight.py`(含 `clear_all`)+ `test_evaluator.py` eval.15-19(并发只 fire 一次 / 不同 pair 不阻塞 / 健检在跑放弃 / 健检结束+全 settled→clear / 有腿未结算→不 clear)

---

## 8. 迁移:健康检查 → NT 原生 reconciliation 的同步影响(#105,设计/待落地 as-of 2026-06-13)

> **本节是 §1–7 的现状真理源**(规则 6 失效就地标记已在文首挂出)。理由/决策史见 `refactor.md #105`。
> 本节只收**横切同步**部分(页锁层次 / 状态位迁移 / pair_inflight 兜底 / "NT 不串行化"事实);OE 取 order/position 的 reload 接口、single-flight、`_current_bets` 双视图、WS 存活检测属 **OE ExecClient 组件家**,见 execution `architecture.md`。

### 8.1 关键事实:NT 不串行化 reconciliation ⊥ execution

NT `LiveExecutionEngine` 把命令处理设计成**并发、无互斥**:

- **所有命令 `create_task` fire-and-forget**(`live/execution_client.py` `submit_order` / `cancel_order` / `query_order` 等均 `self.create_task(self._<handler>(command))`)→ submit/cancel 与 reconciliation 的 `query_order` 是**独立 task,在同一事件循环上各跑各的、在每个 `await` 点交错**;cmd 队列不阻塞。
- `reconciliation_active`(`live/execution_client.py:451`)**只写不读**,不是闸。
- 无 page 锁、无 client 锁、无"对账期间挂起执行"机制。

**为什么 NT 这样**:它假设 venue 是**无状态 REST 端点**(并发 query + submit 无害)——对 **PM(REST)成立**;对 **OE(共享可变 Playwright 页)不成立**:query/reload 与 place/cancel 在同一页交错 = 页面状态损坏(历史上"同时下单丢回执"即此类)。**所以迁到 NT 原生后,OE 侧的页互斥不会消失,只从 HealthCheckLoop 搬到 OE ExecClient 的资源层。**

### 8.2 OE 页级互斥(取代 strategy⊥健康检查 + 健康检查⊥执行两道旧互斥)

- **机制 = OE ExecClient 内一把 `asyncio.Lock`(页锁)**,串行所有"碰 OE execution 页"的操作:`place_order` / `cancel_order` / `reload`(无论 reload 由 in-flight / open / position check 还是存活闸触发)。reload 再叠 **single-flight**(窗口内合并多次触发为一次,见 execution 文档)。
- **取代关系**:旧 `_hc_running`(strategy⊥健康检查)存在的唯一理由是"没有页锁时 reload 会撞进正在进行的下单"。页锁补上后此理由消失 → **`_hc_running` + `health_check.*` 退役,且不引入 `reconcile_in_progress`**。OE 因此与 PM 对称:reconciliation 与 execution 共存,只在资源层(OE 页锁 / PM 无需)串行。
- **唯一 PM 没有、OE 有的不对称**:OE reload 是**阻塞**操作(拆页重建),PM reconcile 是**非阻塞** REST。所以 fire 撞进一次(稀有的)reload 时 OE 腿会卡在页锁上等。两点缓解:① 新设计 reload 很稀(健康态读实时 `_current_bets`,只 WS 判死/单卡死才 reload);② 真正会触发 reload 的时刻往往正是 OE exec 通道不健康之时——那本不该发 OE 单。**真正该补的闸是 venue-exec-liveness(待议),不是 reconcile_in_progress**。

### 8.3 `place_bets` 顺序提交 → 回到并发(页锁补在正确层后)

`place_bets.py` 的逐腿 `for leg: await submitter` 串行,是"OE 页并发 placeBets 丢回执"的上层 workaround。页锁把根因治在资源层后,该 workaround 多余 → **回到并发 `gather`**(PM/OE 腿并行提交,对冲窗口更窄)。⚠️ **必须 live 重验**(原问题是真盘观测的丢回执,验收锚点=并发下两腿回执都收到、`_current_bets` 都更新)。页锁还顺带覆盖**跨 pair 的 OE 页争用**(顺序 `place_bets` 只管单 arb 内的腿),故严格更稳。

### 8.4 pair_inflight 兜底迁移(`PairInFlightGate` 本体保留,触发改变)

`leg_settled` 与 `PairInFlightGate`(§7)**机制本体保留**;变的是**防泄漏兜底**:

> **设计转向(2026-06-14,用户拍板)**:**去掉所有"兜底/猜测"机制(max-hold / A5 / health_check→clear_all),改为"保证每条 execution 结束时一定置位"**。判据:`PairInFlightGate` 的 `exec_count→0` 就是"本 pair 执行会话结束"(那一刻所有 session 都 `_end_session` 了、`_submit_order` 都返回了 → 无该 pair 任务在执行),**race-free**(exec_count 只在最后一条归 0)。关键是让**每一种 execution 都纳入 `exec_count`**,exec_count→0 自然兜到底,不需要任何 backstop。

- **submit+track session(已有)**:`_begin_session` `exec_started` / `_end_session`(terminal **或** watchdog 超时,`session.py:103` NT clock 绝对 alert,不随 session 任务异常取消)`exec_finished`。
- **cancel-only 的撤单(✅ #105 已落地 2026-06-14)**:**撤单也是一次执行,纳入 exec_count** —— base `_cancel_residual_orders` 对每条残单**同步 `exec_started`**(先全加完,避免某条先完成提前清)+ `create_task(_tracked_residual_cancel)`;`_tracked_residual_cancel` 在 `finally` `exec_finished`。子类只实现 `_cancel_residual_one`(真实 venue 撤单)。**这堵住了"`exec_count→0`(session 结束)时撤单 task 还在跑"** —— 撤单 task 在跑则 exec_count>0,不会提前清。`test_orbitexch_client.py::test_cancel_residual_tracked_*`。
- **watchdog 保留**:它是"session 起了但 terminal 不来(卡单)"的保证 —— **session 一定结束**(`_end_session`→`exec_finished`),卡单本身(订单在 venue 上死活)是 leg_settled 的事,不是 in-flight 的事。
- **退役 / 回退**:① **A5 desync 兜底回退**(2026-06-14;它会误清 fire→`_begin_session` 的 handoff 窗口 → 同突发重复 fire,`test_same_pair_concurrent_eval_fires_once` 实测会挂);② **max-hold** + **`health_check.finished→clear_all`** 待 deny(见下)解决后删。
- ⚠️ **待决:deny**(strategy fired、**全 deny** → RiskEngine 在 `_submit_order` 前拒 → 根本没进 execution → `exec_count==0` → in-flight 漏)。**deny 不是一次 execution**,exec_count 兜不到 → 需单独处理:strategy `on_order_denied` 释放 / fire 前加 settled·risk pre-check 不 fire / 暂保留 max-hold 仅为此。**删 max-hold 前必须先定 deny。**

### 8.5 迁移后状态位最终图景

| 机制 | 去留 | 维护 / 读取 |
|---|---|---|
| `leg_settled`(per-pair-leg) | **保留** | `_begin_session` arm false / venue-confirm mark true;RiskEngine·Portfolio live fail-closed |
| `PairInFlightGate`(per-pair) | **保留**(兜底改 §8.4) | strategy `_route_eval` 同步 `try_enter` / execution `exec_started`·`exec_finished` |
| per-session 超时 watchdog | **保留**(pair_inflight 主防线) | `_begin_session` 挂 NT clock alert |
| `execution.started/finished` 消息 | **退役** | 旧消费者(OE DataClient 互斥)随迁移消失;strategy 改直读 callable → 无消费者 |
| 全局 `is_execution_active` callable | **新增** | launcher 注入 strategy;OR(PM `_execution_active`, OE `_execution_active`)= 各自 `len(_active_sessions)`;供 try_enter 兜底 |
| `health_check.started/finished` + `_hc_running` | **退役** | —(页锁取代) |
| `_execution_active` / per-venue 健康检查⊥执行互斥 | **退役** | —(NT 不串行化,页锁在资源层串行) |
| max-hold 自愈 / `health_check→clear_all` | **退役** | —(watchdog + try_enter execution-alive 取代) |
| `reconcile_in_progress` | **不引入** | —(PM 没有;页锁 + leg_settled 已够) |
| **venue-liveness 预闸** | **新增(#105 已定)** | venue 死活 = **reconcile 成败**(OE reload / PM REST 拉,对称);launcher 注入 `is_<venue>_alive` callable,strategy fire 前置 pre-check,dead 不 fire(状态读、非消息;同 execution-alive 范式)。死活/重试机制见 execution `§4.3bis(4)` |

### 8.6 落地清单(设计/待落地)

- [~] OE ExecClient 页锁(`asyncio.Lock`)串行碰页操作:**place/cancel ✅ 已落地(2026-06-13,`execution.py` `_page_lock` 包 `_place_via_executor`/`_cancel_one`;`test_orbitexch_client.py::test_page_lock_serializes_concurrent_page_ops`)**;reload + single-flight 待 reload-then-report 落地一并接
- [x] `place_bets` 顺序提交 → `gather`(**✅ 已落地 2026-06-13,`place_bets.py`;`test_action_place_bets.py`**);⚠️ **仍需 live 重验**两腿回执(页锁串行兜底,真盘确认不丢回执)
- [ ] 退役 `health_check.*` topic + Strategy `_hc_running` + 健康检查⊥执行 per-venue 互斥(§1–7 对应代码)
- [ ] `PairInFlightGate`:删 max-hold;`try_enter` 加 execution-alive + leg_settled 全 true → `clear_all` 兜底
- [ ] launcher 注入全局 `is_execution_active` callable(OR PM/OE `_execution_active`)给 strategy;**退役 `execution.*` 消息**(无消费者)
- [ ] venue-liveness 预闸:launcher 注入 `is_<venue>_alive` callable(死活=reconcile 成败,见 execution §4.3bis(4));strategy fire 前置 pre-check
- [ ] 统一断线保护:reconcile 失败 → venue dead + 持续重试(OE reload / PM REST)直到成功;成功 → alive + mark leg_settled
- [ ] 测试:synchronization / strategy / OE adapter README 待详细设计落定后补

---

## 8.7 协调切换 cutover 计划(顺序 / feature flag / live 验收 / 回滚)

> reload-then-report 接线、退役旧健康检查、开 NT reconciliation、leg_settled remark、退役旧消息**互相依赖,必须一次切**(单独上会出现旧 DataClient reload 与新 ExecClient reload 两条不协调 reload 路径,见 §8.1/§4.3bis)。本节定 cutover 顺序,把风险压到"翻一个 flag"。
> **scope**:本 cutover 只切 **execution-page reconciliation**;competition/OBD 页 Phase-1 staleness → WS 存活的迁移(DataClient)是**另一独立子步**,不在此。

**feature flag**:`oe_native_reconcile_enabled`(config,**默认 False = 旧行为**)。Phase A 全部加性、不改 default;flip 只翻 flag。

### Phase A — 预备(可逐项独立 land + 测,加性、default 行为不变)
- [x] **A1** `_last_frame_ns` 存活锚(**✅ 已落地 2026-06-13**):WS handler `on_frame` 每帧(含 SockJS 心跳 `'h'`)回调 → ExecClient `_mark_exec_frame` 刷 `_last_frame_ns`;`_exec_ws_fresh()` 读(idle=300s);`test_orbitexch_client.py::test_handler_on_frame_fires_*`/`test_exec_ws_fresh_lifecycle`。**`_exec_ws_fresh` 暂未驱动 reload(A2/flip 接入)**。
- [x] **A2** `_reload_exec_page` + `_ensure_exec_snapshot_fresh`(reload + single-flight + 存活闸,`§4.3bis(3)`,**✅ 已落地 2026-06-13**):`_exec_ws_fresh` 真→不 reload;假→single-flight reload(走页锁)+ 等 CURRENT_BETS 重推(`_last_current_bets_ns`>reload_ts,超时=失败=venue dead)。`test_orbitexch_client.py::test_ensure_fresh_*`/`test_reload_exec_page_timeout_returns_false`(4 case)。**flag off,未接入 `generate_*_reports`**。
- [x] **A3** leg_settled 在 `_on_current_bets` 加 mark(**✅ 已落地 2026-06-13**):`LegSettledRegistry.mark_venue(venue_value)`(该 venue 所有 armed 腿置 true,缺席快照=已澄清没成功亦置)+ `_on_current_bets` → `mark_venue(ORBITEXCH)`;**与旧 funnel mark 并存(幂等)**,flip(B4)删 funnel。`test_leg_settled.py::test_mark_venue_*` + `test_orbitexch_client.py::test_on_current_bets_marks_oe_legs_settled`。**只挂 order 真值,position 解耦**(§4.3bis(5) 统一原则)。
- [x] **A4** 全局 `is_execution_active` callable(**✅ 已现成**:launcher `_make_is_execution_active` OR PM/OE `_execution_active`,Q19 旧接线复用)。per-venue `is_<venue>_alive`(backing state 要等 reconcile 接线)→ **留 flip B5**。
- [x] **A5** `try_enter` 兜底(**✅ 已落地 2026-06-13**):`_route_eval` try_enter 被拒 → `exec_in_flight(pair)`(`_exec_count>0`)+ 全局非 alive + leg_settled 全 true → `clear_all` 重试。**与 max-hold + health_check→clear_all 并存**。**实现修正**:必须加 `exec_in_flight`,否则 handoff 窗口被误清→重复 fire(见 §8.4)。`test_evaluator.py::test_pair_inflight_leak_backstop_clears_and_fires`/`_skips_when_execution_active`/`_skips_when_unsettled` + `PairInFlightGate.exec_in_flight`。
- [ ] **A6** position 聚合(`generate_position_status_reports`)**✅ 已落地**(本身不被实时调用,见 `§4.3bis(2)`)。

### Phase B — flip(翻 `oe_native_reconcile_enabled=True`;先 dev/纸面验,再 live)
- [ ] **B1** `generate_*_reports` 接 `_ensure_exec_snapshot_fresh`(reload-then-report 实时生效)。
- [ ] **B2** DataClient HealthCheckLoop **Phase-2 exec reload 关**(flag on → 不挂 / 跳过 `_reload_execution_page`)。
- [ ] **B3** NT reconciliation 配置:`reconciliation=True` + `timeout_connection≥180s` + `open/position check 关` + `inflight 开`(`§4.3bis(7)`)。
- [ ] **B4** leg_settled **删 funnel mark**(只留 `_on_current_bets`)→ 强制 Q3 不变量(Path B 不 mark,`§4.3bis(5)`)。
- [ ] **B5** 退役 `_hc_running` + `health_check.*`(strategy 不订/不挡)→ **venue-liveness 闸接管 fire pre-check**(读 `is_<venue>_alive`)。
- [ ] **B6** `PairInFlightGate` **删 max-hold**(只留 A5 desync 兜底);**退役 `execution.*` 消息**(strategy 改 callable 直读)。⚠️ **待决**:A5 只兜 `_exec_count>0` 的 desync 泄漏;**"fired 但一腿 session 都没起"(exec_count==0,全 deny/cancel-only 丢弃)目前靠 max-hold** → 删 max-hold 前须先处理(保留 max-hold 仅为此 case,或源头修:fire 后下游无 session 时释放闸)。见 §8.4。

### live 验收锚点(flip 后真盘 / mock 验)
1. 下单 → Accepted → fill 全链路正常;`place_bets` 并发两腿回执都收到(slice 1 的 live 待办一并验)。
2. 制造 **reconcile 失败**(reload 失败 / 一直拿不到新 CURRENT_BETS)→ `is_oe_exec_alive`=False → **fire 被 venue-liveness 闸挡** + reconcile 持续重试 → 一旦 reconcile **成功**(拿到真值)→ alive + 不挡 + leg_settled 解封。**注**:WS 静默本身**不判死**(只触发探测 reconcile,§4.3bis(4)),探测成功即 alive;死活裁决一律是 reconcile 成败。
3. Path B(stuck order 无真实 response)→ **leg_settled 留 false**(不被 fabricate 事件置位)→ pair 安全挡住。
4. 注入 pair_inflight 泄漏(`_exec_count` 卡)→ try_enter 经 execution-alive 兜底清。

### 回滚
**`oe_native_reconcile_enabled` → False 即回旧健康检查**(DataClient HealthCheckLoop + funnel mark + `_hc_running` + max-hold 全部恢复)。因 Phase A 加性、Phase B 全部 flag-gated,**回滚只翻 flag、无需 revert 代码**。
