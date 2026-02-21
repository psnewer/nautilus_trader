# Execution 服务测试用例

说明：覆盖订单执行服务的主要路径与异常分支，包含执行、撤单、补救与会话互斥。

用例 1：未初始化执行订单  
前置条件：ExecutionService 未 initialize  
步骤：调用 execute_order  
预期：返回失败，message=Service not initialized

用例 2：跳过执行覆盖  
前置条件：debug skip_execution 启用  
步骤：调用 execute_order  
预期：返回 success，订单状态为 submitted，标记 debug_skipped

用例 3：模拟交易所全成交  
前置条件：debug use_mock_exchange 启用；mock 执行计划为立即 filled  
步骤：执行订单并等待异步更新  
预期：订单状态为 filled；活跃订单列表为空

用例 4：模拟交易所拒单  
前置条件：debug use_mock_exchange 启用；mock 执行计划为 reject  
步骤：执行订单  
预期：返回失败，订单状态为 rejected

用例 5：撤单不存在订单  
前置条件：无任何订单  
步骤：调用 cancel_order  
预期：返回失败，message=Order not found

用例 6：市价成交不存在订单  
前置条件：无任何订单  
步骤：调用 take_remaining_at_market  
预期：返回失败，message=Order not found

用例 7：批量撤单仅处理活跃订单  
前置条件：mock 订单保持活跃  
步骤：执行订单后调用 cancel_all_orders  
预期：撤单成功；活跃订单列表为空

用例 8：OrbitExch 批量撤单只影响 OrbitExch 订单  
前置条件：mock 下单一笔 Polymarket + 一笔 OrbitExch，均为活跃  
步骤：调用 cancel_all_orbitexch_unmatched  
预期：OrbitExch 订单被撤；Polymarket 仍在活跃列表

用例 9：初始轮按“订单全部成交”结束  
前置条件：初始轮为 OrbitExch 下单，成交量达到操作 size  
步骤：执行机会  
预期：会话结束 reason=target_met；成交量可与目标不一致

用例 10：初始轮零成交结束  
前置条件：初始轮成交为 0  
步骤：执行机会  
预期：会话结束 reason=zero_fill

用例 11：补救循环撤单后再规划  
前置条件：初始下单部分成交并存在挂单  
步骤：执行机会  
预期：先撤单，再生成新规划并完成；会话结束 reason=target_met

用例 12：规划后新成交触发重新规划  
前置条件：规划后刷新成交有变化  
步骤：执行机会  
预期：跳过本轮操作并重新规划；至少两次规划且只执行一次操作

用例 13：同一 pair 存在活跃会话时拒绝新机会  
前置条件：pair 已有活跃 session  
步骤：再调用 execute_opportunity  
预期：返回 None

用例 14：补救按平均返水  
前置条件：给定概率与部分成交  
步骤：计算补救目标并验证平均返水  
预期：is_mean_rebate=true

用例 15：补救最小下单金额  
前置条件：给定概率与部分成交  
步骤：计算补救目标与追加下单  
预期：目标 share 与追加 share 符合最小 base 规则

用例 16：超时后刷新确认下单  
前置条件：无 WebSocket 反馈，刷新接口返回新增订单  
步骤：执行追踪并等待超时  
预期：订单状态确认为成功

用例 17：超时后刷新判定撤单失败  
前置条件：无 WebSocket 反馈，刷新接口仍存在订单  
步骤：执行追踪并等待超时  
预期：订单状态判定为失败

用例 18：OrbitExch 超时后刷新确认下单成功  
前置条件：无 WebSocket 反馈，刷新页面返回新增 bet  
步骤：执行追踪并等待超时  
预期：订单状态确认为成功

用例 19：OrbitExch 超时后刷新判定下单失败  
前置条件：无 WebSocket 反馈，刷新页面无新增 bet  
步骤：执行追踪并等待超时  
预期：订单状态判定为失败

用例 20：OrbitExch 超时后刷新确认撤单成功  
前置条件：无 WebSocket 反馈，刷新页面 bet 已消失  
步骤：执行追踪并等待超时  
预期：撤单状态确认为成功

用例 21：OrbitExch 超时后刷新判定撤单失败  
前置条件：无 WebSocket 反馈，刷新页面 bet 仍存在  
步骤：执行追踪并等待超时  
预期：撤单状态判定为失败

用例 22：OrbitExch 超时后刷新确认修改成功  
前置条件：无 WebSocket 反馈，刷新页面 bet 成交量增加  
步骤：执行追踪并等待超时  
预期：修改状态确认为成功

用例 23：OrbitExch 超时后刷新判定修改失败  
前置条件：无 WebSocket 反馈，刷新页面 bet 成交量不变  
步骤：执行追踪并等待超时  
预期：修改状态判定为失败

用例 24：超出失败重试次数  
前置条件：max_failure_retries=1，补救阶段出现失败  
步骤：执行机会  
预期：会话结束 reason=max_failure_retries
