# Execution 测试说明

## Opportunity execution barrier(#106/#107,已落地代码)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.4bis` + execution §3.5。

### execution-3.5.1: partial risk-pass 不 release 到 ExecutionClient
- 前置:两条 `SubmitOrder` 带同一 `opportunity_id`,expected legs 为两腿。
- 输入:只把第一条 risk-pass command 送入 `ArbLiveExecutionEngine._execute_command`。
- 期望:command 暂存在 barrier,ExecutionClient 未收到 submit。
- 验收:`tests/arbitrage/execution/test_engine_barrier.py::test_barrier_waits_until_all_legs_pass_before_release` 覆盖。

### execution-3.5.2: all risk-pass 后 release 全部 legs
- 前置:同上。
- 输入:第二条 risk-pass command 到达。
- 期望:两条 command 都交回父类 execution 路由到 ExecutionClient。
- 验收:同 `test_barrier_waits_until_all_legs_pass_before_release` 覆盖。

### execution-3.5.3: risk deny 关闭 opportunity 并 zero-session finish
- 前置:第一腿已 pending,同 pair 的 `PairInFlightGate` 已置位。
- 输入:`risk.opportunity.leg_denied` 领域消息。
- 期望:pending leg 不进 ExecutionClient,本地发 `OrderDenied`,pair gate 释放。
- 验收:`test_barrier_deny_blocks_pending_leg_and_releases_pair_gate` 覆盖。

### execution-3.5.4: barrier timeout 关闭 opportunity 并 zero-session finish
- 前置:第一腿已 pending,缺少另一条 expected leg。
- 输入:触发 `arb_opp_timeout:{opportunity_id}`。
- 期望:pending leg 不进 ExecutionClient,本地发 `OrderDenied`,pair gate 释放。
- 验收:`test_barrier_timeout_blocks_pending_leg_and_releases_pair_gate` 覆盖。
