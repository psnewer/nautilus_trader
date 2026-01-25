# CLAUDE.md - 开发准则

## 项目概览

本项目基于 NautilusTrader 构建跨市场套利系统，详细需求和架构设计请参考项目文档。

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 需求说明 | [docs/project/requirements.md](docs/project/requirements.md) | 系统功能需求定义 |
| 架构设计 | [docs/project/architecture.md](docs/project/architecture.md) | 微服务架构、事件定义、API设计、目录结构 |
| 数据库设计 | [docs/project/database-schema.md](docs/project/database-schema.md) | PostgreSQL表设计、Redis数据结构 |
| NautilusTrader | [docs/project/NautilusTrader.md](docs/project/NautilusTrader.md) | 框架说明、组件职责、适配器开发 |

## 🤝 工作协作规范

主AI（Claude Code）- 规划者 + 执行者 + 最终决策者
支持AI（Codex） - 审查者 + 测试验证者 （审查测试流程详见AGENTS.md）

## AI输出目录

<project>/.claude/
    ├── context-initial.json        ← 上下文收集输出
    └── operations-log.md           ← 突发或意外情况下的决策记录 (Claude 输出)


## 开发准则

### 整体局部原则

设计与编码工作遵循整体局部原则：

| 层级 | 文档位置 | 作用 |
|------|----------|------|
| **整体** | `docs/project/*.md` | 系统级需求、架构、数据库设计 |
| **局部** | `src/*/requirements.md`、`src/*/architeture.md` | 模块级需求和设计 |

在分析各模块下的需求或设计文件时，应联系 `docs/project/` 下的整体说明文件和关联模块下的各说明文件。

### 需求驱动准则

开发流程均由需求触发，遵循以下流程：

```
需求定义 → 架构设计 → 架构审查 → 编码 → 代码审查 → 测试验证
```

## 🚀 标准工作流程

标准工作流如下述阶段，主AI可根据每个阶段的输入输出决定各个阶段的执行与否

**阶段1：需求理解**
主AI分析需求，识别疑问

**阶段2：上下文收集**
主AI调用上下文收集subAgent收集关联context、设计、代码，必要时可联网搜索

**阶段3：深入分析**
主AI使用sequential-thinking 工具梳理问题

**阶段4：任务规划**
主AI使用shrimp-task-manager 规划任务

**阶段5：执行任务**
主AI执行任务

**阶段6：审查验证**
执行的任务成果交由支持AI(Codex)审查

**阶段7：执行和审查验证迭代**
阶段5执行任务和阶段6审查验证循环执行直至审查通过


## 主AI (Claude) 和 支持AI (Codex) 沟通准则
相同对话主题下，应使用相同threadId (SESSION_ID)，不应开启新会话


## 测试协作原则
测试用例先由主AI(Claude)产生，交由支持AI(Codex)执行测试，Codex执行测试前需检查测试用例并补充完善，Codex编写测试脚本并执行。