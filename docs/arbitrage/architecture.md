# 跨市场套利系统 - 架构设计

## 系统概述

基于 NautilusTrader 构建的跨市场套利系统，采用**微服务架构**设计，各服务通过 MessageBus 解耦通信，支持市场发现、智能匹配、实时配置管理和可视化监控。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Web 看板层                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        WebGateway (FastAPI)                          │   │
│  │   [市场发现] [市场匹配] [套利机会] [订单状态] [配置管理]              │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                         ↑↓ REST/WebSocket                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        MessageBus (事件总线)                         │   │
│  │   Pub/Sub | Request/Response | Point-to-Point                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│       ↑↓              ↑↓              ↑↓              ↑↓              ↑↓    │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐  │
│  │ Config  │    │Discovery│    │ Matcher │    │Strategy │    │  Risk   │  │
│  │ Service │    │ Service │    │ Service │    │ Service │    │ Service │  │
│  └─────────┘    └─────────┘    └─────────┘    └─────────┘    └─────────┘  │
│       │              │              │              │              │        │
│       └──────────────┴──────────────┴──────────────┴──────────────┘        │
│                                    ↓                                        │
│                           ┌─────────────┐                                   │
│                           │ Execution   │                                   │
│                           │ Service     │                                   │
│                           └─────────────┘                                   │
│                                    ↓                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                     NautilusTrader 适配器层 (复用框架)                       │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │  DataClient (继承)                 ExecutionClient (继承)              │ │
│  │  ┌─────────┐  ┌─────────┐         ┌─────────┐  ┌─────────┐           │ │
│  │  │Polymarket│  │OrbitExch│   ...   │Polymarket│  │OrbitExch│   ...    │ │
│  │  │DataClient│  │DataClient│        │ExecClient│  │ExecClient│         │ │
│  │  └─────────┘  └─────────┘         └─────────┘  └─────────┘           │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────────────────────┤
│                              数据存储层                                      │
│  ┌─────────────┐       ┌─────────────┐       ┌─────────────┐               │
│  │    Redis    │       │ PostgreSQL  │       │   Parquet   │               │
│  │  状态缓存   │       │  持久存储   │       │  数据归档   │               │
│  └─────────────┘       └─────────────┘       └─────────────┘               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 微服务设计

### 服务边界划分

| 服务 | 职责 | 输入事件 | 输出事件 |
|------|------|----------|----------|
| **ConfigService** | 配置管理、热更新、广播 | ConfigUpdateRequest | ConfigUpdatedEvent |
| **DiscoveryService** | 市场发现、监控 | ConfigUpdatedEvent | MarketDiscoveredEvent |
| **MatcherService** | 市场匹配、配对管理 | MarketDiscoveredEvent, ConfigUpdatedEvent | MarketPairMatchedEvent |
| **StrategyService** | 套利监控、机会识别 | MarketPairMatchedEvent, QuoteTick, ConfigUpdatedEvent | OpportunityDetectedEvent |
| **RiskService** | 风险监控、敞口计算 | QuoteTick, PositionEvent, ConfigUpdatedEvent | RiskStatusEvent, RiskAlertEvent |
| **ExecutionService** | 订单执行决策 | OpportunityDetectedEvent, RiskStatusEvent, ConfigUpdatedEvent | OrderExecutedEvent, ... |
| **WebGateway** | API网关、WebSocket推送 | 所有事件 (订阅 MessageBus) | HTTP/WebSocket响应 |

---

## 消息传递机制

### MessageBus 通信模式

