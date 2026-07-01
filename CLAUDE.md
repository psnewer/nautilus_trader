# CLAUDE.md

To reduce common LLM coding mistakes, please follow the @karpathy-skills guidelines.

## 项目概览

本项目基于 NautilusTrader 构建跨市场套利系统。目前主链路先实现 Polymarket 和 OrbitExch 之间的体育赛事套利交易;SharpExch 正按 OE 型 venue 做第一阶段接入,尚未完成实盘 Data/Execution 接线。

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 迁移初设 | [docs/arbitrage/refactor.md](docs/arbitrage/refactor.md) | NT 迁移初设 + 决策史(Q1–Q20)+ 修订记录(**先读这个**了解为什么) |
| 架构总览 | [docs/arbitrage/architecture.md](docs/arbitrage/architecture.md) | NT 端态架构总览 + 组件导航 |
| 组件详细设计 | [docs/arbitrage/architectures/](docs/arbitrage/architectures/) | 分组件详细设计(接口/数据流/算法/时序),面向代码落地 |
| 需求说明 | [docs/arbitrage/requirements.md](docs/arbitrage/requirements.md) | 系统功能需求(行为真理源;按旧服务名组织) |
| 数据库设计 | [docs/arbitrage/database-schema.md](docs/arbitrage/database-schema.md) | PostgreSQL表设计、Redis数据结构 |
| NautilusTrader | [docs/arbitrage/NautilusTrader.md](docs/arbitrage/NautilusTrader.md) | 框架说明、组件职责、适配器开发 |
| 测试架构设计 | [docs/arbitrage/debug-framework.md](docs/arbitrage/debug-framework.md) | 实盘测试、模拟盘测试架构说明 |

## 设计文档分层(本项目落位,对接全局 `design-docs` 方法论)

设计/架构工作遵循全局 `design-docs` skill 的分层方法论(决策日志 / 详细设计 / 需求 / 测试,单一真理源,跨组件机制按"无单一归属"判据单独成章)。本项目的落位:

| 层 | 落位 |
|---|---|
| 决策日志(为什么 + 定了什么 + 指针) | `docs/arbitrage/refactor.md`(Q1–Q20 + 修订记录,单一真理源仅限"理由/历史") |
| 详细设计(是什么/怎么做,设计真理源) | `docs/arbitrage/architecture.md`(总览)+ `docs/arbitrage/architectures/<组件>/architecture.md` |
| 需求(行为真理源) | `docs/arbitrage/requirements/...`(注:按旧服务名组织,语义仍有效) |
| 测试用例 | `tests/arbitrage/<capability>/README.md` |

做这类工作前先调 `design-docs` skill。冲突时:详细设计有把握 → 以详细设计为准并回写 refactor.md 修订记录;没把握 → 讨论。
