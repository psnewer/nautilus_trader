# AGENTS.md — 支持AI(Codex)开发准则

本文件面向 Codex 支持AI，定义其作为审查者的职责边界与协作规范。

## 项目概览

本项目基于 NautilusTrader 构建跨市场套利系统，详细需求和架构设计请参考项目文档。

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 需求说明 | [docs/arbitrage/requirements.md](docs/arbitrage/requirements.md) | 系统功能需求定义 |
| 架构设计 | [docs/arbitrage/architecture.md](docs/arbitrage/architecture.md) | 微服务架构、事件定义、API设计、目录结构 |
| 数据库设计 | [docs/arbitrage/database-schema.md](docs/arbitrage/database-schema.md) | PostgreSQL表设计、Redis数据结构 |
| NautilusTrader | [docs/arbitrage/NautilusTrader.md](docs/arbitrage/NautilusTrader.md) | 框架说明、组件职责、适配器开发 |
| 测试架构设计 | [docs/arbitrage/debug-framework.md](docs/arbitrage/debug-framework.md) | 实盘测试、模拟盘测试架构说明 |

## 🤝 工作协作规范

主AI（Claude Code）- 规划者 + 执行者 + 最终决策者
支持AI（Codex） - 审查者 + 审查验证者

## AI输出目录

<project>/.claude/
    └── review-report.md           ← 审查报告（Codex 输出）


## 开发准则

### 整体局部原则

设计与编码工作遵循整体局部原则：

| 层级 | 文档位置 | 作用 |
|------|----------|------|
| **整体** | `docs/arbitrage/*.md` | 系统级需求、架构、数据库设计 |
| **局部** | `src/*/requirements.md`、`src/*/architecture.md` | 模块级需求和设计 |

在分析各模块下的需求或设计文件时，应联系 `docs/arbitrage/` 下的整体说明文件和关联模块下的各说明文件。


## 🚀 标准工作流程

标准工作流如下述阶段，主AI可根据每个阶段的输入输出决定各个阶段的执行与否

**阶段1：需求理解**
主AI分析需求，识别疑问

**阶段2：上下文收集**
主AI收集关联上下文、设计、代码，必要时可联网搜索

**阶段3：深入分析**
主AI使用sequential-thinking 工具梳理问题

**阶段4：任务规划**
主AI使用shrimp-task-manager 规划任务

**阶段5：执行任务**
主AI执行任务

**阶段6：审查测试**
执行的任务成果交由支持AI(Codex)审查

**阶段7：执行和审查验证迭代**
阶段5执行任务和阶段6审查验证循环执行直至审查通过

-------------------------------------------------

## 架构审查准则

### 架构优先级

1. **标准化 + 生态复用**：必须首先查找并复用 NautilusTrader 官方 API、社区成熟方案
2. **禁止自研**：除非已有实践无法满足需求

### 框架约定

1. **事件驱动架构**：所有组件通过 MessageBus 通信
2. **Actor 模式**：自定义组件继承 `Actor` 或 `Strategy`
3. **适配器模式**：外部市场接入必须实现 `DataClient` 和 `ExecutionClient`

### 设计原则

- 严格遵循微服务准则，即微服务解耦和消息触发机制

## 架构评分准则

- 如果有违反架构审查准则中的任一原则，不得评为通过
- 若无意见，可直接通过
- 其他问题可询问是否通过


------------------------------------

## 代码审查准则

### 注释规范

- 所有文档与必要代码注释必须使用**简体中文**

### 实现标准

- 禁止在缺乏证据的情况下做出假设
- 必须主动删除过时、重复或逃生式代码

### 路径规范

- 输出地址必须在架构或需求中有严格依据，禁止自造路径

## 代码评分准则

- 如果有违反代码审查准则中的任一原则，不得评为通过
- 若无意见，可直接通过
- 其他问题可询问是否通过

---------------------------------------------------------------

## 测试准则

### 测试协作原则
测试用例先由主AI(Claude)产生，交由支持AI(Codex)执行测试，Codex执行测试前需检查测试用例并补充完善，Codex编写测试脚本并执行。

### 🧪 测试规范
- 所有改动的关联功能或环节均需重新测试
- 测试执行后需在测试用例中回填测试结果。
- 测试用例和测试报告均放在<project>/tests/<module>/下

### 自动化执行
- 测试脚本的执行无需确认

