# settlement 测试

对应章节: `refactor.md §5.8`;详细设计 `architectures/execution/architecture.md §4.6`

**落地状态(2026-05-22)**:`PolymarketSettlement` 已实现于 `src/arbitrage/settlement/settlement.py`(编排;IO 仍是 `nautilus_trader/adapters/polymarket/contract.py`)。`tests/arbitrage/settlement/test_settlement.py` **11 passed**(用 FakeContract 录调用,不上链):merge min 取量 + negRisk 透传 + ≥2 门槛(8.2/8.3)、redeem redeemable 门控 + negRisk amounts(8.4/8.5)、失败仅记录不抛 + 异常吞进 errors(8.7)、空/零持仓 no-op。**仍待**:8.6(数据源=Data API 原始而非 NT cache)与"在 tick 内被调用 / 结果不作健康判据"属 PM 健康检查宿主,跟 PM 子类落地一起验(见 pm-adapter README)。

## 锁定的关键性约束(Q18 + Q18b 修正,2026-05-21)

- merge / claim(redeem)是**链上 CTF 合约操作**,**上游 NT PM ExecutionClient 没有也无法表达**(只包 CLOB 订单簿)。本工程自研保留(`contract.py:PolymarketContractService`)。
- 归属/触发(**#110 修正,2026-06-16;推翻 Q18b 的"PM 健康检查 tick"**): **并入 NT 连续 position 对账**(`LiveExecEngineConfig.position_check_interval_secs=300`),复用对账那次 `/positions` 拉取。**PM 无 HealthCheckLoop**(健康检查彻底退役,对齐 OE #109);**无独立 Actor、无独立调度**。
- **三层宿主(Q18c;#110 触发改 NT 对账)**:
  - 宿主/触发 = **PM `ExecutionClient` 薄子类**的 `generate_position_status_reports`(NT 周期调它):上游 `_fetch_user_positions` 拉一次、stash `_last_raw_positions`;override 用 stash 喂结算。
  - 编排 = **`PolymarketSettlement` 普通类**(`src/arbitrage/settlement/settlement.py`;非 ExecutionClient 方法)
  - IO = **`contract.py:PolymarketContractService`**(保留)
  - **结算 fire-and-forget + single-flight**:`create_task(_run_settlement(raw))`,**不 `await`**(链上 tx 数秒,绝不阻塞 NT 对账循环 / inflight check);`_settlement_inflight` 守卫防并发重复提交。本目录用例针对 `PolymarketSettlement`(编排)+ contract IO;宿主触发见 pm-adapter README(pm-adapter-5.health.4)。
- 结果不作健康判据: `TxResult` 失败仅 log + 下个对账周期重试,**不影响** `VenueExecutionLiveness`。
- 数据源: **对账那次 PM Data API `/positions` 原始响应**(含 `redeemable`/`negativeRisk`/`conditionId`/`size`);**不能用 NT cache 持仓**(上游翻成 `PositionStatusReport` 时丢了 redeemable 等)。映射 = `pm_raw_position_to_settlement(item: dict)`。
- 与执行**不互斥**(#110):结算 fire-and-forget、不阻塞对账;OE 下单是 page.evaluate 与对账互不冲突(synchronization §8)。并发由 single-flight 守卫,不靠互斥。
- redeem 结算滞后: 由 NT position 对账周期(300s)兜住(每周期检查 `redeemable`),无需事件。
- 本目录测的是 **merge/redeem 决策逻辑 + contract IO**;**调度/在 tick 内被调用/结果不作健康判据** 的用例见 `tests/arbitrage/adapters/polymarket/README.md` 健康检查段。

## 预期用例(摘要)

### settlement-8.2: merge —— 同 condition 两 outcome 都持仓
- 前置: Data API `/positions` 返回某 condition 下 token_0 size=80、token_1 size=50,`neg_risk=false`
- 输入: 健康检查 tick 触发
- 期望: 调 `contract.merge_positions(condition_id, amount=50, neg_risk=false)`(amount=min)
- 验收: amount 取两腿最小;negRisk 标志透传;只对 ≥2 outcome 的 condition 触发

### settlement-8.3: merge —— negRisk 市场走 NegRiskAdapter
- 前置: 某 condition `neg_risk=true`,两 outcome 都持仓
- 输入: 健康检查 tick
- 期望: 调 `merge_positions(..., neg_risk=true)` → `NegRiskAdapter.mergePositions(bytes32,uint256)` 编码路径
- 验收: 与标准二元(CTF)路径区分正确

### settlement-8.4: redeem —— redeemable 门控
- 前置: 某 condition 持仓,Data API `redeemable=true`
- 输入: 健康检查 tick
- 期望: 调 `redeem_positions(...)`;negRisk 时传 `amounts=[各 outcome size]`,标准时不传
- 验收: 仅 `redeemable=true` 才 redeem;`redeemable=false` 跳过(不报错)

### settlement-8.5: redeem 时机 —— 结算前不触发
- 前置: 刚下完单、赛事**未结算**,Data API `redeemable=false`
- 输入: 多次 健康检查 tick
- 期望: 一直跳过 redeem,直到某次 健康检查 tick 看到 `redeemable=true` 才赎回
- 验收: 验证"健康检查周期性兜住结算滞后",不依赖任何 session-complete 事件

### settlement-8.6: 数据源必须是 Data API 原始,不是 NT cache
- 前置: NT cache 有该 condition 持仓(但无 redeemable/mergeable 字段)
- 输入: 健康检查 tick 取数据
- 期望: 健康检查 tick 拉 Data API `/positions` 原始响应取 redeemable/mergeable/neg_risk
- 验收: 静态检查 merge/redeem 路径不从 `cache.positions_open()` 读 redeemable(NT cache 没有此字段)

### settlement-8.7: 失败重试(幂等)
- 前置: 一次 `merge_positions` 返回 `TxResult(success=False)`
- 输入: 当次 健康检查 tick 结束 + 下次 健康检查 tick
- 期望: 当次仅 log warning 不抛;下次 健康检查 tick 按当时持仓重算 merge amount 重试
- 验收: 无特殊补偿逻辑;merge/redeem 天然幂等

### settlement-8.8: 结果回流不显式 publish
- 前置: 一次 merge 成功,链上持仓减少
- 输入: 下一个 PM 健康检查 tick / Data API 拉取
- 期望: 持仓经 report 通路更新 cache → `portfolio.way_rebate` 调用即反映新持仓
- 验收: merge/redeem 路径不调 `msgbus.publish` / 不写 cache;不主动触发 way_rebate 重算

## Debug 相关

Debug `skip_settlement`(§6.6,P10):健康检查路径不真正上链 / mock `TxResult`。归 `tests/arbitrage/debug/`。

## 没有的用例

- ~~session-complete 事件触发 cleanup~~ —— 改为并入 PM 健康检查 tick(Q18b)
- ~~`PostSessionCleanup` / `PolymarketSettlementActor` 外壳~~ —— 编排逻辑平移进 PM 健康检查路径,旧外壳删除