```
┌─────────────────────────────────────────────────────────────────┐
│                        MessageBus                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Pub/Sub    │  │  Req/Resp    │  │ Point-to-Point│          │
│  │              │  │              │  │              │          │
│  │ - 配置更新   │  │ - 状态查询   │  │ - 订单命令   │          │
│  │ - 市场发现   │  │ - 配置读取   │  │ - 风控检查   │          │
│  │ - 配对匹配   │  │ - 持仓查询   │  │              │          │
│  │ - 套利机会   │  │ - ...        │  │              │          │
│  │ - 订单执行   │  │              │  │              │          │
│  │ - 风险告警   │  │              │  │              │          │
│  │ - ...        │  │              │  │              │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 事件定义

```python
# common/commands/config_commands.py
@dataclass
class ConfigUpdateRequest:
    """配置更新请求（命令）"""
    config_key: str           # 配置路径
    new_value: Any            # 新值
    requester: str            # 请求来源 (web/api/system)
    request_id: str           # 请求ID（幂等性）
    timestamp: datetime

# common/events/config_events.py
@dataclass
class ConfigUpdatedEvent:
    """配置更新事件（命令处理后发布）"""
    config_key: str           # 配置路径
    old_value: Any            # 旧值
    new_value: Any            # 新值
    updated_by: str           # 更新来源 (web/api/system)
    request_id: str           # 关联的请求ID
    sequence: int             # 序列号（用于排序）
    timestamp: datetime

# common/events/market_events.py
@dataclass
class MarketDiscoveredEvent:
    """市场发现事件"""
    venue_id: str             # 交易所ID
    instrument_id: str        # 合约ID
    instrument: Instrument    # 合约详情
    action: str               # ADDED/UPDATED/REMOVED

@dataclass
class MarketPairMatchedEvent:
    """市场配对事件"""
    pair_id: str              # 配对ID
    market_a: str             # 市场A
    market_b: str             # 市场B
    match_type: str           # 匹配类型
    confidence: float         # 置信度

# common/events/opportunity_events.py
@dataclass
class OpportunityDetectedEvent:
    """套利机会事件"""
    opportunity_id: str
    pair_id: str              # 配对ID（全局唯一）
    # 市场A标识
    venue_a: str              # 交易所A (e.g., POLYMARKET)
    instrument_a: str         # 合约A
    price_a: float            # 价格A
    # 市场B标识
    venue_b: str              # 交易所B (e.g., ORBITEXCH)
    instrument_b: str         # 合约B
    price_b: float            # 价格B
    # 套利信息
    spread_pct: float         # 价差百分比
    estimated_profit: float   # 预估收益
    max_quantity: float       # 最大可执行量
    # 时间
    detected_at: datetime     # 检测时间
    expires_at: datetime      # 过期时间
    sequence: int             # 序列号

# common/events/risk_events.py
@dataclass
class RiskStatusEvent:
    """风险状态事件（持续广播）"""
    # 作用域标识
    account_id: str           # 账户ID
    portfolio_id: str         # 组合ID（可选，用于多策略隔离）
    venue_id: str | None      # 交易所ID（None 表示全局汇总）
    # 风险指标
    total_exposure: float     # 总敞口
    available_margin: float   # 可用保证金
    risk_level: str           # LOW/MEDIUM/HIGH/CRITICAL
    can_open_position: bool   # 是否允许开仓
    limits: dict              # 各项限额状态
    # 时序标识
    sequence: int             # 序列号（递增）
    produced_at: datetime     # 生成时间
    valid_until: datetime     # 有效期（过期后视为陈旧）

@dataclass
class RiskAlertEvent:
    """风险告警事件"""
    account_id: str           # 账户ID
    alert_type: str           # EXPOSURE_HIGH, MARGIN_LOW, ...
    severity: str             # WARNING/CRITICAL
    message: str
    sequence: int
    timestamp: datetime

# common/events/execution_events.py
@dataclass
class OrderExecutedEvent:
    """订单执行事件"""
    execution_id: str
    order_id: str
    status: str               # FILLED/PARTIAL/FAILED
    filled_qty: float
    avg_price: float
    fee: float
```

---

## 服务间关联设计

### 数据流向图

```
┌─────────────┐
│ ConfigService│ ─────────── ConfigUpdatedEvent ──────────────────────────────────┐
└─────────────┘                                                                   │
                                                                                  ↓
