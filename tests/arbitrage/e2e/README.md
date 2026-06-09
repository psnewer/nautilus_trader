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
- e2e-2: 当前主流程闭环:mean_rebate 下一轮机会重新 submit 时,execution session 入口发现同 instrument 未成交残单 → cancel-only 撤残单并丢弃本次 submit(`test_mean_rebate_cancel_only.py`)
- e2e-3: 单腿成交另一腿失败时的专门 recovery 状态机(后议,不属于当前主流程闭环)
- e2e-4: 启动重连 reconciliation(Cache 状态与 venue 一致)
- e2e-5: 多 MatchedPair 并发处理
