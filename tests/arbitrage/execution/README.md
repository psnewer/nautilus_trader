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

### execution-3.5.7: evaluation 窗口内 pair open orders 变化则整组拒绝
- 前置:所有腿携带相同 `open_orders_digest`,且已全部 Risk pass。
- 输入:barrier release 前重算出的 pair-wide digest 与基线不同。
- 期望:不向任何 venue ExecutionClient release；所有暂存腿生成本地 `OrderDenied`。
- 验收:✅ `test_engine_barrier.py::test_barrier_denies_all_legs_when_pair_open_orders_changed`

### execution-3.5.8:缺失 open-order baseline 时 fail-closed
- 前置:旧格式 opportunity 没有 `arb:open_orders_digest`。
- 输入:所有 expected legs 已收齐。
- 期望:整组拒绝，不为兼容旧 metadata 绕过窗口校验。
- 验收:✅ `test_engine_barrier.py::test_barrier_denies_legacy_opportunity_without_open_orders_baseline`

## 异常路径置位保证(#105 ①②,已落地代码)

对应设计:execution §4.1(submit 异常收口 PM/OE 对称)+ §4.2(watchdog 与 per-pair 计数原子)。
目标:**只要执行进入了一笔 session,无论成功/失败/异常/超时,pair in-flight 一定有出口被清**。

### execution-4.1.5: OE submit 异常 → 立刻 rejected + 结束 session(对齐 PM,#105 ①)
- 前置:`_begin_session` 通过(submit+track),注入会抛异常的 `_place_via_executor`。
- 输入:`_submit_order` 执行,placement await 抛 `TimeoutError`(Playwright 崩)。
- 期望:`generate_order_rejected`(reason 含 "exception before venue acknowledgement")+ `_end_session`
  立刻调用;不生成 `OrderAccepted`;不干等 §4.2 watchdog 整个超时。
- 验收:`test_orbitexch_client.py::test_submit_order_exception_rejects_and_ends_session`。

### execution-4.2.2: watchdog 先 arm,异常不留半建立 session(#261 改写)
- 前置:注入会抛的 `set_time_alert_ns`(`_begin_order_session` 内唯一可能抛的操作)。
- 期望:session 不得半建立 —— `_active_sessions` 为空、`_execution_active` 为 False。
  #261 后 `_execution_active` **直接决定 barrier 的全局闸**,留一条没有看门狗的悬挂 session
  会让它恒 True,从此拒绝所有新机会。
- 验收:`test_session.py::test_alert_failure_leaves_no_half_built_session`
  + `test_begin_session_arms_watchdog`(正常路径:watchdog 已 arm)。

### execution-4.2.4: submit_order 同步建 session(#261 承重前提)
- 前置:stub NT 基类 `submit_order` 记录下发。
- 期望:`submit_order` 返回时 session **已存在**(未跑任何 loop 迭代),且顺序为
  先 `_begin_session` 后 dispatch;cancel-only 时不下发。
- 理由:barrier 在 `_release` 里同步派发各腿,派发与 pop ctx 之间无 `await`;session 若留到
  `_submit_order` 协程才建,派生态有空窗,`[A1,A2,B1,B2]` 会让两个机会双双执行。
- 验收:`test_session.py::test_submit_order_builds_session_synchronously`
  + `test_submit_order_cancel_only_does_not_dispatch`
  + OE/SE `test_submit_order_builds_session_before_dispatch`。

### execution-4.2.4: cancel session 只由撤单终态收口
- 前置:一笔 cancel session 已建立。
- 输入:先收到该订单的 `OrderFilled`,再收到 `OrderCanceled`。
- 期望:fill 事件不结束 cancel session;只有 `OrderCanceled` / `OrderCancelRejected` / timeout 能结束 cancel session。
- 验收:`test_session.py::test_cancel_session_ignores_fill_until_cancel_terminal`。

