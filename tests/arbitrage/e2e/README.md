# e2e 测试(占位)

端到端套利场景测试,涵盖完整链路: discovery → matching → strategy → execution → events。

对应章节: 不归属单一 §,贯穿 §5.1-§5.7

## 与 `src/arbitrage/testing/` 的关系

- `src/arbitrage/testing/`(`runner.py` / `scenario.py` / `conditions.py`)是**实盘场景测试框架**,跑真实账户/真实订单
- 本目录是**集成测试**,用 mock / paper trading 跑端到端流程,不动真实订单
- 两者互补:本目录覆盖逻辑正确性,`src/arbitrage/testing/` 覆盖实盘场景

迁移完成后两者可能合并(Step 8 清理时考虑)。

## 预期用例(占位)

- e2e-1: 完整套利会话(从 instrument 加载到双腿成交)在 paper trading 上的端到端验证
- e2e-2: 当前主流程闭环:mean_rebate 下一轮机会重新 submit 时,execution barrier 收齐同 opportunity 的 risk-pass legs;若任一 leg 有 residual 且 risk-pass legs 中没有显式撤单腿 → 整次 opportunity cancel-only,撤残单并丢弃本次所有新 submit(`test_mean_rebate_cancel_only.py` 需升级为 barrier 级验收)
- e2e-3: 单腿成交另一腿失败时的专门 recovery 状态机(后议,不属于当前主流程闭环)
- e2e-4: 启动重连 reconciliation(Cache 状态与 venue 一致)
- e2e-5: 多 MatchedPair 并发处理

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
- 输入: 用 `TestClock.advance_time(...)` 触发 `arb_opp_timeout:{opportunity_id}`。
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
- 前置:PM+OE 两腿同一 `opportunity_id`,两条 risk-pass;PM instrument 有 residual open order,OE instrument 无 residual;本轮 risk-pass legs 无显式撤单腿。
- 输入:barrier 收齐两腿。
- 步骤:观察 PM/OE ExecutionClient 调用。
- 期望:PM residual 被撤;OE 新 submit 不进入 OE ExecutionClient;PM 新 submit 也不进入 PM ExecutionClient。
- 验收:同一 opportunity 不允许一边 cancel-only、一边 submit+track。

### e2e-9c: residual + 显式撤单腿不触发普通整次 cancel-only
- 前置:同一 opportunity risk-pass legs 中包含显式撤单腿,且某 instrument 有 residual。
- 输入:barrier 收齐 legs。
- 期望:barrier 不因 residual 把整次 opportunity 改写为普通 cancel-only;按撤单腿所属设计继续。
- 验收:撤单腿判定来自显式 metadata/command,不是 residual cancel-only 的内部动作。

## VenueExecutionLiveness opportunity 门控(设计待落地,2026-06-15)

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
- 验收: Risk 用 `expected_legs` 推导 required venues,不是只看当前 order venue;`risk.opportunity.leg_denied` 触发 barrier zero-session finish。

### e2e-11: venue liveness 恢复后下一轮 opportunity 可通过
- 前置: e2e-10 后,reconcile 成功写 `oe_order_alive=true` 且 `oe_position_alive=true`;PM 仍 alive。
- 输入: 下一轮 Strategy 重新评估同 pair 并生成新 opportunity。
- 期望: liveness gate 不再 deny;若余额/rebate gates 也通过,两腿进入 execution barrier,收齐后 release。
- 验收: liveness 是 Risk live 状态,不依赖 Strategy 缓存或旧 opportunity 状态。
