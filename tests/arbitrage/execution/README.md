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

### execution-3.5.5: all risk-pass 后若同 pair 有 residual 且无撤单腿,整次 opportunity cancel-only
- 前置:两条 `SubmitOrder` 带同一 `opportunity_id`;同一 `pair_id` 的任一 registered instrument 有 residual open order;本轮 risk-pass legs 不含显式撤单腿。
- 输入:两条订单均经 Risk pass 回到 barrier。
- 期望:barrier 不 release 任一新 submit 到 PM/OE ExecutionClient;PM residual 进入 tracked cancel;PM/OE 两条新 submit 都收到本地 deny/reject。
- 验收:不会出现“PM 撤旧、OE 同轮又开新单”的半边执行;live 验收锚点为 `Opportunity cancel-only: residual open orders present`。
- 状态:✅ `test_engine_barrier.py::test_barrier_cancel_only_blocks_all_new_submits_when_residual_and_no_cancel_leg`

### execution-3.5.5a: 单腿 opportunity 也按 pair-wide 范围检查 residual
- 前置:本轮 opportunity 的 `expected_legs` 只有一条 PM leg;`PairRegistry.instrument_ids_for_pair(pair_id)` 返回同 pair 的其它 PM/OE instruments;其中一个非 expected instrument 有 residual open order。
- 输入:该单腿订单经 Risk pass 回到 barrier。
- 期望:barrier 仍发现同 pair 其它 instrument 的 residual,整次 opportunity cancel-only,不 release 新 submit。
- 验收:`test_engine_barrier.py::test_barrier_residual_check_is_pair_wide_even_for_single_leg_opportunity` 覆盖。

### execution-3.5.6: residual 存在但 risk-pass legs 含显式撤单腿时不改写为整次 cancel-only
- 前置:同一 opportunity 已收齐 risk-pass legs,其中至少一条 leg 由 metadata/command 明确标记为撤单腿。
- 输入:某 instrument 仍存在 residual open order。
- 期望:barrier 不按普通 residual 规则丢弃整次 opportunity;后续按显式撤单腿语义执行。
- 验收:撤单腿必须显式表达,不能由 residual cancel-only 内部动作反推。
- 状态:✅ `test_engine_barrier.py::test_barrier_residual_with_explicit_cancel_leg_releases_normally`

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

## VenueExecutionLiveness 写入(已落地代码路径,2026-06-15)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.5` + execution §4.3bis/§4.4。

## OE fx 边界(已落地代码路径,2026-06-30)

对应设计:execution §4.3bis(5c)。adapter 外部统一 USD 口径,OE adapter 自己负责 BALANCE/CURRENT_BETS 入站乘 fx、placeBets 出站除 fx。

### execution-5.fx.1: factory 注入启动 fx
- 前置:`prepare_arb_context(..., session_timeout_secs_by_venue={"ORBITEXCH": 45.0}, arbitrage_params=ArbitrageParams(fx=1.25))`。
- 输入:`ArbOrbitExchLiveExecClientFactory.create(...)`。
- 期望:构造出的 `OrbitExchExecutionClient._current_fx()==1.25`。
- 验收:`tests/arbitrage/execution/test_factories.py::test_oe_factory_create_with_context_returns_arb_client`。

### execution-5.factory.1: OE session timeout keyed map 必填
- 前置:`prepare_arb_context(venue_liveness=...)` 未提供 `session_timeout_secs_by_venue["ORBITEXCH"]`。
- 输入:`ArbOrbitExchLiveExecClientFactory.create(...)`。
- 期望:factory fail-fast,不使用默认值或旧 venue 专属字段兜底。
- 验收:`tests/arbitrage/execution/test_factories.py::test_oe_factory_requires_session_timeout_keyed_value`。

### execution-5.fx.2: Web 热改 fx 同步到 OE client
- 前置:OE execution client 已订阅 `command.arb.arbitrage_params`。
- 输入:publish `SetArbitrageParamsCommand(fx=1.31)`。
- 期望:`_current_fx()` 更新为 1.31,后续 executor payload 使用新 fx。
- 验收:`tests/arbitrage/execution/test_orbitexch_client.py::test_arbitrage_fx_command_updates_oe_client`。

### execution-5.fx.3: fill delta 不受 fx 热改误触发
- 前置:OE `CURRENT_BETS.sizeMatched=7.00` 已产生一次 fill;随后 web 把 fx 从 1.3 改为 1.4。
- 输入:同一个 `sizeMatched=7.00` 快照再次到达。
- 期望:不产生第二个 fill;第一次 fill quantity = `7 * 1.3` USD。
- 验收:`tests/arbitrage/execution/test_orbitexch_client.py::test_on_current_bets_fill_delta_uses_raw_matched_when_fx_changes`。

### execution-5.fx.4: BALANCE 入站归一为 USD
- 前置:OE execution client 当前 `fx=1.3`。
- 输入:general WS 推送 `BALANCE.balance=37.49`。
- 期望:写入 NT account cache 的余额数值为 `37.49 * 1.3` 后按 Money 精度取整;Risk 后续直接比较 USD stake。
- 验收:`tests/arbitrage/execution/test_orbitexch_client.py::test_on_general_frame_balance_normalized_to_usd`。

### execution-4.5.1: order reconcile 成功置 order_alive
- 前置:PM 或 OE ExecutionClient 注入共享 `VenueExecutionLiveness`;该 venue `order_alive=false`。
- 输入:order/open-order reconcile 成功,拿到完整真实 response。
- 期望:`venue_order_alive[venue]=true`;不写 `venue_position_alive`。
- 验收:`test_orbitexch_client.py::test_on_current_bets_marks_oe_liveness_alive`;PM report 包装路径由 execution 文档约束,后续 live/reconcile 测继续补强。

### execution-4.5.2: position reconcile 成功置 position_alive
- 前置:该 venue `position_alive=false`。
- 输入:position reconcile 成功,拿到完整真实 response。
- 期望:`venue_position_alive[venue]=true`;不写 `venue_order_alive`。
- 验收:`test_venue_liveness.py::test_venue_alive_requires_order_and_position_alive`。

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
