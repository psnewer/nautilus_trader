# Execution 服务测试报告

## 执行信息

- 执行命令：`pytest tests/arbitrage/services/execution/test_execution_service.py -q`
- 执行结果：通过
- 用例数：24
- 通过数：24
- 失败数：0

## 用例结果

- 用例 1：未初始化执行订单 — 通过
- 用例 1 结果：返回失败，message=Service not initialized
- 用例 2：跳过执行覆盖 — 通过
- 用例 2 结果：订单状态为 submitted，debug_skipped=true
- 用例 3：模拟交易所全成交 — 通过
- 用例 3 结果：订单状态为 filled，活跃订单列表为空
- 用例 4：模拟交易所拒单 — 通过
- 用例 4 结果：订单状态为 rejected
- 用例 5：撤单不存在订单 — 通过
- 用例 5 结果：返回失败，message=Order not found
- 用例 6：市价成交不存在订单 — 通过
- 用例 6 结果：返回失败，message=Order not found
- 用例 7：批量撤单仅处理活跃订单 — 通过
- 用例 7 结果：撤单成功，活跃订单列表为空
- 用例 8：OrbitExch 批量撤单只影响 OrbitExch 订单 — 通过
- 用例 8 结果：OrbitExch 订单被撤，Polymarket 仍保留活跃
- 用例 9：初始轮按“订单全部成交”结束 — 通过
- 用例 9 结果：会话结束 reason=target_met，成交与目标可不一致
- 用例 10：初始轮零成交结束 — 通过
- 用例 10 结果：会话结束 reason=zero_fill
- 用例 11：补救循环撤单后再规划 — 通过
- 用例 11 结果：撤单后重新规划并完成，reason=target_met
- 用例 12：规划后新成交触发重新规划 — 通过
- 用例 12 结果：检测新成交后跳过本轮操作并重新规划（planning>=2，执行次数=1）
- 用例 13：同一 pair 存在活跃会话时拒绝新机会 — 通过
- 用例 13 结果：execute_opportunity 返回 None
- 用例 14：补救按平均返水 — 通过
- 用例 14 结果：is_mean_rebate=true
- 用例 15：补救最小下单金额 — 通过
- 用例 15 结果：目标 share 与追加 share 符合最小 base 规则
- 用例 16：超时后刷新确认下单 — 通过
- 用例 16 结果：订单状态确认为成功
- 用例 17：超时后刷新判定撤单失败 — 通过
- 用例 17 结果：订单状态判定为失败
- 用例 18：OrbitExch 超时后刷新确认下单成功 — 通过
- 用例 18 结果：订单状态确认为成功
- 用例 19：OrbitExch 超时后刷新判定下单失败 — 通过
- 用例 19 结果：订单状态判定为失败
- 用例 20：OrbitExch 超时后刷新确认撤单成功 — 通过
- 用例 20 结果：撤单状态确认为成功
- 用例 21：OrbitExch 超时后刷新判定撤单失败 — 通过
- 用例 21 结果：撤单状态判定为失败
- 用例 22：OrbitExch 超时后刷新确认修改成功 — 通过
- 用例 22 结果：修改状态确认为成功
- 用例 23：OrbitExch 超时后刷新判定修改失败 — 通过
- 用例 23 结果：修改状态判定为失败
- 用例 24：超出失败重试次数 — 通过
- 用例 24 结果：会话结束 reason=max_failure_retries