### execution-4.2.5: cancel-only 残单撤单进入同一 watchdog / exec_count
- 前置:strategy 已持有 pair in-flight;同 pair 有两条 residual open order。
- 输入:cancel-only 发出两条残单撤单请求,撤单 coroutine 先完成,随后两条 `OrderCanceled` 到达。
- 期望:撤单请求完成不落回 `_execution_active`;每条 cancel terminal 到齐后才落回 False(#261:撤单在飞期间 barrier 不放行新机会)。
- 验收:`test_session.py::test_base_cancel_only_tracks_residual_until_cancel_terminal` / `test_orbitexch_client.py::test_cancel_residual_tracked_clears_inflight_when_all_done` / `test_cancel_residual_inflight_held_until_last_cancel`。

## VenueExecutionLiveness 写入(已落地代码路径,2026-06-15)

对应设计:`docs/arbitrage/architectures/_cross-cutting/synchronization.md §8.5` + execution §4.3bis/§4.4。

### execution-4.3bis.1: OE connect 等待 BALANCE + CURRENT_BETS
- 前置:OE ExecutionClient 已在导航前注册 general WS handler，并创建两个业务 future。
- 输入:导航/登录期间 general WS 依次推送 `BALANCE` 与 `CURRENT_BETS`。
- 期望:`_connect` 最多等待 30s;两者到齐时使用真实余额,不先生成 0 余额;若超时缺余额才生成 0 USD 兜底,缺 CURRENT_BETS 后续由 reconcile reload 自愈。
- 验收:`test_connect_ready_waits_for_balance_and_current_bets_signals` 覆盖等待中收齐；`test_connect_ready_consumes_signals_received_before_wait_starts` 覆盖登录期间早到后立即返回。

### execution-4.3bis.1a:删除未接入 NT 的 OE 页面执行能力
- 前置:OE 正常逐单撤单与 `CancelAllOrders` API 路径保留。
- 输入:批量撤单 API 返回失败，或调用方检查旧 take/modify 方法。
- 期望:不点击 `Cancel All Unmatched` 页面控件兜底；executor 不再暴露 `take_remaining_at_market` / `modify_size_and_take`，旧 services HTTP `take-at-market` 入口与 MODIFY planner/tracker 分支一并删除。
- 验收:`test_execution_translation.py::test_cancel_all_unmatched_api_failure_has_no_ui_fallback` / `test_oe_executor_does_not_expose_legacy_take_at_market`。

### execution-4.3bis.2: OE snapshot fresh 必须已有 CURRENT_BETS
- 前置:OE execution WS 已有任意帧,但 `_last_current_bets_ns==0`。
- 输入:NT reconciliation 调 `_ensure_exec_snapshot_fresh()`。
- 期望:不能仅凭 SockJS open/心跳/PROPERTIES 判 snapshot fresh;必须 reload execution 页并等 CURRENT_BETS 重推。
- 验收:`tests/arbitrage/execution/test_orbitexch_client.py::test_ensure_fresh_reloads_when_ws_fresh_but_no_current_bets`。

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

## AccountState accepted 本地预扣(Q17 修订,已落地;#254 起仅 OE/SE)

对应设计:execution §4.5 + risk §3.1。AccountState 统一表达可用余额快照:
`total = free = available`,`locked = 0`。accepted 后不请求 venue,只按订单本身本地预扣并写
`generate_account_state`;后续真实余额帧/账户查询可覆盖该估算。

**#254:PM 关闭 accepted 预扣** —— `ArbPolymarketExecutionClient` 覆盖
`_reserve_available_balance_for_accepted_order` 为 no-op(taker 单 accepted 后——现由
MATCHED/MINED/CONFIRMED 任一先到达 ack,见 execution architecture §3.1 #256——数秒内即
CONFIRMED,NT fill 增量记账及时扣减,预扣叠加成双扣且 PM 无推送源要等 ~5min reconcile 纠正)。
mixin 通用路径与 `order_required_balance` 公式不动(本节 4.5.5-4.5.7 的 mixin 级用例继续锁定
OE/SE 语义与公式契约,PM instrument 用例锁定的是 mixin/公式层,真实 PM client 不再触发);
PM 侧验收:`test_polymarket_client.py::test_arb_pm_accepted_reserve_is_noop`。

### execution-4.5.5: accepted 后按 venue capability 本地预扣余额
- 前置:任意 tradable venue account cache 已有 `free=100 USD`;订单进入 submit session 并收到 `OrderAccepted`。
- 输入:
  - PM/probability venue:`quantity=50`,`price=0.20`。
  - OE/SE/decimal venue:`quantity=12`,`price=1.80`。
  - OE/SE decimal LAY:`quantity=10`,`price=5.00`。
- 期望:PM BUY、PM SELL、decimal BACK/LAY 均按 Venue Registry
  `order_required_balance` 的结果更新 free；本节只验证 Execution 正确消费该契约，公式唯一真理源见
  `architectures/_cross-cutting/venues.md §4.1`。
- 验收:
  - 预扣逻辑挂在 `ArbExecutionSessionMixin._send_order_event()` 的 `OrderAccepted` 处理后,三方 ExecutionClient 不各自写重复逻辑。
  - 资金需求委托 Venue Registry `order_required_balance`,不写死 venue 或 side 分支。
  - accepted 预扣不发外部 HTTP/WS 请求。
  - 已由 `tests/arbitrage/execution/test_session.py::test_accepted_reserves_probability_venue_available_balance` / `test_accepted_reserves_decimal_venue_available_balance_without_fx` / `test_accepted_reserves_decimal_lay_liability` / `test_accepted_reserves_sharpexch_available_balance_without_fx` / `test_accepted_order_reserved_notional_uses_venue_capability` 覆盖。

### execution-4.5.6: accepted 预扣后真实余额更新可覆盖本地估算

### execution-4.5.7: probability SELL accepted 零预扣(#233)

- `test_accepted_probability_sell_reduction_does_not_reserve_cash` 与 helper 断言锁定 PM SELL 不降低 cached free；BUY、decimal BACK/LAY 公式保持原样。
- 前置:accepted 本地预扣已把账户 `free=88`。
- 输入:随后 venue 真值来源到达:PM 显式 QueryAccount、OE WS `BALANCE`、或 SE profile/balance response。
- 期望:ExecutionClient 按真值再次 `generate_account_state`,覆盖本地估算。
- 验收:本地预扣只是短期保守值,不是独立 balance ledger;不维护 `reserved` 累计表。

### execution-4.5.7: 第一阶段不在 cancel terminal 加回余额
- 前置:accepted 本地预扣后,该订单被 cancel。
- 输入:收到 `OrderCanceled` / OE-SE `CURRENT_BETS` 确认撤单。
- 期望:第一阶段不主动加回余额;等待下一次 venue 真值余额更新覆盖。
- 验收:余额可能短期偏保守,但不会因本地推断和 venue 真值重复加回。若未来要加回,也必须由 execution terminal 事件驱动,不能由 Risk 倒推。