┌─────────────┐    MarketDiscovered     ┌─────────────┐    MarketPairMatched    ┌─────────────┐
│ Discovery   │ ─────────────────────→ │   Matcher   │ ─────────────────────→ │  Strategy   │
│ Service     │         Event          │   Service   │         Event          │  Service    │
└─────────────┘                        └─────────────┘                        └─────────────┘
                                                                                     │
                                                                      OpportunityDetectedEvent
                                                                                     │
┌─────────────┐                                                                      │
│    Risk     │ ←── QuoteTick, PositionEvent, ConfigUpdatedEvent                     │
│   Service   │                                                                      │
└─────────────┘                                                                      │
      │                                                                              │
      │ RiskStatusEvent                                                              │
      │                                                                              │
      │         ┌────────────────────────────────────────────────────────────────────┘
      │         │
      ↓         ↓
┌─────────────────────────────┐
│      ExecutionService       │  ← 综合判断: 机会 + 风险状态 → 是否执行
│  订阅: OpportunityDetected  │
│        + RiskStatus         │
└─────────────────────────────┘
              │
     OrderExecutedEvent
              │
              ↓
      ┌─────────────┐
      │ WebGateway  │
      │ (WebSocket) │
      └─────────────┘
```

### 解耦策略

1. **事件驱动解耦**
   - 服务只依赖事件接口，不依赖具体服务实现
   - 新增服务只需订阅相关事件，无需修改现有服务

2. **配置广播机制**
   ```
   Web面板 → ConfigService → ConfigUpdatedEvent → MessageBus
                                                      │
              ┌────────────────┬────────────────┬─────┴─────┬────────────────┐
              ↓                ↓                ↓           ↓                ↓
         Discovery        Matcher          Strategy      Risk          Execution
         (更新过滤)      (更新规则)       (更新阈值)   (更新限额)      (更新参数)
   ```

3. **服务独立部署**
   - 每个服务可独立启停
   - 服务故障不影响其他服务运行
   - 支持服务级别的水平扩展

---

## 各服务详细设计

### 1. ConfigService (配置管理服务)

**职责**：统一配置管理，支持热更新和广播

```
┌─────────────────────────────────────────────────────────┐
│                    ConfigService                         │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ ConfigStore │    │ Validator   │    │ Broadcaster │ │
│  │ (PostgreSQL)│    │ (Schema)    │    │ (MessageBus)│ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - get_config(path) → ConfigValue                       │
│  - update_config(path, value) → bool                    │
│  - subscribe_changes(callback)                          │
│                                                          │
│  事件:                                                   │
│  - 发布: ConfigUpdatedEvent                             │
└─────────────────────────────────────────────────────────┘
```

### 2. DiscoveryService (市场发现服务)

**职责**：发现并监控各交易所可交易市场

```
┌─────────────────────────────────────────────────────────┐
│                   DiscoveryService                       │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ MarketScanner│    │ MarketFilter│    │ MarketCache │ │
│  │ (定时扫描)  │    │ (配置过滤)  │    │ (Redis)     │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - discover_markets(venue_id) → List[Market]            │
│  - get_active_markets() → Dict[VenueId, List[Market]]   │
│                                                          │
│  订阅: ConfigUpdatedEvent (更新扫描配置)                 │
│  发布: MarketDiscoveredEvent                            │
└─────────────────────────────────────────────────────────┘
```

### 3. MatcherService (市场匹配服务)

**职责**：识别不同平台间的相同事件/品种

```
┌─────────────────────────────────────────────────────────┐
│                    MatcherService                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ MatchEngine │    │ RuleEngine  │    │ PairStore   │ │
│  │ (匹配算法)  │    │ (规则配置)  │    │ (PostgreSQL)│ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - match_markets(market_a, market_b) → MatchResult      │
│  - get_matched_pairs() → List[MarketPair]               │
│  - create_manual_pair(pair) → bool                      │
│                                                          │
│  订阅: MarketDiscoveredEvent, ConfigUpdatedEvent        │
│  发布: MarketPairMatchedEvent                           │
└─────────────────────────────────────────────────────────┘
```

### 4. StrategyService (套利策略服务)

**职责**：监控价差、识别套利机会

```
┌─────────────────────────────────────────────────────────┐
│                   StrategyService                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │SpreadMonitor│    │OpportunityDet│    │ SignalGen   │ │
│  │ (价差监控)  │    │ (机会识别)  │    │ (信号生成)  │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - calculate_spread(pair) → SpreadInfo                  │
│  - get_opportunities() → List[Opportunity]              │
│                                                          │
│  订阅: MarketPairMatchedEvent, QuoteTick, ConfigUpdated │
│  发布: OpportunityDetectedEvent                         │
└─────────────────────────────────────────────────────────┘
```

### 5. RiskService (风险监控服务)

**职责**：持续监控市场数据和持仓，计算风险状态，广播风险事件

```
┌─────────────────────────────────────────────────────────┐
│                     RiskService                          │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ExposureCalc │    │ LimitMonitor│    │ AlertManager│ │
│  │ (敞口计算)  │    │ (限额监控)  │    │ (告警管理)  │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - get_risk_status() → RiskStatus                       │
│  - get_exposure() → ExposureReport                      │
│  - emergency_stop() → bool                              │
│                                                          │
│  订阅: QuoteTick, PositionEvent, ConfigUpdatedEvent     │
│  发布: RiskStatusEvent (持续广播), RiskAlertEvent       │
└─────────────────────────────────────────────────────────┘
```

**说明**：RiskService 独立于交易流程，持续监控市场数据和持仓变化，计算当前风险状态并广播。

### 6. ExecutionService (订单执行服务)

**职责**：综合判断套利机会和风险状态，决定是否执行订单

```
┌─────────────────────────────────────────────────────────┐
│                   ExecutionService                       │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │DecisionMaker│    │OrderRouter  │    │StateTracker │ │
│  │ (执行决策)  │    │ (路由分发)  │    │ (状态跟踪)  │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  接口:                                                   │
│  - submit_order(order) → OrderId                        │
│  - cancel_order(order_id) → bool                        │
│  - get_order_status(order_id) → OrderStatus             │
│                                                          │
│  订阅: OpportunityDetectedEvent (Strategy)              │
│        RiskStatusEvent (Risk)                           │
│        ConfigUpdatedEvent                               │
│  发布: OrderExecutedEvent, OrderFailedEvent             │
└─────────────────────────────────────────────────────────┘
```

**执行决策逻辑**：
```python
def on_opportunity_detected(self, event: OpportunityDetectedEvent):
    # 获取当前风险状态（从最近的 RiskStatusEvent 缓存）
    risk_status = self._current_risk_status
    now = datetime.utcnow()

    # 1. 检查风险状态是否存在
    if risk_status is None:
        self._log_skipped(event, reason="no_risk_status")
        return

    # 2. 检查风险状态是否过期（新鲜度校验）
    if now > risk_status.valid_until:
        self._log_skipped(event, reason="risk_status_stale")
        self._publish_alert("RISK_STATUS_STALE")
        return

    # 3. 检查作用域匹配（如果有 venue 级别的风险状态）
    if risk_status.venue_id and risk_status.venue_id not in [event.venue_a, event.venue_b]:
        self._log_skipped(event, reason="risk_scope_mismatch")
        return

    # 4. 综合判断
    if risk_status.can_open_position and risk_status.risk_level != "CRITICAL":
        self._execute_arbitrage(event)
    else:
        self._log_skipped(event, reason="risk_not_acceptable")
