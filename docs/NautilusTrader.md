# NautilusTrader 平台文档

## 项目概述

NautilusTrader 是一个高性能算法交易平台，支持回测和实盘交易。它采用 Python/Rust 混合代码库，性能关键组件使用 Rust 编写，通过 PyO3 和 Cython 提供 Python 绑定。

---

## 构建命令

```bash
# 安装所有依赖（发布模式）
make install

# 修改 Rust/Cython 代码后构建（推荐开发使用）
make build-debug

# 运行 Python 测试
make pytest

# 运行 Rust 测试（需要 cargo-nextest）
make cargo-test

# 运行单个 Python 测试文件
uv run --active --no-sync pytest tests/unit_tests/path/to/test_file.py

# 运行单个 Python 测试
uv run --active --no-sync pytest tests/unit_tests/path/to/test_file.py::test_name -v

# 运行单个 Rust crate 测试
make cargo-test-crate-nautilus-model

# 运行带特定功能的 Rust crate 测试
make cargo-test-crate-nautilus-infrastructure FEATURES="redis,postgres"

# 代码质量检查（clippy + ruff）
make check-code

# 格式化 Rust 代码
make format

# 对所有文件运行 pre-commit hooks
make pre-commit

# PR 前的完整预检查
make pre-flight

# 安装所需开发工具
make install-tools

# 查看所有可用的 make 目标
make help
```

---

## 基础设施服务（用于集成测试）

```bash
# 首次设置：启动容器并初始化数据库
make init-services

# 停止服务（保留数据）
make stop-services

# 恢复服务
make start-services

# 清除所有内容包括卷
make purge-services
```

**服务配置**：
- PostgreSQL: localhost:5432, 用户: nautilus, 密码: pass, 数据库: nautilus
- Redis: localhost:6379

---

## 架构

### 双语言结构

| 层级 | 路径 | 说明 |
|------|------|------|
| **Python 包** | `nautilus_trader/` | 用户接口 API、策略开发、适配器 |
| **Rust crates** | `crates/` | 核心引擎、性能关键组件，通过 PyO3 暴露 |

### 核心组件（Python 和 Rust 镜像）

| 组件 | 功能 |
|------|------|
| `core` | 基础类型（UUID、datetime、正确性断言） |
| `model` | 领域模型（instruments、orders、positions、events、Price、Quantity、Money） |
| `common` | 共享工具（logging、clock、message bus、cache、actors） |
| `data` | 数据处理和聚合 |
| `execution` | 订单管理和执行 |
| `portfolio` | 投资组合和持仓跟踪 |
| `risk` | 风险管理 |
| `backtest` | 回测引擎 |
| `live` | 实盘交易基础设施 |
| `persistence` | 数据存储（Parquet、catalog） |
| `serialization` | Arrow/MessagePack 序列化 |

### 适配器模式

交易所集成位于：
- Python: `nautilus_trader/adapters/`
- Rust: `crates/adapters/`

每个适配器将交易所特定 API 转换为 NautilusTrader 的统一接口。

### 事件驱动架构

系统使用事件驱动消息总线（`MessageBus`），组件通过类型化事件通信。策略继承自 `Strategy` 并实现事件处理器：
- `on_start`
- `on_data`
- `on_event`
- 等等

### 精度模式

| 模式 | 平台 | 位数 | 小数位 |
|------|------|------|--------|
| **高精度**（默认） | Linux/macOS | 128位整数 | 最多16位 |
| **标准精度** | Windows | 64位整数 | 最多9位 |

---

## 关键模式

### Python/Rust 互操作

- Rust 代码通过 `crates/pyo3/` 中的 PyO3 暴露 Python 绑定
- Cython（`.pyx`）文件提供额外的 Python 绑定
- 修改 Rust/Cython 代码后，运行 `make build-debug` 重新编译

### 配置

配置使用 `msgspec` 进行验证。配置类继承自 `nautilus_trader/config/` 中的基础配置。

### 测试

| 类型 | 路径 |
|------|------|
| 单元测试 | `tests/unit_tests/` |
| 集成测试 | `tests/integration_tests/` |
| 性能测试 | `tests/performance_tests/` |

Rust 测试通过 `cargo nextest` 和 `nextest` profile 运行。

---

## 重要文件

| 文件 | 说明 |
|------|------|
| `build.py` | Cython/Rust 编译的自定义构建脚本 |
| `pyproject.toml` | Python 依赖和工具配置 |
| `Cargo.toml` | Rust 工作区配置和工作区 lints |
| `Makefile` | 开发自动化（运行 `make help` 查看所有目标） |

---

## 贡献说明

- PR 目标是 `develop` 分支（默认分支）
- 提交前安装并运行 pre-commit hooks：`pre-commit install`
- 新交易所集成需要事先讨论（参见 ROADMAP.md）
