# e2e 测试

端到端套利场景测试,涵盖完整链路: discovery → matching → strategy → execution → events。

对应章节: 不归属单一 §,贯穿 §5.1-§5.7

**状态(2026-07-08)**:barrier / liveness 的关键机制已有 execution/risk 离线单测覆盖,本文保留跨组件 E2E 与真钱 live 验收清单。需要真实账户或全节点启动的项目必须由用户明确触发。

## 与 `src/arbitrage/testing/` 的关系

- `src/arbitrage/testing/`(`runner.py` / `scenario.py` / `conditions.py`)是**实盘场景测试框架**,跑真实账户/真实订单
- 本目录是**集成测试**,用 mock / paper trading 跑端到端流程,不动真实订单
- 两者互补:本目录覆盖逻辑正确性,`src/arbitrage/testing/` 覆盖实盘场景

迁移完成后两者可能合并(Step 8 清理时考虑)。

## 预期用例

- e2e-1: 完整套利会话(从 instrument 加载到双腿成交)在 paper trading 上的端到端验证
- e2e-2: 当前主流程闭环:Strategy Action 先生成 execution plan，Evaluator 统一分发后，execution barrier 收齐同 opportunity 的 risk-pass legs；若任一 leg 有 residual → 整次 opportunity cancel-only，撤残单并丢弃本次所有新 submit。显式补偿撤单使用同一 grouped-command barrier 的 CancelOrder policy，不伪造成 SubmitOrder 腿。测试输入 leg 必须已带 `share_if_wins/qty`，Action 不再用 `share` 参数兜底(`test_mean_rebate_cancel_only.py` 需升级为 barrier 级验收)
- e2e-3: 单腿成交另一腿失败时的专门 recovery 状态机(后议,不属于当前主流程闭环)
- e2e-4: 启动重连 reconciliation(Cache 状态与 venue 一致)
- e2e-5: 多 MatchedPair 并发处理
- e2e-12: SharpExch 完整真钱套利端到端验收(venue 插拔化第二阶段完成后再测;第一阶段只验 SE adapter probes 与 skip node smoke)

## Opportunity execution barrier(已落地代码,待 live 验证,2026-06-14)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.4bis` + execution §3.5。

### e2e-6: 两腿 Risk pass 后才 release 到 ExecutionClient
- 前置: 两条 `SubmitOrder` 带同一 `opportunity_id`,不同 `leg_key`,相同 `expected_legs`。
- 输入: 两条订单均经 Risk pass 回到 `ExecEngine.execute`。
- 步骤: 在只收到第一条 pass 时断言 ExecutionClient 未被调用;收到第二条 pass 后再断言两条都 release。
- 期望: barrier 暂存 partial pass;收齐才进入 venue execution。
- 验收: 没有一腿先于另一腿 risk decision 进入 ExecutionClient。
- 状态:✅ 离线 execution 单测覆盖;live e2e 待验证。

### e2e-7: 任一腿 Risk deny 时整次 opportunity zero-session finish
- 前置: 第一腿 Risk pass 已在 barrier pending;第二腿 Risk deny 并发布 `risk.opportunity.leg_denied`。
- 输入: barrier 收到 deny 消息。
- 步骤: 检查 pending 第一腿未进入 ExecutionClient,并收到本地 `OrderDenied`。
- 期望: opportunity 以 denied 结束,所有 pending 清理。
- 验收: 统一 execution finish outlet 被调用一次,`pair_inflight` 由 outlet 释放,不是 deny 分支直接释放。
- 状态:✅ 离线 execution 单测覆盖;live e2e 待验证。

### e2e-8: barrier timeout 使用 NT clock 并走统一出口
- 前置: barrier 收到一条 risk-pass leg,缺少另一个 expected leg。
- 输入: 用 `TestClock.advance_time(...)` 触发 `arb_group_timeout:submit:{opportunity_id}`。
- 步骤: 检查 pending leg 未进入 ExecutionClient,收到本地 `OrderDenied`。
- 期望: timeout 等同 opportunity denied,取消 timer 并清 context。
- 验收: timeout 只覆盖 Risk decision 收齐窗口;release 后不影响 per-session venue watchdog。
- 状态:✅ 离线 execution 单测覆盖;live e2e 待验证。

### e2e-9: release 后由真实 session 汇总到同一 execution 出口
- 前置: 两腿 Risk pass,barrier 已 release 到 PM/OE ExecutionClient;两侧 session 各自结束。
- 输入: 先结束一腿,再结束另一腿。
- 步骤: 观察 opportunity context。
- 期望: 第一腿结束不释放 `pair_inflight`;最后一腿结束触发同一个 finish outlet。
- 验收: pass / deny / timeout 三条路径都由同一个 outlet 释放 `pair_inflight`。

