# Execution 测试说明

## execution-3.6: 订单级全 venue 市价提交边界

- **前置**:`PlaceBetsAction.market` 分别缺失/`false`/`true`，Strategy 提交带原计划价的
  NT `LimitOrder`，订单 metadata 对应缺失/假/真。
- **输入**:PM BUY/SELL、OE BACK/LAY、SE BACK/LAY。
- **步骤**:订单照常经过 Strategy、Risk 和 opportunity barrier，最后进入各
  ExecutionClient 的服务端提交函数。
- **期望**:缺失/`false` 时保留原限价；`true` 时 PM 使用官方 `MarketOrderArgs` + FOK，BUY amount
  为计划 `share×price` 成本、SELL amount 为计划 share，BUY 将签名后的 base quantity
  回写 NT 订单；OE/SE 最终 payload 使用 BACK `1.01`，LAY 使用执行时 NT book 的最差
  可成交赔率，缺深度回退计划价。Strategy 不读取深度、不改计划价。
- **验收**:`test_polymarket_client.py::test_pm_market_metadata_uses_official_market_order_at_submit_boundary`、
  `test_market_price.py`、OE/SE `_place_via_executor_market_lay_uses_worst_book_price`、
  `test_execution_translation.py` 的 OE/SE market payload 参数化用例及
  `test_action_place_bets.py::test_strategy_keeps_planned_price_for_execution_adapter`。

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
- 输入:触发 `arb_group_timeout:submit:{opportunity_id}`。
- 期望:pending leg 不进 ExecutionClient,本地发 `OrderDenied`,pair gate 释放。
- 验收:`test_barrier_timeout_blocks_pending_leg_and_releases_pair_gate` 覆盖。

### execution-3.5.5: all risk-pass 后若同 pair 有 residual,整次 opportunity cancel-only
- 前置:两条 `SubmitOrder` 带同一 `opportunity_id`;同一 `pair_id` 的任一 registered instrument 有 residual open order。
- 输入:两条订单均经 Risk pass 回到 barrier。
- 期望:barrier 不 release 任一新 submit 到 PM/OE ExecutionClient;PM residual 进入 tracked cancel;PM/OE 两条新 submit 都收到本地 deny/reject。
- 验收:不会出现“PM 撤旧、OE 同轮又开新单”的半边执行;live 验收锚点为 `Opportunity cancel-only: residual open orders present`。
- 状态:✅ `test_engine_barrier.py::test_barrier_cancel_only_blocks_all_new_submits_when_residual_exists`

### execution-3.5.5a: 单腿 opportunity 也按 pair-wide 范围检查 residual
- 前置:本轮 opportunity 的 `expected_legs` 只有一条 PM leg;`PairRegistry.instrument_ids_for_pair(pair_id)` 返回同 pair 的其它 PM/OE instruments;其中一个非 expected instrument 有 residual open order。
- 输入:该单腿订单经 Risk pass 回到 barrier。
- 期望:barrier 仍发现同 pair 其它 instrument 的 residual,整次 opportunity cancel-only,不 release 新 submit。
- 验收:`test_engine_barrier.py::test_barrier_residual_check_is_pair_wide_even_for_single_leg_opportunity` 覆盖。

### execution-3.5.6: grouped CancelOrder 收齐后才统一 release
- 前置:同一 pair 有两条 open order，Strategy 生成两条
  `CancelOrder` 携带相同 `opportunity_id/expected_cancels`。
- 输入:第一条先进入 ExecutionEngine，第二条随后进入。
- 期望:第一条到达时任何 ExecutionClient 均未收到撤单；第二条到达后两条标准
  `CancelOrder` 按组内顺序 release。
- 状态:✅ `test_engine_barrier.py::test_cancel_barrier_waits_until_all_commands_before_release`

### execution-3.5.6a:Submit/Cancel 复用同一 group registry 与全局闸
- 前置:一个 submit group 已在共享 registry 中等待 sibling。
- 输入:新的 grouped CancelOrder 依次到达。
- 期望:cancel group 作为“已有其它 execution”被整组拒绝；不存在第二套 cancel registry。
- 状态:✅ `test_engine_barrier.py::test_cancel_group_is_blocked_by_pending_submit_group`

