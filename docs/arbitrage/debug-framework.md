# Debug 测试框架

## 概述

Debug 测试框架允许在实盘环境中测试订单执行流程，同时确保订单不会实际成交。提供两种机制：

### 1. 变量覆盖 (DebugManager)
- 强制触发套利机会（即使市场没有）
- 以极端价格下单（确保不成交）
- 控制订单规模（最小化风险）
- 跳过实际执行（干跑模式）

### 2. 测试数据注入 (TestDataManager)
- 注入虚假订单、持仓数据
- 模拟赔率和套利机会
- 模拟执行结果（成功/失败）
- 可配置的条件匹配

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                     DebugManager (单例)                      │
│                                                             │
│  ┌─────────────────┐    ┌─────────────────────────────┐    │
│  │  DebugConfig    │    │  DebugOverride (多个)        │    │
│  │  - enabled      │    │  - min_rebate_rate          │    │
│  │  - verbose      │    │  - polymarket_price         │    │
│  │  - overrides    │───>│  - orbitexch_price          │    │
│  │                 │    │  - polymarket_size          │    │
│  │                 │    │  - orbitexch_size           │    │
│  │                 │    │  - skip_execution           │    │
│  └─────────────────┘    │  - ...                      │    │
│                         └─────────────────────────────┘    │
│                                                             │
│  方法:                                                       │
│  - enable() / disable()                                     │
│  - set_override(name, enabled, value)                       │
│  - get_override(name, default) -> 覆盖值或默认值             │
│  - is_override_active(name) -> bool                         │
└─────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────┐          ┌─────────────────────┐
│ ExecutionService│          │   StrategyService   │
│                 │          │                     │
│ _apply_debug_   │          │ 检查 min_rebate_rate│
│  overrides()    │          │ 覆盖                │
└─────────────────┘          └─────────────────────┘
```

## 使用方式

### 1. 配置文件 (debug_config.json)

```json
{
  "enabled": true,
  "overrides": {
    "min_rebate_rate": {
      "enabled": true,
      "value": -10.0
    },
    "polymarket_price": {
      "enabled": true,
      "value": 0.01
    },
    "orbitexch_price": {
      "enabled": true,
      "value": 10.0
    },
    "polymarket_size": {
      "enabled": true,
      "value": 5
    },
    "orbitexch_size": {
      "enabled": true,
      "value": 10.0
    }
  },
  "mock_data": {}
}
```

### 2. 代码中使用

```python
from src.arbitrage.services.debug import debug_manager

# 加载配置
debug_manager.load("debug_config.json")

# 启用/禁用
debug_manager.enable()
debug_manager.disable()

# 获取覆盖值
price = debug_manager.get_override("polymarket_price", default=real_price)

# 检查是否激活
if debug_manager.is_override_active("skip_execution"):
    # 跳过执行逻辑
    pass

# 设置覆盖
debug_manager.set_override("polymarket_price", enabled=True, value=0.01)
```

## 可用覆盖

### 策略相关 (Strategy)

| 名称 | 描述 | 默认值 | 用途 |
|------|------|--------|------|
| `strategy_name` | 覆盖默认策略名 | "default" | 测试时切换策略 (如 max-rebate) |
| `min_rebate_rate` | 最小返水率 | -10.0 | 设为负数强制触发套利 |
| `force_opportunity` | 强制生成机会 | true | 忽略赔率计算 |

### 执行相关 (Execution)

| 名称 | 描述 | 默认值 | 用途 |
|------|------|--------|------|
| `polymarket_price` | Polymarket 价格 | 0.01 | 1% 概率，不会成交 |
| `orbitexch_price` | OrbitExch 价格 | 10.0 | 赔率 10（10%概率），正常范围但远离市场价 |
| `polymarket_size` | Polymarket 订单大小 | 5 | 测试用 share 数量 |
| `orbitexch_size` | OrbitExch 订单大小 | 10.0 | 测试用 stake 金额 |
| `order_size` | 通用订单金额（旧） | 1.0 | 兼容旧配置 |
| `skip_execution` | 跳过执行 | true | 不发送订单 |
| `execution_delay` | 执行延迟 (秒) | 5.0 | 观察订单流程 |

## 订单元数据

当 debug 模式启用时，订单的 `metadata` 会包含以下字段：

```json
{
    "debug_mode": true,
    "debug_overrides": {
        "price": 0.55,
        "size": 100.0
    },
    "debug_skipped": true,
    "size_multiplier": 1.036
}
```

## 安全提示

⚠️ **警告**: Debug 模式仅用于测试环境。在生产环境中请确保：

1. 理解每个覆盖的影响
2. 使用最小金额测试
3. 测试完成后立即禁用
4. 检查订单元数据确认是否为测试订单

## 典型测试配置

### safe_test (安全测试)

```json
{
  "enabled": true,
  "overrides": {
    "polymarket_price": {"enabled": true, "value": 0.01},
    "orbitexch_price": {"enabled": true, "value": 10.0},
    "polymarket_size": {"enabled": true, "value": 5},
    "orbitexch_size": {"enabled": true, "value": 10}
  }
}
```

**用途**: 测试完整订单流程，订单会发送到交易所但不会成交。

### force_trigger (强制触发)

```json
{
  "enabled": true,
  "overrides": {
    "min_rebate_rate": {"enabled": true, "value": -10.0},
    "polymarket_price": {"enabled": true, "value": 0.01},
    "orbitexch_price": {"enabled": true, "value": 10.0},
    "polymarket_size": {"enabled": true, "value": 5},
    "orbitexch_size": {"enabled": true, "value": 10}
  }
}
```

**用途**: 即使市场没有套利机会也强制触发，测试策略触发逻辑。

### dry_run (干跑)

```json
{
  "enabled": true,
  "overrides": {
    "min_rebate_rate": {"enabled": true, "value": -10.0},
    "skip_execution": {"enabled": true, "value": true}
  }
}
```

**用途**: 不发送订单，仅记录流程，用于测试策略逻辑。

---

# 测试数据注入 (MockDataManager)

## 概述

MockDataManager 作为 DebugConfig 的一部分，提供灵活的测试数据注入机制。

## 数据分类 (MockCategory)

| 分类 | 说明 | 用途 |
|------|------|------|
| `odds` | 赔率数据 | 模拟市场赔率 |
| `orders` | 订单数据 | 模拟挂单列表 |
| `positions` | 持仓数据 | 模拟持仓状态 |
| `opportunities` | 套利机会 | 模拟套利信号 |
| `execution` | 执行结果 | 模拟下单结果 |
| `market` | 市场数据 | 模拟市场状态 |
| `account` | 账户数据 | 模拟余额等 |
| `timeline` | 订单状态时序 | 模拟部分成交/拒单/撤单 |

## 使用方式

```python
from src.arbitrage.services.debug import debug_manager, MockCategory