```

### 7. WebGateway (Web API 网关)

**职责**：对外提供 REST/WebSocket 接口

```
┌─────────────────────────────────────────────────────────┐
│                     WebGateway                           │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │  REST API   │    │ WebSocket   │    │EventAggregator│
│  │ (FastAPI)   │    │ (实时推送)  │    │ (事件聚合)  │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│  REST 端点:                                              │
│  - /api/v1/markets           市场管理                   │
│  - /api/v1/matches           配对管理                   │
│  - /api/v1/opportunities     套利机会                   │
│  - /api/v1/orders            订单管理                   │
│  - /api/v1/config            配置管理                   │
│                                                          │
│  WebSocket:                                              │
│  - /ws/v1/stream             实时数据流                 │
│                                                          │
│  订阅: 所有事件 (用于WebSocket推送)                     │
└─────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
nautilus_trader/                     # NautilusTrader 核心框架（复用，不修改核心）
├── core/                            # 核心类型和基础设施
│   ├── message.pyx                  # 消息基类
│   ├── data.pyx                     # 数据类型 (QuoteTick, Bar, ...)
│   └── ...
├── model/                           # 交易模型
│   ├── instruments/                 # 合约定义
│   ├── orders/                      # 订单类型
│   └── position.pyx                 # 持仓
├── msgbus/                          # MessageBus 消息总线
│   └── bus.pyx                      # 发布/订阅/请求响应
├── cache/                           # 高性能缓存
├── data/                            # DataEngine 数据引擎
├── execution/                       # ExecutionEngine 执行引擎
├── risk/                            # RiskEngine 风控引擎
├── live/                            # 实盘基类
│   ├── data_client.py               # LiveDataClient 基类
│   └── execution_client.py          # LiveExecutionClient 基类
├── adapters/                        # 交易所适配器（在此扩展）
│   ├── _template/                   # 适配器模板
│   ├── polymarket/                  # Polymarket 适配器（已存在）
│   │   ├── data.py                  # PolymarketDataClient
│   │   ├── execution.py             # PolymarketExecutionClient
│   │   └── ...
│   ├── orbitexch/                   # OrbitExch 适配器（已存在）
│   │   ├── data.py
│   │   ├── execution.py
│   │   └── ...
│   └── ...                          # 其他交易所适配器
└── trading/                         # 策略基类
    ├── strategy.pyx                 # Strategy 基类
    └── trader.pyx                   # Trader 交易者