### execution-3.5.6b: grouped CancelOrder busy/timeout fail-closed
- 输入:组首条进入时已有 execution session，或只到达部分命令直到 barrier timeout。
- 期望:不调用 venue；已到达命令收到标准 `OrderCancelRejected`，订单保持 NT 事件管道
  解析出的状态。
- 状态:✅ `test_cancel_barrier_rejects_group_when_other_execution_is_active` /
  `test_cancel_barrier_timeout_rejects_arrived_commands`

### execution-3.5.7: evaluation 窗口内 pair orders/positions 变化则整组拒绝
- 前置:所有腿携带相同 `positions_digest`（#317:open_orders_digest 已删）,且已全部 Risk pass。
- 输入:barrier release 前重算出的任一 pair-wide digest 与基线不同。
- 期望:不向任何 venue ExecutionClient release；所有暂存腿生成本地 `OrderDenied`。
- 验收:✅ `test_engine_barrier.py::test_barrier_denies_all_legs_when_pair_open_orders_changed`、
  `test_barrier_denies_all_legs_when_pair_positions_changed`

### execution-3.5.8:缺失 position baseline 时 fail-closed
- 前置:旧格式 opportunity 缺少 `arb:positions_digest`（#317:open_orders_digest 校验已删）。
- 输入:所有 expected legs 已收齐。
- 期望:整组拒绝，不为兼容旧 metadata 绕过窗口校验。
- 验收:✅ `test_engine_barrier.py::test_barrier_denies_legacy_opportunity_without_open_orders_baseline`、
  `test_barrier_denies_legacy_opportunity_without_positions_baseline`

### execution-3.5.9:同 opportunity 各腿 position baseline 不一致时 fail-closed
- 前置:两条 risk-pass 腿携带不同 `positions_digest`。
- 输入:第二条腿进入 barrier。
- 期望:立即整组拒绝，不使用首腿或末腿摘要覆盖另一条。
- 验收:✅ `test_engine_barrier.py::test_barrier_denies_opportunity_when_leg_positions_digests_differ`

## 异常路径置位保证(#105 ①②,已落地代码)

对应设计:execution §4.1(submit 异常收口 PM/OE 对称)+ §4.2(watchdog 与 per-pair 计数原子)。
目标:**只要执行进入了一笔 session,无论成功/失败/异常/超时,pair in-flight 一定有出口被清**。

### execution-4.1.4a: PM HTTP 拒绝立即终止，传输未知保持 in-flight
- 输入:PM POST 分别抛出 `PolyApiException(status_code=400)` 与
  `PolyApiException(status_code=None)`。
- 期望:HTTP 400 生成 `OrderRejected`，由既有 session terminal 立即收口；无 HTTP 响应时
  不生成终态，订单保持 `SUBMITTED` 等待 NT in-flight check。
- 验收:✅ `test_polymarket_client.py::test_polymarket_http_submit_rejection_is_not_ambiguous` /
  `test_polymarket_transport_submit_failure_remains_ambiguous`。

### execution-4.1.4b: PM 外部 taker fill 先建 external order
- 输入:PM USER `CONFIRMED` 成交没有本进程 `client_order_id`。
- 期望:先送 `OrderStatusReport(ACCEPTED)`，再送真实 `FillReport`，使 NT 标准 reconcile 能建立
  external order 并把成交应用到 Position。
- 验收:✅ `test_polymarket_client.py::test_polymarket_external_taker_fill_bootstraps_order_before_fill`。

### execution-4.1.4c: PM USER WS 枚举外状态统一拒单
- 输入:`event_type=order/trade` 的 USER WS 消息携带不属于对应官方枚举的 `status`。
- 期望:严格 decoder 的 `ValidationError` 转入未知状态处理；按 order id 找到本地订单并生成
  `OrderRejected`，不区分 `enable_timeout`，由标准 terminal 路径结束 session。已知枚举值不被
  重分类；网络无响应、断线和空 HTTP 回执仍属于传输结果未知。