# 添加模拟数据
debug_manager.add_mock(
    item_id="mock_orders",
    category=MockCategory.ORDERS,
    name="测试订单",
    data=[{"order_id": "test-001", "status": "live"}],
    enabled=True,
    conditions={"venue": "polymarket"},
)

# 获取模拟数据
orders = debug_manager.get_mock_data(
    MockCategory.ORDERS,
    context={"venue": "polymarket"}
)

# 或使用默认值
orders = debug_manager.get_or_default(
    MockCategory.ORDERS,
    default=fetch_real_orders(),
    context={"venue": "polymarket"}
)
```

## 配置文件格式

```json
{
  "enabled": true,
  "overrides": { ... },
  "mock_data": {
    "mock_item_id": {
      "category": "orders",
      "name": "描述",
      "enabled": true,
      "data": { ... },
      "conditions": {"venue": "polymarket"},
      "priority": 0
    }
  }
}
```

## 条件匹配

当多个数据项匹配时，返回优先级最高的：

```json
{
  "general_orders": {
    "priority": 0,
    "conditions": {}
  },
  "specific_orders": {
    "priority": 10,
    "conditions": {"venue": "polymarket"}
  }
}
```

---

# 订单状态时序模拟 (Timeline)

## 概述

Timeline 功能允许在 Live 测试中模拟真实订单状态转换时序，如部分成交、拒单、撤单等。
当 `skip_execution=true` 且配置了 `mock_data.timeline` 时生效。

## 事件类型

| 事件 | 说明 |
|------|------|
| `ACCEPT` | 订单被接受 |
| `PARTIAL_FILL` | 部分成交，`fill_pct` 指定填充百分比 |
| `FILL` | 全部成交（或填充剩余部分） |
| `REJECT` | 拒单，`reject_reason` 指定原因 |
| `CANCEL` | 撤单，`cancel_reason` 指定原因 |
| `EXPIRE` | 订单过期 |

## 配置格式

```json
{
  "mock_data": {
    "se_partial_fill": {
      "category": "timeline",
      "name": "SE 部分成交测试",
      "enabled": true,
      "conditions": {"venue": "sharpexch"},
      "data": {
        "steps": [
          {"event": "ACCEPT", "delay_ms": 100},
          {"event": "PARTIAL_FILL", "delay_ms": 500, "fill_pct": 0.5},
          {"event": "FILL", "delay_ms": 1000}
        ]
      }
    },
    "pm_reject": {
      "category": "timeline",
      "name": "PM 拒单测试",
      "enabled": true,
      "conditions": {"venue": "polymarket"},
      "data": {
        "steps": [
          {"event": "ACCEPT", "delay_ms": 100},
          {"event": "REJECT", "delay_ms": 2000, "reject_reason": "INSUFFICIENT_FUNDS"}
        ]
      }
    }
  }
}
```

## 步骤参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `event` | string | 事件类型（必填） |
| `delay_ms` | int | 事件触发前延迟毫秒数（默认 0） |
| `fill_pct` | float | PARTIAL_FILL 时填充百分比 0-1（默认 1.0） |
| `reject_reason` | string | REJECT 时拒单原因 |
| `cancel_reason` | string | CANCEL 时撤单原因 |

## 条件匹配

Timeline 配置支持按以下上下文字段匹配：

| 字段 | 说明 |
|------|------|
| `venue` | 交易所（小写），如 "polymarket", "orbitexch", "sharpexch" |
| `instrument_id` | 完整 instrument ID |
| `order_side` | 订单方向 "BUY" / "SELL" |
| `order_type` | 订单类型 "LIMIT" / "MARKET" |

## 使用示例

测试 SE 订单延迟 2 秒后部分成交 30%，再延迟 3 秒全部成交：

```json
{
  "enabled": true,
  "overrides": {
    "skip_execution": {"enabled": true, "value": true}
  },
  "mock_data": {
    "se_slow_fill": {
      "category": "timeline",
      "enabled": true,
      "conditions": {"venue": "sharpexch"},
      "data": {
        "steps": [
          {"event": "ACCEPT", "delay_ms": 500},
          {"event": "PARTIAL_FILL", "delay_ms": 2000, "fill_pct": 0.3},
          {"event": "FILL", "delay_ms": 3000}
        ]
      }
    }
  }
}
```

---

# 场景测试框架 (TestScenario)

## 概述

`src/arbitrage/testing/` 是一套声明式的场景化实盘/模拟盘测试框架。一个测试场景包含：
- 部分覆盖 `debug_config.json` 默认值
- 切换策略（也作为参数）
- 通过日志驱动的退出条件（成功 / 失败 / 超时）
- 自动监控运行时日志，结果落 `test_runs/<timestamp>_<name>.json`

## 核心组件

| 组件 | 路径 | 作用 |
|------|------|------|
| `TestScenario` | `testing/scenario.py` | 声明式描述一次测试 |
| `LogMonitor` | `testing/monitor.py` | 注册 logging.Handler，捕获所有日志 |
| 退出条件原语 | `testing/conditions.py` | `LogMatch` / `AllOf` / `AnyOf` / `Sequence` / `Negate` |
| `ScenarioRunner` | `testing/runner.py` | 启动 web_gateway, 评估退出, 出报告 |

## 退出条件原语

| 原语 | 说明 |
|------|------|
| `LogMatch(logger=None, level=None, contains=None, pattern=None)` | 匹配单行日志 |
| `AllOf(*conds)` | 所有子条件都满足（顺序无关） |
| `AnyOf(*conds)` | 任一子条件满足 |
| `Sequence(*conds)` | 顺序匹配（A 满足后才考虑 B） |
| `Negate(cond)` | 反向（用于"不应出现"，配合超时使用） |

## 场景定义

```python
from src.arbitrage.testing.conditions import AllOf, AnyOf, LogMatch, Sequence
from src.arbitrage.testing.scenario import TestScenario