src/arbitrage/
├── common/                          # 共享模块
│   ├── __init__.py
│   ├── events/                      # 事件定义
│   │   ├── __init__.py
│   │   ├── config_events.py
│   │   ├── market_events.py
│   │   ├── opportunity_events.py
│   │   └── execution_events.py
│   ├── models/                      # 数据模型
│   │   ├── __init__.py
│   │   ├── market.py
│   │   ├── pair.py
│   │   ├── opportunity.py
│   │   └── order.py
│   └── interfaces/                  # 服务接口
│       ├── __init__.py
│       └── service_base.py
│
├── services/                        # 微服务模块
│   ├── config/                      # 配置管理服务
│   │   ├── __init__.py
│   │   ├── requirements.md          # 模块需求
│   │   ├── design.md                # 模块设计
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── market_discovery/            # 市场发现服务
│   │   ├── __init__.py
│   │   ├── requirements.md
│   │   ├── design.md
│   │   ├── service.py
│   │   ├── scanner.py
│   │   └── filter.py
│   │
│   ├── market_matcher/              # 市场匹配服务
│   │   ├── __init__.py
│   │   ├── requirements.md
│   │   ├── design.md
│   │   ├── service.py
│   │   ├── engine.py
│   │   └── rules.py
│   │
│   ├── strategy/                    # 套利策略服务
│   │   ├── __init__.py
│   │   ├── requirements.md
│   │   ├── design.md
│   │   ├── service.py
│   │   ├── spread_monitor.py
│   │   └── opportunity_detector.py
│   │
│   ├── risk/                        # 风险控制服务
│   │   ├── __init__.py
│   │   ├── requirements.md
│   │   ├── design.md
│   │   ├── service.py
│   │   └── checker.py
│   │
│   ├── execution/                   # 订单执行服务
│   │   ├── __init__.py
│   │   ├── requirements.md
│   │   ├── design.md
│   │   ├── service.py
│   │   ├── order_manager.py
│   │   └── router.py
│   │
│   └── web_gateway/                 # Web API 网关
│       ├── __init__.py
│       ├── requirements.md
│       ├── design.md
│       ├── app.py
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── markets.py
│       │   ├── matches.py
│       │   ├── opportunities.py
│       │   ├── orders.py
│       │   └── config.py
│       └── websocket/
│           ├── __init__.py
│           └── handler.py
│
└── tests/                           # 测试
    ├── unit/
    ├── integration/
    └── e2e/
