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
