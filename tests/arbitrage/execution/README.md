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

## 异常路径置位保证(#105 ①②,已落地代码)

对应设计:execution §4.1(submit 异常收口 PM/OE 对称)+ §4.2(watchdog 与 per-pair 计数原子)。
目标:**只要执行进入了一笔 session,无论成功/失败/异常/超时,pair in-flight 一定有出口被清**。

### execution-4.1.5: OE submit 异常 → 立刻 rejected + 结束 session(对齐 PM,#105 ①)
- 前置:`_begin_session` 通过(submit+track),注入会抛异常的 `_place_via_executor`。
- 输入:`_submit_order` 执行,placement await 抛 `TimeoutError`(Playwright 崩)。
- 期望:`generate_order_rejected`(reason 含 "exception before venue acknowledgement")+ `_end_session`
  立刻调用;不生成 `OrderAccepted`;不干等 §4.2 watchdog 整个超时。
- 验收:`test_orbitexch_client.py::test_submit_order_exception_rejects_and_ends_session`。

### execution-4.2.2: watchdog 在 exec_started 之前 arm,异常不留悬挂 count(#105 ②)
- 前置:`PairInFlightGate` 已由 strategy `try_enter` 置位;注入会抛的 `set_time_alert_ns`(代理 clock)。
- 输入:`_begin_session` 执行,arm watchdog 时抛。
- 期望:`exec_started` 未触达(`_exec_count==0`),session 未建立;eval 层 in-flight 仍可由 `release_eval` 清。
- 验收:`test_session.py::test_watchdog_armed_before_exec_started_no_leak_on_alert_failure`
  + `test_begin_session_arms_watchdog_and_exec_started`(正常路径:exec_started 自增 + watchdog 已 arm)。

### execution-4.2.3: _end_session 出口对称 —— exec_finished 先于 publish(#105 ②)
- 前置:session 在飞(`exec_started` 已 ++,in-flight 置位);注入会抛的 `_publish_execution`。
- 输入:`_end_session` 执行,publish 抛。
- 期望:`exec_finished` 已先行 → `_exec_count→0` → in-flight 被清(publish 抛之前已落)。
- 验收:`test_session.py::test_end_session_clears_inflight_even_if_publish_throws`。

## VenueExecutionLiveness 写入(设计待落地,2026-06-15)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.5` + execution §4.3bis/§4.4。

### execution-4.5.1: order reconcile 成功置 order_alive
- 前置:PM 或 OE ExecutionClient 注入共享 `VenueExecutionLiveness`;该 venue `order_alive=false`。
- 输入:order/open-order reconcile 成功,拿到完整真实 response。
- 期望:`venue_order_alive[venue]=true`;不写 `venue_position_alive`。
- 验收:order 与 position 状态拆分;不再调用 `LegSettledRegistry.mark/mark_venue`。

### execution-4.5.2: position reconcile 成功置 position_alive
- 前置:该 venue `position_alive=false`。
- 输入:position reconcile 成功,拿到完整真实 response。
- 期望:`venue_position_alive[venue]=true`;不写 `venue_order_alive`。
- 验收:PM order/position 两条路径都成功后,risk 派生 `venue_alive=true`。

### execution-4.5.3: reconcile 失败 fail-closed
- 前置:venue 当前 alive。
- 输入:order reconcile 超时/失败或未拿到完整 order response。
- 期望:`venue_order_alive[venue]=false`;position 失败同理只置 `venue_position_alive=false`。
- 验收:WS 静默只触发探测;真正置 false 的依据是 reconcile 失败。

### execution-4.5.4: 普通 submit 不主动置 false
- 前置:venue order/position 均 alive。
- 输入:普通 submit+track session 开始。
- 期望:不因“每次下单”把 alive 置 false;session 生命周期仍由 `PairInFlightGate` + watchdog 管。
- 验收:只有 stuck/reconcile failure 等真相不可信路径才置 false。