- 验收:`test_polymarket_unknown_user_ws_status_generates_order_rejected` /
  `test_polymarket_known_user_ws_status_is_not_reclassified` /
  `test_polymarket_ws_decoder_routes_validation_error_to_unknown_status_handler` /
  `test_session.py::test_rejected_is_terminal`。

### execution-4.1.4c: in-flight QueryOrder 不改变 venue liveness
- 输入:PM 单次 `get_order` 成功/失败，或 OE/SE 强制 reload 成功/失败。
- 期望:查询与订单更新行为保持不变；任何分支都不调用 order/position
  `mark_*_dead/mark_*_alive`。venue liveness 继续由 WS 与启动/周期 reconciliation 更新。
- 验收:✅ PM `test_arb_inflight_query_updates_order_without_changing_liveness` /
  `test_arb_inflight_query_failure_does_not_change_liveness_or_session`；OE/SE
  `test_query_order_forces_reload_without_pushing_reports` /
  `test_query_order_reload_failure_does_not_change_liveness`。

### execution-4.1.5: OE submit 异常 → 立刻 rejected + 结束 session(对齐 PM,#105 ①)
- 前置:`_begin_session` 通过(submit+track),注入会抛异常的 `_place_via_executor`。
- 输入:`_submit_order` 执行,placement await 抛 `TimeoutError`(Playwright 崩)。
- 期望:`generate_order_rejected`(reason 含 "exception before venue acknowledgement")+ `_end_session`
  立刻调用;不生成 `OrderAccepted`;不干等 §4.2 watchdog 整个超时。
- 验收:`test_orbitexch_client.py::test_submit_order_exception_rejects_and_ends_session`。

### execution-4.2.2: watchdog 先 arm,异常不留半建立 session(#261 改写)
- 前置:注入会抛的 `set_time_alert_ns`(`_begin_order_session` 内唯一可能抛的操作)。
- 期望:session 不得半建立 —— `_active_sessions` 为空、`_execution_active` 为 False。
  #261 后 session 态**直接决定 barrier 闸**(#316 起为 per-pair `_pair_execution_active`),留一条没有看门狗
  的悬挂 session 会让本 pair 恒被挡,从此拒绝该 pair 的所有新机会。
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

### execution-4.2.5: submit enable_timeout 与 cancel ACK 独立
- 前置:submit 订单可带 `arb:enable_timeout=false`；cancel metadata 不含该字段。
- 输入:submit session 收到 `OrderAccepted`，或 cancel session 收到 venue 正常撤单响应的 ACK。
- 期望:submit 的 `OrderAccepted` 仍先正常上送，accepted 余额 hook 保持原顺序（PM no-op，
  OE/SE 本地预扣）；随后取消 watchdog、
  清对应 session 并释放 `_execution_active`。cancel ACK 同样只结束 session，不伪造或修改
  订单状态；后续真实成交/撤单事件继续走 NT 标准事件管道。
- 对照:submit 字段缺失/true 时继续等 submit 终态或 timeout；cancel 不读取该字段，正常响应
  一律结束 session，传输结果未知不 ACK。
- 验收:✅ `test_session.py::test_disabled_timeout_ends_session_on_accepted` /
  `test_disabled_timeout_ack_only_ends_own_execution_client_session` /
  `test_enabled_timeout_keeps_session_active_on_accepted` /
  `test_accepted_keeps_session_active` /
  `test_normal_cancel_response_ends_cancel_session`。

### execution-4.2.5a: cancel session 不继承原订单 enable_timeout
- 前置:原订单 tags 的 `enable_timeout` 分别为缺失/false/true。
- 期望:三种情况下 cancel session 都不读取原订单参数；正常撤单响应 ACK 均立即结束 session。
- 验收:`test_session.py::test_cancel_session_does_not_inherit_original_order_enable_timeout`。