```

---

## API 设计

### REST API

```yaml
# 市场发现
GET  /api/v1/markets                    # 获取所有市场
GET  /api/v1/markets/{venue_id}         # 获取特定交易所市场
POST /api/v1/markets/discover           # 触发市场发现

# 市场匹配
GET  /api/v1/matches                    # 获取所有匹配对
GET  /api/v1/matches/{pair_id}          # 获取匹配详情
POST /api/v1/matches                    # 手动创建配对
PUT  /api/v1/matches/{pair_id}          # 更新配对
DELETE /api/v1/matches/{pair_id}        # 删除配对

# 套利机会
GET  /api/v1/opportunities              # 获取当前套利机会
GET  /api/v1/opportunities/history      # 历史套利机会

# 订单管理
GET  /api/v1/orders                     # 获取订单列表
GET  /api/v1/orders/{order_id}          # 获取订单详情
GET  /api/v1/orders/active              # 获取活跃订单

# 配置管理
GET  /api/v1/config                     # 获取当前配置
GET  /api/v1/config/{path}              # 获取特定配置
PUT  /api/v1/config/{path}              # 更新配置
GET  /api/v1/config/schema              # 获取配置 Schema

# 系统控制
GET  /api/v1/status                     # 系统状态
POST /api/v1/control/start              # 启动系统
POST /api/v1/control/stop               # 停止系统
POST /api/v1/control/emergency-stop     # 紧急停止
```

### WebSocket API

```yaml
# 连接
WS /ws/v1/stream

# 订阅消息格式
{
    "action": "subscribe",
    "channels": ["markets", "matches", "opportunities", "orders", "config"]
}

# 推送消息类型
- market.discovered        # 市场发现
- market.updated           # 市场更新
- match.created            # 配对创建
- match.updated            # 配对更新
- opportunity.detected     # 套利机会
- opportunity.expired      # 机会过期
- order.submitted          # 订单提交
- order.filled             # 订单成交
- order.failed             # 订单失败
- config.updated           # 配置更新
- risk.alert               # 风险告警
```

---

## 部署架构

### 单节点部署

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│  ┌────────────────────────────────────────────────┐    │
│  │              Arbitrage Services                 │    │
│  │  (所有微服务在同一进程/容器中运行)              │    │
│  └────────────────────────────────────────────────┘    │
│  ┌─────────────┐  ┌─────────────┐                      │
│  │    Redis    │  │ PostgreSQL  │                      │
│  └─────────────┘  └─────────────┘                      │
└─────────────────────────────────────────────────────────┘
```

### 分布式部署 (未来)

```
┌─────────────────────────────────────────────────────────┐
│                    Kubernetes                            │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │
│  │Config   │ │Discovery│ │Matcher  │ │Strategy │       │
│  │Service  │ │Service  │ │Service  │ │Service  │       │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘       │
│  ┌─────────┐ ┌─────────┐ ┌─────────────────────┐       │
│  │Risk     │ │Execution│ │    WebGateway       │       │
│  │Service  │ │Service  │ │    (多实例)         │       │
│  └─────────┘ └─────────┘ └─────────────────────┘       │
│  ┌─────────────────────────────────────────────┐       │
│  │          Redis Cluster + PostgreSQL          │       │
│  └─────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────┘
```

---

## 适配器复用原则

遵循 CLAUDE.md 第 81-82 行规定：**标准化 + 生态复用，禁止自研**。

### 复用 nautilus_trader/adapters/ 现有结构

**禁止** 在 `src/arbitrage/` 下新建适配器目录。所有适配器开发必须在：