class PlaceAndCancelScenario(TestScenario):
    name = "place_and_cancel"
    description = "下单 -> 撤单 -> 退出"

    # 部分覆盖 debug_config（缺省字段保持默认）
    debug_overrides = {
        "polymarket_price": (True, 0.01),
        "orbitexch_price":  (True, 100.0),
        "polymarket_size":  (True, 5),
        "orbitexch_size":   (True, 7),
        "min_rebate_rate":  (True, -10.0),
    }

    # 切策略 == debug_overrides["strategy_name"]
    strategy = "max-rebate"

    # 成功：两边下单 -> 两边撤单
    success = Sequence(
        AllOf(
            LogMatch(logger="PolymarketExecutor", level="INFO", contains="Order placed"),
            LogMatch(logger="OrbitExchExecutor",  level="INFO", contains="Order placed"),
        ),
        AllOf(
            LogMatch(logger="PolymarketExecutor", level="INFO", contains="Order cancelled"),
            LogMatch(logger="OrbitExchExecutor",  level="INFO", contains="Order cancelled"),
        ),
    )

    # 失败：任一 executor ERROR
    failure = AnyOf(
        LogMatch(logger="PolymarketExecutor", level="ERROR", contains="Failed to place order"),
        LogMatch(logger="OrbitExchExecutor",  level="ERROR", contains="Failed to place order"),
    )

    timeout_sec = 300.0
```

## 运行

```bash
python -m src.arbitrage.testing --scenario place_and_cancel
# 可选: --debug-config / --host / --port / --report-dir
```

退出码:
| 码 | 含义 |
|----|------|
| 0 | PASS |
| 1 | FAIL |
| 2 | TIMEOUT |

## 报告

每次运行落到 `test_runs/<timestamp>_<name>.json`，包含:
- 场景元信息、outcome、耗时
- 应用了哪些 debug 覆盖
- 成功 / 失败条件树（命中事件）
- 关键事件（命中事件 + 所有 WARNING/ERROR 日志）

终端会同时打印一份精简版供肉眼查看。