### execution-4.2.4: cancel session 不把成交当撤单响应
- 前置:一笔 cancel session 已建立(qty=10)。
- 输入:先收到该订单的 `OrderFilled`(即使打满),再收到 `OrderCanceled`。
- 期望:成交不结束 cancel session(残量语义:只等撤单终态);只有
  `OrderCanceled` / `OrderCancelRejected` / timeout 能结束 cancel session。
- 验收:`test_session.py::test_cancel_session_ignores_fill_until_cancel_terminal`。

### execution-4.2.4a: cancel_order 同步建 session
- 前置:stub NT 基类 `cancel_order` 记录下发。
- 输入:显式 grouped/普通 `CancelOrder` 从 ExecutionEngine release 到 client。
- 期望:`cancel_order` 返回时 cancel session 已存在，adapter 收到
  `arb_cancel_session_started=True`，不得在异步 `_cancel_order` 中重复建 session。
- 验收:`test_session.py::test_cancel_track_marks_execution_active_before_dispatch`。

### execution-4.2.5: cancel-only 残单撤单进入同一 watchdog / exec_count
- 前置:strategy 已持有 pair in-flight;同 pair 有两条 residual open order。
- 输入:cancel-only 发出两条残单撤单请求；正常响应由 ACK 收口；测试替身不产生 ACK 时，随后以两条 `OrderCanceled` 收口。
- 期望:每条 session 分别由正常响应 ACK、cancel terminal 或 watchdog 结束；仍有任一 session 在飞时 `_execution_active` 保持 True。
- 验收:`test_session.py::test_base_cancel_only_tracks_residual_until_cancel_terminal` /
  `test_base_cancel_only_marks_residual_pending_cancel_before_venue_io` /
  `test_orbitexch_client.py::test_cancel_residual_tracked_keeps_execution_active_until_all_terminal` /
  `test_cancel_residual_execution_active_held_until_last_cancel`。

### execution-4.2.5a: cancel-only 对齐 PENDING_CANCEL 与 UNKNOWN reject 恢复

- 前置:残单为 `ACCEPTED`，barrier/per-client cancel-only 绕过 `Strategy.cancel_order` 直接撤单。
- 输入:共用 residual cancel 入口发起撤单；随后 inflight QueryOrder 没有返回有效 report。
- 期望:venue IO 前先发布 `OrderPendingCancel` 并把 Cache 订单置为 `PENDING_CANCEL`；再次遇到该残单时不重复发 venue cancel；只发送一次 inflight `QueryOrder`，下个 threshold 仍无有效报告时生成 `OrderCancelRejected(reason=UNKNOWN)`、恢复撤单前的 `ACCEPTED/PARTIALLY_FILLED` 并清理 tracking，使下一轮 cancel-only 可以再次撤。
- 验收:`test_session.py::test_base_cancel_only_marks_residual_pending_cancel_before_venue_io` / `test_engine_barrier.py::test_pending_cancel_inflight_failure_rejects_cancel_and_restores_open_state`。

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
- 验收:`test_engine_barrier.py::test_stale_order_report_batch_is_discarded_at_engine_boundary` 证明远端查询成功后即使本地批次 stale 也标活；OE 实时帧另由 `test_on_current_bets_marks_oe_liveness_alive` 覆盖。

### execution-4.5.2: position reconcile 成功置 position_alive
- 前置:该 venue `position_alive=false`。
- 输入:position reconcile 成功,拿到完整真实 response。
- 期望:`venue_position_alive[venue]=true`;不写 `venue_order_alive`。
- 验收:`test_engine_barrier.py::test_stale_position_report_batch_is_failed_at_engine_boundary` 与 `test_venue_liveness.py::test_venue_alive_requires_order_and_position_alive`。

### execution-4.5.3: reconcile 失败 fail-closed
- 前置:venue 当前 alive。
- 输入:order reconcile 超时/失败或未拿到完整 order response。
- 期望:`venue_order_alive[venue]=false`;position 失败同理只置 `venue_position_alive=false`。
- 验收:`test_engine_barrier.py::test_periodic_order_query_exception_marks_only_order_dead` / `test_periodic_position_query_exception_marks_only_position_dead`；启动 mass-status 整体失败与部分失败分别见 `test_session.py::test_generate_mass_status_none_marks_both_dimensions_dead` / `test_generate_mass_status_partial_failure_marks_each_dimension_independently`。