### e2e-9b: residual cancel-only 必须在 opportunity barrier 层协调两 venue
- 前置:PM+OE 两腿同一 `opportunity_id`,两条 risk-pass;PM instrument 有 residual open order,OE instrument 无 residual。
- 输入:barrier 收齐两腿。
- 步骤:观察 PM/OE ExecutionClient 调用。
- 期望:PM residual 被撤;OE 新 submit 不进入 OE ExecutionClient;PM 新 submit 也不进入 PM ExecutionClient。
- 验收:同一 opportunity 不允许一边 cancel-only、一边 submit+track。

### e2e-9c: spread cancel 跨 venue 命令经共享 barrier 的 CancelOrder policy
- 前置:spread cancel 命中，目标 pair 在 PM/OE/SE 中有两条以上 open order。
- 输入:Strategy 为每条订单发出共享 `opportunity_id/expected_cancels` 的标准 CancelOrder。
- 期望:命令未收齐前任何 venue 不收到撤单；收齐后统一 release，各 venue 独立确认终态。
- 验收:busy/timeout 不触达 venue且 `OrderCancelRejected` 回退本地状态；不宣称 venue 原子完成。

### e2e-9d: evaluation 后成交改变仓位时不 release 旧机会
- 前置:Strategy 已记录 pair 的 `positions_digest`（#317:open_orders_digest 已删），订单通过 Risk、尚未收齐
  barrier；期间 BUY/SELL/外部成交或 position reconcile 已更新 NT Cache position。
- 输入:最后一条 risk-pass 腿进入 barrier。
- 期望:barrier 重算 position digest 发现变化，整组本地 deny，不向任何 venue release。
- 验收:离线 `test_barrier_denies_all_legs_when_pair_positions_changed` 已覆盖；真实成交窗口 E2E 待验。

## VenueExecutionLiveness opportunity 门控(代码已落地,E2E 待验,2026-06-15)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.5` + risk §3.1。

### e2e-10: PM+OE opportunity 任一 required venue not alive 时不半边落地
- 前置: Strategy 生成 PM+OE 两腿,两条订单都带 `expected_legs=("pm:home:0","oe:away:1")`;PM alive,OE `order_alive=false`。
- 输入: 两条订单进入 Risk。
- 步骤:
  1. 先处理 PM leg risk check。
  2. 再处理 OE leg risk check。
  3. 观察 Execution barrier 与 ExecutionClient。
- 期望:
  - PM leg 也被 Risk deny,因为 required venues 包含 OE。
  - OE leg 同样 deny。
  - ExecutionClient 没有任何一腿真实 submit。
- 验收: Risk 用 `expected_legs` 推导 required venues,不是只看当前 order venue;`risk.opportunity.leg_denied` 触发 barrier zero-session finish。离线验收已覆盖 PM/OE/SE required venue 解析与无法解析的 `pmsports:*` fail-closed;本目录保留全链路 E2E 待验。

### e2e-11: venue liveness 恢复后下一轮 opportunity 可通过
- 前置: e2e-10 后,reconcile 成功写 `oe_order_alive=true` 且 `oe_position_alive=true`;PM 仍 alive。
- 输入: 下一轮 Strategy 重新评估同 pair 并生成新 opportunity。
- 期望: liveness gate 不再 deny;若余额/rebate gates 也通过,两腿进入 execution barrier,收齐后 release。
- 验收: liveness 是 Risk live 状态,不依赖 Strategy 缓存或旧 opportunity 状态。

### e2e-12: SharpExch 完整真钱套利端到端验收
- 前置: venue 插拔化第二阶段完成,SE/OE/PM 均由 registry/capability 配置进入 Matching/Strategy/Risk/Execution,且用户明确授权真钱测试。
- 输入: 启动包含 SE 的真实 arb node,开启真实 strategy/risk/barrier/execution 链路。
- 步骤: 选择小额机会,观察从 discovery、matching、strategy candidate、risk pass、barrier release、SE execution、CURRENT_BETS/fill/reconcile 到 opportunity finish 的完整链路。
- 期望: 不依赖第一阶段 PM/OE/SE 硬编码路径推进真钱 E2E;SE leg 与其它 venue leg 同步 release,成交/撤单/失败均走统一出口。
- 验收: 一次完整 opportunity 不出现半边绕过 barrier、pair_inflight 卡死、SE liveness 误判、或残单未进入 cancel-only/reconcile 的情况。
- 状态:第二阶段完成后再执行;第一阶段不跑该真钱 E2E。