```
nautilus_trader/adapters/
├── polymarket/          # 已存在，直接扩展
│   ├── data.py          # PolymarketDataClient
│   ├── execution.py     # PolymarketExecutionClient
│   └── ...
├── orbitexch/           # 已存在，直接扩展
│   ├── data.py
│   ├── execution.py
│   └── ...
└── _template/           # 新适配器参考此模板
```

### NautilusTrader 适配器模式

```python
# 参考 nautilus_trader/adapters/_template/
# 所有适配器遵循框架官方模式
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.execution_client import LiveExecutionClient
```

### 适配器与服务的关系

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          服务层 (业务逻辑)                               │
│  DiscoveryService、MatcherService、StrategyService、RiskService、...    │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ 通过 NautilusTrader API 交互
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    NautilusTrader 核心引擎                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │  DataEngine  │  │ExecutionEngine│  │  RiskEngine  │                  │
│  │  (数据路由)  │  │ (订单路由)   │  │ (风控检查)   │                  │
│  └──────────────┘  └──────────────┘  └──────────────┘                  │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ 调用适配器
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    适配器层 (继承 NautilusTrader 基类)                    │
│  PolymarketDataClient, PolymarketExecutionClient, OrbitExchDataClient...│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 自动执行流程

ExecutionService 同时订阅 StrategyService 和 RiskService，综合判断后自动执行：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              并行数据流                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐                                                            │
│  │   Risk      │ ←── QuoteTick, PositionEvent (持续监控)                    │
│  │  Service    │                                                            │
│  └─────────────┘                                                            │
│         │                                                                   │
│         │ RiskStatusEvent (持续广播风险状态)                                 │
│         │                                                                   │
│         ↓                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                       ExecutionService                               │   │
│  │                                                                       │   │
│  │   ┌─────────────────────────────────────────────────────────────┐   │   │
│  │   │  Decision Logic:                                             │   │   │
│  │   │  IF opportunity_detected AND risk_status.can_open_position   │   │   │
│  │   │     AND risk_level != CRITICAL                               │   │   │
│  │   │  THEN execute_arbitrage()                                    │   │   │
│  │   └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         ↑                                                                   │
│         │ OpportunityDetectedEvent (机会触发)                               │
│         │                                                                   │
│  ┌─────────────┐                                                            │
│  │  Strategy   │ ←── MarketPairMatchedEvent, QuoteTick                      │
│  │  Service    │                                                            │
│  └─────────────┘                                                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**关键点**：
- **RiskService**：独立监控市场数据和持仓，持续广播 `RiskStatusEvent`
- **StrategyService**：监控价差，检测到机会时发布 `OpportunityDetectedEvent`
- **ExecutionService**：订阅两者，综合判断：`机会 + 风险可接受 → 执行订单`

---

## 技术选型

| 层级 | 技术 | 说明 |
|------|------|------|
| 核心框架 | NautilusTrader | 事件驱动交易系统 |
| 消息总线 | NautilusTrader MessageBus | 服务间通信 |
| Web 框架 | FastAPI | 高性能异步 API |
| WebSocket | websockets | 实时数据推送 |
| 缓存 | Redis | 状态缓存、会话管理 |
| 数据库 | PostgreSQL | 配置存储、历史数据 |
| 容器化 | Docker | 部署和运维 |

---

## 文档结构说明

按照 workflow.md 的整体局部原则：

| 文档位置 | 内容 | 作用 |
|----------|------|------|
| `docs/arbitrage/requirements.md` | 整体需求 | 定义系统级功能需求 |
| `docs/arbitrage/architecture.md` | 整体架构 | 定义服务边界和交互 |
| `docs/arbitrage/database-schema.md` | 整体数据设计 | 定义共享数据结构 |
| `src/*/requirements.md` | 模块需求 | 定义模块详细需求 |
| `src/*/design.md` | 模块设计 | 定义模块内部设计 |

模块设计文档需引用整体文档中的相关定义，保持一致性。