### execution-4.5.3a: order 批量查询失败不推进 missing-order
- 前置:本地 cache 存在该 venue 的 open/inflight 订单，周期 open-order reconcile 已发起。
- 输入:该 venue 的 `generate_order_status_reports` 抛异常；其他 venue 可正常返回。
- 步骤:聚合各 client 的查询结果，再进入 NT missing-at-venue 比较。
- 期望:失败 venue 只置 `order_alive=false`；其本地 open/inflight client order id 进入保护集，不增加
  `open_check_missing_retries`，不触发单订单查询，也不生成 `OrderRejected` / `OrderCanceled`。
  查询成功并真实返回 `[]` 时仍按既有 missing-order 语义处理。
- 验收:`tests/arbitrage/execution/test_engine_barrier.py::test_periodic_order_query_exception_marks_only_order_dead`。

### execution-4.5.3b: 外部挂单导入建立完整 client/account 归属
- 前置:启动或周期对账返回 cache 中不存在的 open order；report 可缺 `account_id`，但 instrument venue
  已注册既有 ExecutionClient routing。
- 输入:状态为 `ACCEPTED` 的 `OrderStatusReport`，包含 client/venue order id。
- 步骤:按 report account 或既有 venue/default routing 解析报告来源，生成 external order 并应用 accepted。
- 期望:report 与 order 的 `account_id` 均为报告来源 client 的账户；cache 同时建立
  `client_order_id → client_id` 与 `venue_order_id → client_order_id` 索引。后续 QueryOrder/CancelOrder
  可路由到原 client，不依赖历史不完整订单兼容逻辑。
- 验收:`tests/arbitrage/execution/test_engine_barrier.py::test_external_open_order_reconciliation_indexes_account_and_execution_client`。

### execution-4.5.4: 普通 submit 不主动置 false
- 前置:venue order/position 均 alive。
- 输入:普通 submit+track session 开始。
- 期望:不因“每次下单”把 alive 置 false;session 生命周期仍由 `PairInFlightGate` + watchdog 管。
- 验收:只有 stuck/reconcile failure 等真相不可信路径才置 false。

### execution-4.5.8: PM reconcile 纯 NET 快照,不拉 trades API(#279)
- 背景:重启后 PM 历史持仓(母单已成交、不在 cache);启动 `generate_fill_reports` 拉 trades 超时抛异常曾连坐掀翻整个 mass-status → 权威 position 被丢弃 → cache flat → recovery 误发双开(实盘 nohup 取证)。设计见 execution architecture §4.3bis (5d) / refactor #279。
- 输入:调用 `ArbPolymarketExecutionClient.generate_fill_reports`;上游 `PolymarketExecutionClient.generate_fill_reports` 被 monkeypatch 成"一调用即抛"。
- 期望:返回 `[]`,且**不触达上游**(不抛)。position 对账由 `_reconcile_position_report_netting` 凭 `/positions` 权威净仓 NET 合成,不需要真 fill;PM live 成交仍由 USER WS trade 累加,不受影响。
- 验收:`test_polymarket_client.py::test_arb_generate_fill_reports_returns_empty_without_trades_api`。

### execution-4.5.8a: 周期 position reconcile 原地修复零开仓均价(#339)
- 前置:同一 instrument 恰有一个本地 open Position，`avg_px_open <= 0`；venue report 的非零 signed quantity 与本地完全相等，且 `avg_px_open > 0`。
- 输入:执行真实周期入口 `_process_cached_position_discrepancies`，而非只调用底层 reconcile helper。
- 期望:原 Position 的 `avg_px_open` 直接更新为 venue 值；quantity、realized PnL 与 Position 身份不变，不生成 EXTERNAL order、fill 或开平仓事件。已有正均价不一致、多个本地 Position、零仓或 venue 均价无效时不走该修复。
- 验收:`tests/unit_tests/live/test_execution_recon.py::TestReconciliationEdgeCases::test_position_reconciliation_repairs_zero_avg_px_when_quantity_matches`；setter 边界由 `tests/unit_tests/model/test_position.py::TestPosition::test_set_avg_px_open_updates_in_place` 锁定。

