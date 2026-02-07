# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

本项目基于 NautilusTrader 构建跨市场套利系统。NautilusTrader 是一个高性能、事件驱动的算法交易平台，核心使用 Rust 编写并通过 Cython/PyO3 暴露 Python 接口。

## 构建和开发命令

### 安装
```bash
make install           # Release模式安装（生产环境）
make install-debug     # Debug模式安装（开发环境，推荐）
make install-just-deps # 仅安装依赖，不构建包
```

### 构建
```bash
make build             # Release模式构建
make build-debug       # Debug模式构建（修改Rust/Cython代码后推荐使用）
```

### 测试
```bash
# Python测试
make pytest                              # 并行运行所有Python测试
uv run pytest tests/unit_tests/ -x       # 运行单元测试，首个失败即停止
uv run pytest tests/unit_tests/test_xxx.py::test_func -v  # 运行单个测试

# Rust测试
make cargo-test                          # 运行所有Rust测试
make cargo-test-crate-nautilus-model     # 测试特定crate
cargo nextest run -p nautilus-model --lib  # 直接使用nextest
```

### 代码质量
```bash
make pre-commit        # 运行所有pre-commit检查
make check-code        # 运行clippy和ruff
make ruff              # Python linter (自动修复)
cargo +nightly fmt     # Rust格式化
```

### 开发服务（集成测试）
```bash
make init-services     # 启动Docker服务并初始化数据库
make start-services    # 启动服务（不重新初始化）
make stop-services     # 停止服务
make purge-services    # 清除所有服务和数据
```

## 代码架构

### 目录结构
- `nautilus_trader/` - Python包，包含Cython扩展模块
- `crates/` - Rust crates (核心逻辑)
- `src/` - 本项目自定义模块（套利系统）
- `tests/` - 测试套件 (unit_tests, integration_tests, performance_tests)

### 核心模块 (nautilus_trader/)
- `adapters/` - 交易所适配器（Binance, Polymarket等）
- `model/` - 领域模型（Instrument, Order, Position等）
- `core/` - 核心组件（Clock, UUID, 类型定义）
- `common/` - 通用组件（Actor, MessageBus, Config）
- `data/` - 数据引擎和数据客户端
- `execution/` - 执行引擎和执行客户端
- `trading/` - 策略基类和交易组件

### 适配器开发模式
新交易所适配器需实现：
- `DataClient` - 市场数据订阅（行情、订单簿）
- `ExecutionClient` - 订单执行（下单、撤单、查询）
- `InstrumentProvider` - 交易品种发现

参考: `nautilus_trader/adapters/binance/` 或 `nautilus_trader/adapters/polymarket/`

## 项目文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 需求说明 | [docs/arbitrage/requirements.md](docs/arbitrage/requirements.md) | 系统功能需求定义 |
| 架构设计 | [docs/arbitrage/architecture.md](docs/arbitrage/architecture.md) | 微服务架构、事件定义、API设计 |
| 数据库设计 | [docs/arbitrage/database-schema.md](docs/arbitrage/database-schema.md) | PostgreSQL表设计、Redis数据结构 |
| NautilusTrader | [docs/arbitrage/NautilusTrader.md](docs/arbitrage/NautilusTrader.md) | 框架说明、组件职责、适配器开发 |

---

## 工作协作规范

主AI（Claude Code）- 规划者 + 执行者 + 最终决策者
支持AI（Codex） - 审查者 + 测试验证者 （审查测试流程详见AGENTS.md）

### AI输出目录

```
.claude/
    ├── context-initial.json        ← 上下文收集输出
    └── operations-log.md           ← 突发或意外情况下的决策记录 (Claude 输出)
```

## 开发准则

### 整体局部原则

| 层级 | 文档位置 | 作用 |
|------|----------|------|
| **整体** | `docs/arbitrage/*.md` | 系统级需求、架构、数据库设计 |
| **局部** | `docs/arbitrage/requirements/services/*`、`docs/arbitrage/architectures/services/*` | 模块级需求和设计 |

在分析各模块下的需求或设计文件时，应联系 `docs/arbitrage/` 下的整体说明文件和关联模块下的各说明文件。

### 需求驱动准则

```
需求定义 → 架构设计 → 架构审查 → 编码 → 代码审查 → 测试验证
```

## 标准工作流程

**阶段1：需求理解** - 分析需求，识别疑问

**阶段2：上下文收集** - 收集关联context、设计、代码，必要时可联网搜索

**阶段3：深入分析** - 使用sequential-thinking工具梳理问题

**阶段4：任务规划** - 使用shrimp-task-manager规划任务

**阶段5：执行任务** - 执行实现

**阶段6：审查验证** - 交由支持AI(Codex)审查

**阶段7：迭代** - 阶段5和阶段6循环执行直至审查通过

## 测试协作原则

测试用例先由主AI(Claude)产生，交由支持AI(Codex)执行测试。Codex执行测试前需检查测试用例并补充完善。

## 补充工作流策略
- **上下文收集策略**（@~/.claude/workflows/context-search-strategy.md）：作为上下文收集步骤补充。
- **MCP 工具策略**（@~/.claude/workflows/mcp-tool-strategy.md）：补充每类 MCP 的触发条件、失败补救措施。