# 订单执行服务 - 架构设计

## 概述

订单执行服务负责在 Polymarket 和 OrbitExch 平台执行订单，支持下单、撤单和市价成交。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    ExecutionService                          │
│                                                              │
│  ┌──────────────────────┐  ┌──────────────────────┐        │
│  │  PolymarketExecutor  │  │  OrbitExchExecutor   │        │
│  │                      │  │                      │        │
│  │  - py-clob-client    │  │  - Playwright Page   │        │
│  │  - L2 签名           │  │  - HTTP API          │        │
│  │  - GTC/FOK/FAK       │  │  - UI 交互           │        │
│  └──────────────────────┘  └──────────────────────┘        │
│               │                        │                    │
└───────────────┼────────────────────────┼────────────────────┘
                │                        │
                ▼                        ▼
        ┌───────────────┐      ┌───────────────────┐
        │   Polymarket  │      │    OrbitExch      │
        │   CLOB API    │      │   Browser Page    │
        └───────────────┘      └───────────────────┘
```

## 模块说明

### 1. ExecutionService (`service.py`)

主服务，提供统一的订单执行接口：

- `execute_order(order)` - 执行单个订单
- `cancel_order(order_id)` - 撤销订单
- `take_remaining_at_market(order_id)` - 市价成交未成交部分
- `execute_arbitrage_orders(poly, orbit)` - 执行套利订单对
- `cancel_all_orders(venue)` - 撤销所有活跃订单

### 2. PolymarketExecutor (`polymarket_executor.py`)

Polymarket 平台执行器：

- 使用 `py-clob-client` 官方客户端
- L2 签名方式（需要私钥）
- 订单类型：GTC (Good-Til-Cancelled), FOK (Fill-Or-Kill), FAK (Fill-And-Kill)
- 所有订单默认使用 GTC，等同于 POC

### 3. OrbitExchExecutor (`orbitexch_executor.py`)

OrbitExch 平台执行器：

- 复用 OddsSubscriptionService 的 Playwright 页面
- 下单：HTTP POST 到 `/customer/api/placeBets`
- 撤单：API 调用或点击 'Cancel Bet' 按钮
- 市价成交：点击 'Take @XX' 按钮

**赔率格式注意**：OrbitExch 使用 (100/概率) 格式，如概率 0.5 对应赔率 2.0

### 4. Order Model (`models.py`)

```python
@dataclass
class Order:
    order_id: str
    venue: Venue              # POLYMARKET | ORBITEXCH
    pair_id: str
    market_type: str          # "home" | "draw" | "away"
    side: OrderSide           # BUY | SELL | BACK | LAY
    price: float
    size: float
    order_type: OrderType     # GTC | FOK | FAK
    status: OrderStatus       # PENDING -> LIVE -> FILLED/CANCELLED
    ...
```

## API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/execution/status` | 获取服务状态 |
| POST | `/api/execution/orders` | 下单 |
| DELETE | `/api/execution/orders/{order_id}` | 撤单 |
| POST | `/api/execution/orders/{order_id}/take-at-market` | 市价成交 |
| GET | `/api/execution/orders` | 获取订单列表 |
| GET | `/api/execution/orders/active` | 获取活跃订单 |
| POST | `/api/execution/cancel-all` | 撤销所有订单 |
| POST | `/api/execution/initialize` | 初始化服务 |

## 配置

### 环境变量

```bash
# Polymarket
POLYMARKET_API_KEY=xxx
POLYMARKET_API_SECRET=xxx
POLYMARKET_PASSPHRASE=xxx
POLYMARKET_PRIVATE_KEY=xxx
POLYMARKET_FUNDER=xxx

# OrbitExch (复用 OddsSubscription 的登录状态)
ORBITEXCH_USERNAME=xxx
ORBITEXCH_PASSWORD=xxx
```

## 使用示例

### 1. 初始化服务

```python
from src.arbitrage.services.execution import ExecutionService, ExecutionConfig

config = ExecutionConfig(
    polymarket_api_key="xxx",
    polymarket_private_key="xxx",
)
service = ExecutionService(config=config)
await service.initialize()
```

### 2. 执行订单

```python
from src.arbitrage.services.execution import Order, Venue, OrderSide

order = Order(
    venue=Venue.POLYMARKET,
    pair_id="match-123",
    market_type="home",
    token_id="0x...",
    side=OrderSide.BUY,
    price=0.55,
    size=100.0,
)
result = await service.execute_order(order)
```

### 3. 执行套利订单对

```python
poly_order = Order(venue=Venue.POLYMARKET, ...)
orbit_order = Order(venue=Venue.ORBITEXCH, ...)

poly_result, orbit_result = await service.execute_arbitrage_orders(
    poly_order, orbit_order
)
```

## 与其他服务集成

### StrategyService 触发执行

```python
# 在 StrategyService 中
def on_opportunity_detected(self, opportunity):
    # 创建订单
    orders = self._create_orders_from_opportunity(opportunity)

    # 触发执行
    execution_service = app_state.get_execution_service()
    await execution_service.execute_arbitrage_orders(*orders)
```

### 共享 OrbitExch 页面

```python
# OddsSubscriptionService 启动后
odds_service = app_state.get_odds_service()
execution_service = app_state.get_execution_service()

# 共享页面引用
for comp_id, page in odds_service._orbitexch_client._pages.items():
    execution_service.set_orbitexch_page(comp_id, page)
```