### execution-4.5.8b: order reconcile 禁止用无效均价补成交(#374)
- 前置:本地订单累计成交量小于 venue `OrderStatusReport.filled_qty`,需要合成缺失成交。
- 输入:`report.avg_px` 分别为 `None`、`0`;即使限价 `report.price` 有效也不作为成交价替代。
- 期望:该订单对账返回失败,不生成 inferred `OrderFilled`,不改变本地 `filled_qty`、order/position 或 inferred-fill tracking,等待后续 reconcile 拿到有效均价再重试。
- 边界:若 report 与本地 `filled_qty` 已相等,无需补成交,所以 `avg_px=None` 不阻塞既有订单状态对账。
- 验收:`tests/unit_tests/live/test_execution_recon.py::test_handle_fill_quantity_mismatch_rejects_missing_fill_without_valid_avg_px`、`test_handle_fill_quantity_mismatch_ignores_avg_px_when_no_fill_is_missing`。

### execution-4.5.9: 全 venue reconcile 应用前乐观并发校验(#308;#318 per-pair)
- 前置:PM/OE/SE report 请求发出前按 **instrument 分格**记录本账户 order/position 摘要
  (`{instrument → digest}`)。**#318**:order 摘要只含 order、position 摘要只含 position(含 realized_pnl);
  **无 realized_revision**。
- 输入:远端请求等待或多 venue gather 期间，WS Fill 或其他标准事件改变某 pair 的 order/position/realized。
- 期望:adapter 返回携带分格摘要的 `GuardedReports`(摘要附到每份 report);ExecutionEngine 查询正常返回时先标记 alive,
  再**逐 pair**在应用前识别失效并只丢该 pair,不改变 liveness。
- 引擎边界(**per-pair**):stale pair 的 order 不参与 reconcile 且其本地 open/inflight ids 视为已报告(空批凭本地单判);
  stale pair 的 position 令该 venue 进 failed_venues;mass-status 不再整批 abort、交 super 逐 report per-pair 过滤;
  单份 QueryOrder report 按其 pair 复核。deferred realized payload 的 instrument 即使没有 PositionReport
  也按 scope 复核；realized offset 只对通过的 instrument 选择性 commit。
- 正交性:远端查询成功由启动/周期 reconciliation 上层标记对应 order/position alive；摘要过期只影响 report 应用，
  不把 venue 改回 dead。有效 position batch 在应用前提交 deferred payload，空 report batch 也不例外。
- 最终入口:连续 report 在 `_reconcile_order_report/_reconcile_position_report` 再校验；空 position
  batch 派生的 flat report 继承 venue 摘要；deferred realized 提交后重取应用阶段摘要，不能因自身
  revision 变化误拒同批 position reports。
- 验收:各 adapter README #308 所列用例；摘要字段变化由
  `tests/arbitrage/common/test_{open_orders,positions}.py` 覆盖，引擎应用边界由
  `test_engine_barrier.py::test_stale_*_at_engine_boundary`、
  `test_valid_position_report_batch_commits_deferred_payload` 与
  `test_position_batch_{validates_and_commits,skips_stale}_closed_only_realized_instrument`、
  `test_mass_status_commits_closed_only_realized_instrument_after_validation`、
  `test_deferred_realized_commit_refreshes_position_application_snapshot`、
  `test_stale_single_order_report_is_discarded_at_apply_boundary`、
  `test_flat_position_report_inherits_empty_batch_snapshot` 覆盖；startup mass report 携带
  guarded batches 由 `test_session.py::test_generate_mass_status_carries_guarded_report_batches` 覆盖。

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
