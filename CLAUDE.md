# CLAUDE.md

To reduce common LLM coding mistakes, please follow the @karpathy-skills guidelines.

## 项目概览

本项目基于 NautilusTrader 构建跨市场套利系统。目前，先实现polymarket和orbitexch平台之间的体育赛事套利交易。

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 需求说明 | [docs/arbitrage/requirements.md](docs/arbitrage/requirements.md) | 系统功能需求定义 |
| 架构设计 | [docs/arbitrage/architecture.md](docs/arbitrage/architecture.md) | 微服务架构、事件定义、API设计 |
| 数据库设计 | [docs/arbitrage/database-schema.md](docs/arbitrage/database-schema.md) | PostgreSQL表设计、Redis数据结构 |
| NautilusTrader | [docs/arbitrage/NautilusTrader.md](docs/arbitrage/NautilusTrader.md) | 框架说明、组件职责、适配器开发 |
| 测试架构设计 | [docs/arbitrage/debug-framework.md](docs/arbitrage/debug-framework.md) | 实盘测试、模拟盘测试架构说明 |
