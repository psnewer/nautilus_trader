# settlement 测试

对应章节: `refactor.md §5.8`;详细设计 `architectures/execution/architecture.md §4.6`

**落地状态(2026-07-28)**:`PolymarketSettlement` 已实现于 `nautilus_trader/adapters/polymarket/settlement.py`(编排;IO 为同目录 `contract.py`)。离线用例覆盖 merge min 取量、negRisk、redeem、失败/no-op；宿主调度、realized PnL 对账与 liveness 验收见 PM adapter README。

## 锁定的关键性约束(Q18 + Q18b 修正,2026-05-21)

- merge / claim(redeem)是**链上 CTF 合约操作**,**上游 NT PM ExecutionClient 没有也无法表达**(只包 CLOB 订单簿)。本工程自研保留(`contract.py:PolymarketContractService`)。
- 链上 IO 走 Polymarket 官方 collateral adapter:标准二元 target=`CtfCollateralAdapter`,negRisk target=`NegRiskCtfCollateralAdapter`,collateral 参数为 pUSD。旧的直接 CTF+USDC.e 路径会让资金落到 pending deposit/Activate Funds,不能直接恢复 CLOB buying power。
- 归属/触发(**#110 修正,2026-06-16;推翻 Q18b 的"PM 健康检查 tick"**): **并入 NT 连续 position 对账**(`LiveExecEngineConfig.position_check_interval_secs=300`),复用对账那次 `/positions` 拉取。**PM 无 HealthCheckLoop**(健康检查彻底退役,对齐 OE #109);**无独立 Actor、无独立调度**。
- **三层宿主(Q18c;#110 触发改 NT 对账)**:
  - 宿主/触发 = **PM `ExecutionClient` 薄子类**的 `generate_position_status_reports`(NT 周期调它):第一次 `/positions` 的 raw stash 喂结算；尝试过 merge 后，无论成功失败，同轮再拉 `/positions`，只向 NT 返回第二次 reports。
  - 编排 = **`PolymarketSettlement` 普通类**(`nautilus_trader/adapters/polymarket/settlement.py`;非 ExecutionClient 方法)
  - IO = **`contract.py:PolymarketContractService`**(保留)
  - **结算 await + single-flight(#283)**:report 协程 await `_run_settlement(raw)` 以判断是否需要
    重拉 positions；同步链上 SDK 已由 `contract.py` 丢线程池，因此不会阻塞 NT app loop。
    `_settlement_inflight` 仅防并发重复提交。
- 结果不作健康判据: `TxResult` 失败仅 log + 下个对账周期重试,**不影响** `VenueExecutionLiveness`。成功 merge 不计算 realized PnL、不伪造 OrderFilled。
- CLOB buying-power cache 同步当前默认关闭:切到 collateral adapter+pUSD 后,PM ExecClient 不主动调用 `update_balance_allowance(COLLATERAL)`。保留宿主 helper 便于 live 证明需要时恢复;本目录只测编排,宿主行为见 PM adapter README。
- 数据源: **对账那次 PM Data API `/positions` 原始响应**(含
  `redeemable`/`negativeRisk`/`conditionId`/`size`);**不能用 NT cache
  持仓**(上游翻成 `PositionStatusReport` 时丢了 redeemable 等)。映射 =
  `pm_raw_position_to_settlement(item: dict)`。
- 与执行**不互斥**(#110):结算等待只挂起 report 协程，不阻塞 app loop；并发由
  single-flight 守卫，不靠健康检查互斥。
- redeem 结算滞后: 由 NT position 对账周期(300s)兜住(每周期检查 `redeemable`),无需事件。
- 本目录测的是 **merge/redeem 决策逻辑 + contract IO**;**调度/在 tick 内被调用/结果不作健康判据** 的用例见 `tests/arbitrage/adapters/polymarket/README.md` 健康检查段。

## 预期用例(摘要)

### settlement-8.2: merge —— 同 condition 两 outcome 都持仓
- 前置: Data API `/positions` 返回某 condition 下 token_0 size=80、token_1 size=50,`neg_risk=false`
- 输入: 健康检查 tick 触发
- 期望: 调 `contract.merge_positions(condition_id, amount=50, neg_risk=false)`(amount=min)
- 验收: amount 取两腿最小;negRisk 标志透传;只对 ≥2 outcome 的 condition 触发

### settlement-8.3: merge —— negRisk 市场走 NegRiskCtfCollateralAdapter
- 前置: 某 condition `neg_risk=true`,两 outcome 都持仓
- 输入: 健康检查 tick
- 期望: 调 `merge_positions(..., neg_risk=true)` → `NegRiskCtfCollateralAdapter` target + inherited `mergePositions(address,bytes32,bytes32,uint256[],uint256)` ABI
- 验收: 与标准二元(CTF)路径区分正确

### settlement-8.4: redeem —— redeemable 门控
- 前置: 某 condition 持仓,Data API `redeemable=true`
- 输入: 健康检查 tick
- 期望: 调 `redeem_positions(condition_id, neg_risk)`；negRisk 只切换 collateral adapter target，不传 outcome amounts，adapter 在链上读取调用者当前 YES/NO balances
- 验收: 仅 `redeemable=true` 才 redeem;`redeemable=false` 跳过；negRisk calldata 使用 inherited `redeemPositions(address,bytes32,bytes32,uint256[])` selector，不得使用旧两参数 selector

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
- 输入:同一轮 position reconcile 在 merge 返回成功后继续执行
- 期望:宿主重拉 `/positions`，再拉 `/closed-positions`；最终 reports 走 NT 原生 reconcile
- 验收: merge/redeem 路径不调 `msgbus.publish` / 不写 NT Position cache / 不造 OrderFilled

### settlement-8.9: 链上调用不阻塞 event loop(2026-06-21)
- 前置:`contract.py` 的 `RelayClient.execute` / `resp.wait()` 是**同步阻塞**调用；position
  report 协程会 await settlement
- 输入: `merge_positions` / `redeem_positions` 跑期间,并发一个每 10ms tick 的心跳协程
- 期望: 阻塞调用经 `loop.run_in_executor(None, ...)` 丢线程池;阻塞那 ~0.6s 里心跳持续推进(>10 次)
- 验收:心跳计数远超 1(loop 未被冻)；若退回直接同步调用，心跳会被饿死。文件
  `test_contract_offload.py`。

### settlement-8.10: 标准 merge 走 CtfCollateralAdapter + pUSD(2026-07-10)
- 前置: 标准二元 condition 两 outcome 都持仓
- 输入: `contract.merge_positions(condition_id, amount, neg_risk=false)`
- 期望: SafeTransaction `to=CtfCollateralAdapter(0xAdA100...)`,calldata 内 collateral 为 pUSD `0xC011...DFB`
- 验收: `test_contract_offload.py::test_standard_merge_uses_ctf_collateral_adapter_and_pusd`

### settlement-8.11: negRisk merge 走 NegRiskCtfCollateralAdapter(2026-07-10)
- 前置: negRisk condition 两 outcome 都持仓
- 输入: `contract.merge_positions(condition_id, amount, neg_risk=true)`
- 期望: SafeTransaction `to=NegRiskCtfCollateralAdapter(0xadA200...)`,calldata 使用 inherited collateral-adapter merge selector 并包含 pUSD
- 验收: `test_contract_offload.py::test_neg_risk_merge_uses_neg_risk_ctf_collateral_adapter`

### settlement-8.12: negRisk redeem 使用 inherited collateral-adapter ABI(2026-07-16)
- 前置: Data API 返回 negRisk condition `redeemable=true`
- 输入: `contract.redeem_positions(condition_id, neg_risk=true)`
- 期望: target 为 `NegRiskCtfCollateralAdapter`,calldata 使用 `redeemPositions(address,bytes32,bytes32,uint256[])`;合约自行读取调用者 YES/NO balances
- 验收: `test_contract_offload.py::test_neg_risk_redeem_uses_inherited_collateral_adapter_abi`

### settlement-8.13: merge 不另造 realized PnL(2026-07-28)

- 前置:同 condition 两 outcome 已在 NT cache，settlement 成功 merge。
- 输入:同一轮 position reconcile 发现并成功执行 merge。
- 期望:settlement 不写 Portfolio/ledger；宿主重拉 `/positions` 后才返回 reports，随后拉
  `/closed-positions` 更新 Data API realized 基线。
- 验收:代码中无 `MergeRealization` / condition adjustment；PM adapter realized reconcile
  用例覆盖 `positions → merge → positions → closed positions` 顺序。

## Debug 相关

Debug `skip_settlement`(§6.6,P10):健康检查路径不真正上链 / mock `TxResult`。归 `tests/arbitrage/debug/`。

## 没有的用例

- ~~session-complete 事件触发 cleanup~~ —— 改为并入 PM 健康检查 tick(Q18b)
- ~~`PostSessionCleanup` / `PolymarketSettlementActor` 外壳~~ —— 编排逻辑平移进 PM 健康检查路径,旧外壳删除
