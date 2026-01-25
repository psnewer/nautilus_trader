# 跨市场套利系统 - 数据库设计

## 概述

本文档定义套利系统的数据存储方案，采用微服务架构下的数据分离策略。

---

## 存储架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            微服务层                                      │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │
│  │ Config  │ │Discovery│ │ Matcher │ │Strategy │ │Execution│          │
│  │ Service │ │ Service │ │ Service │ │ Service │ │ Service │          │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘          │
│       │           │           │           │           │                 │
└───────┼───────────┼───────────┼───────────┼───────────┼─────────────────┘
        │           │           │           │           │
        ↓           ↓           ↓           ↓           ↓
┌───────────────────────────────────────────────────────────────────────┐
│                          数据存储层                                    │
│                                                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                      PostgreSQL (持久存储)                       │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │  │
│  │  │ 配置表   │ │ 市场表   │ │ 配对表   │ │ 执行表   │           │  │
│  │  │ configs  │ │ markets  │ │ pairs    │ │ orders   │           │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                        Redis (实时缓存)                          │  │
│  │  - 行情数据        - 订单状态        - 配置缓存                 │  │
│  │  - 会话状态        - 套利机会        - 服务状态                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└───────────────────────────────────────────────────────────────────────┘
```

---

## PostgreSQL 表设计

### 1. 配置管理表

#### system_configs (系统配置)

```sql
CREATE TABLE system_configs (
    id              SERIAL PRIMARY KEY,
    config_key      VARCHAR(100) NOT NULL UNIQUE,   -- 配置键 (e.g., global.log_level)
    config_value    JSONB NOT NULL,                 -- 配置值
    config_type     VARCHAR(20) NOT NULL,           -- 类型: GLOBAL, STRATEGY, RISK, VENUE
    description     TEXT,                           -- 配置说明
    schema_def      JSONB,                          -- JSON Schema 定义（用于验证）
    version         INTEGER DEFAULT 1,              -- 配置版本
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_configs_key ON system_configs(config_key);
CREATE INDEX idx_configs_type ON system_configs(config_type);

-- 示例数据
INSERT INTO system_configs (config_key, config_value, config_type, description) VALUES
('global.log_level', '"INFO"', 'GLOBAL', '日志级别'),
('strategy.min_spread_threshold', '0.1', 'STRATEGY', '最小价差阈值(%)'),
('risk.max_single_order_value', '10000', 'RISK', '单笔最大金额');
```

#### config_audit_log (配置变更日志)

```sql
CREATE TABLE config_audit_log (
    id              SERIAL PRIMARY KEY,
    config_key      VARCHAR(100) NOT NULL,
    old_value       JSONB,
    new_value       JSONB NOT NULL,
    changed_by      VARCHAR(50),                    -- 变更来源: web/api/system
    change_reason   TEXT,
    changed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_key ON config_audit_log(config_key);
CREATE INDEX idx_audit_time ON config_audit_log(changed_at);
```

---

### 2. 市场发现表

#### venues (交易所配置)

```sql
CREATE TABLE venues (
    id              SERIAL PRIMARY KEY,
    venue_id        VARCHAR(50) NOT NULL UNIQUE,    -- 交易所标识 (POLYMARKET, ORBITEXCH)
    name            VARCHAR(100) NOT NULL,          -- 显示名称
    venue_type      VARCHAR(20) NOT NULL,           -- PREDICTION_MARKET, EXCHANGE
    api_url         VARCHAR(255),
    ws_url          VARCHAR(255),
    is_enabled      BOOLEAN DEFAULT true,
    config          JSONB,                          -- 交易所特定配置
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_venues_enabled ON venues(is_enabled);
```

#### markets (已发现市场)

```sql
CREATE TABLE markets (
    id              SERIAL PRIMARY KEY,
    instrument_id   VARCHAR(150) NOT NULL UNIQUE,   -- 合约标识
    venue_id        VARCHAR(50) NOT NULL,           -- 交易所
    symbol          VARCHAR(100) NOT NULL,          -- 交易对/事件符号
    market_type     VARCHAR(20) NOT NULL,           -- EVENT, SPOT, PERPETUAL

    -- 事件/市场信息
    event_name      VARCHAR(500),                   -- 事件名称
    event_slug      VARCHAR(200),                   -- 事件标识
    outcome         VARCHAR(100),                   -- YES/NO 或其他结果

    -- 交易参数
    price_precision INTEGER,
    size_precision  INTEGER,
    min_quantity    DECIMAL(20, 8),
    tick_size       DECIMAL(20, 8),

    -- 状态
    is_active       BOOLEAN DEFAULT true,
    metadata        JSONB,                          -- 扩展元数据
    discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (venue_id) REFERENCES venues(venue_id)
);

CREATE INDEX idx_markets_venue ON markets(venue_id);
CREATE INDEX idx_markets_active ON markets(is_active);
CREATE INDEX idx_markets_event ON markets(event_slug);
CREATE INDEX idx_markets_type ON markets(market_type);
```

---

### 3. 市场匹配表

#### market_pairs (市场配对)

```sql
CREATE TABLE market_pairs (
    id              SERIAL PRIMARY KEY,
    pair_id         VARCHAR(100) NOT NULL UNIQUE,   -- 配对标识
    market_a_id     INTEGER NOT NULL,               -- 市场 A
    market_b_id     INTEGER NOT NULL,               -- 市场 B

    -- 匹配信息
    match_type      VARCHAR(30) NOT NULL,           -- CROSS_PLATFORM, SAME_EVENT
    match_rule      VARCHAR(50),                    -- 使用的匹配规则
    confidence      DECIMAL(5, 4),                  -- 匹配置信度 (0-1)

    -- 配置
    is_active       BOOLEAN DEFAULT true,
    is_manual       BOOLEAN DEFAULT false,          -- 是否手动创建
    config          JSONB,                          -- 配对特定配置

    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (market_a_id) REFERENCES markets(id),
    FOREIGN KEY (market_b_id) REFERENCES markets(id),
    UNIQUE(market_a_id, market_b_id)
);

CREATE INDEX idx_pairs_active ON market_pairs(is_active);
CREATE INDEX idx_pairs_type ON market_pairs(match_type);
```

#### match_rules (匹配规则配置)

```sql
CREATE TABLE match_rules (
    id              SERIAL PRIMARY KEY,
    rule_id         VARCHAR(50) NOT NULL UNIQUE,    -- 规则标识
    rule_name       VARCHAR(100) NOT NULL,          -- 规则名称
    description     TEXT,
    rule_config     JSONB NOT NULL,                 -- 规则配置
    priority        INTEGER DEFAULT 0,              -- 优先级
    is_enabled      BOOLEAN DEFAULT true,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 示例规则
INSERT INTO match_rules (rule_id, rule_name, rule_config) VALUES
('same_event_name', '相同事件名称匹配', '{"match_field": "event_name", "similarity_threshold": 0.9}'),
('same_event_slug', '相同事件标识匹配', '{"match_field": "event_slug", "exact_match": true}');
```

---

### 4. 套利执行表

#### arbitrage_opportunities (套利机会)

```sql
CREATE TABLE arbitrage_opportunities (
    id              SERIAL PRIMARY KEY,
    opportunity_id  VARCHAR(100) NOT NULL UNIQUE,
    pair_id         VARCHAR(100) NOT NULL,

    -- 价差信息
    price_a         DECIMAL(20, 8) NOT NULL,
    price_b         DECIMAL(20, 8) NOT NULL,
    spread          DECIMAL(20, 8) NOT NULL,
    spread_pct      DECIMAL(10, 6) NOT NULL,

    -- 预估收益
    estimated_profit DECIMAL(20, 8),
    max_quantity    DECIMAL(20, 8),

    -- 状态
    status          VARCHAR(20) DEFAULT 'DETECTED',  -- DETECTED, EXECUTING, EXECUTED, EXPIRED

    -- 时间戳
    detected_at     TIMESTAMP NOT NULL,
    expired_at      TIMESTAMP,
    executed_at     TIMESTAMP,

    FOREIGN KEY (pair_id) REFERENCES market_pairs(pair_id)
);

CREATE INDEX idx_opp_pair ON arbitrage_opportunities(pair_id);
CREATE INDEX idx_opp_status ON arbitrage_opportunities(status);
CREATE INDEX idx_opp_detected ON arbitrage_opportunities(detected_at DESC);
```

#### arbitrage_executions (套利执行记录)

```sql
CREATE TABLE arbitrage_executions (
    id              SERIAL PRIMARY KEY,
    execution_id    VARCHAR(100) NOT NULL UNIQUE,
    opportunity_id  VARCHAR(100) NOT NULL,

    -- 订单信息
    order_a_id      VARCHAR(100),                   -- A 侧订单
    order_b_id      VARCHAR(100),                   -- B 侧订单
    execution_order VARCHAR(20),                    -- SIMULTANEOUS, A_FIRST, B_FIRST

    -- 成交信息
    filled_qty_a    DECIMAL(20, 8),
    filled_qty_b    DECIMAL(20, 8),
    avg_price_a     DECIMAL(20, 8),
    avg_price_b     DECIMAL(20, 8),

    -- 费用和收益
    fee_a           DECIMAL(20, 8),
    fee_b           DECIMAL(20, 8),
    realized_profit DECIMAL(20, 8),
    profit_pct      DECIMAL(10, 6),

    -- 状态
    status          VARCHAR(20) NOT NULL,           -- PENDING, PARTIAL, FILLED, FAILED
    error_message   TEXT,

    -- 时间
    started_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP,

    FOREIGN KEY (opportunity_id) REFERENCES arbitrage_opportunities(opportunity_id)
);

CREATE INDEX idx_exec_opp ON arbitrage_executions(opportunity_id);
CREATE INDEX idx_exec_status ON arbitrage_executions(status);
CREATE INDEX idx_exec_time ON arbitrage_executions(started_at DESC);
```

#### orders (订单记录)

```sql
CREATE TABLE orders (
    id              SERIAL PRIMARY KEY,
    order_id        VARCHAR(100) NOT NULL UNIQUE,   -- 内部订单ID
    venue_order_id  VARCHAR(100),                   -- 交易所订单ID
    execution_id    VARCHAR(100),                   -- 关联执行ID

    -- 订单信息
    venue_id        VARCHAR(50) NOT NULL,
    instrument_id   VARCHAR(150) NOT NULL,
    side            VARCHAR(10) NOT NULL,           -- BUY, SELL
    order_type      VARCHAR(20) NOT NULL,           -- MARKET, LIMIT

    -- 数量和价格
    quantity        DECIMAL(20, 8) NOT NULL,
    price           DECIMAL(20, 8),                 -- 限价单价格
    filled_qty      DECIMAL(20, 8) DEFAULT 0,
    avg_fill_price  DECIMAL(20, 8),

    -- 状态
    status          VARCHAR(20) NOT NULL,           -- PENDING, SUBMITTED, PARTIAL, FILLED, CANCELLED, FAILED
    error_message   TEXT,

    -- 时间
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitted_at    TIMESTAMP,
    filled_at       TIMESTAMP,

    FOREIGN KEY (venue_id) REFERENCES venues(venue_id)
);

CREATE INDEX idx_orders_execution ON orders(execution_id);
CREATE INDEX idx_orders_venue ON orders(venue_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_time ON orders(created_at DESC);
```

---

### 5. 风控和统计表

#### risk_events (风险事件)

```sql
CREATE TABLE risk_events (
    id              SERIAL PRIMARY KEY,
    event_type      VARCHAR(50) NOT NULL,           -- LIMIT_EXCEEDED, EMERGENCY_STOP
    event_level     VARCHAR(20) NOT NULL,           -- INFO, WARNING, CRITICAL
    description     TEXT,
    event_data      JSONB,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_risk_type ON risk_events(event_type);
CREATE INDEX idx_risk_level ON risk_events(event_level);
CREATE INDEX idx_risk_time ON risk_events(created_at DESC);
```

#### performance_stats (性能统计)

```sql
CREATE TABLE performance_stats (
    id              SERIAL PRIMARY KEY,
    stat_date       DATE NOT NULL,
    stat_type       VARCHAR(20) NOT NULL,           -- DAILY, WEEKLY

    -- 套利统计
    opportunities_detected INTEGER DEFAULT 0,
    opportunities_executed INTEGER DEFAULT 0,

    -- 收益统计
    total_profit    DECIMAL(20, 8) DEFAULT 0,
    total_fee       DECIMAL(20, 8) DEFAULT 0,
    net_profit      DECIMAL(20, 8) DEFAULT 0,

    -- 执行统计
    success_rate    DECIMAL(5, 4),
    avg_spread_pct  DECIMAL(10, 6),

    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(stat_date, stat_type)
);

CREATE INDEX idx_stats_date ON performance_stats(stat_date);
```

---

## Redis 数据结构

### 1. 配置缓存

```redis
# 配置值缓存
SET config:{config_key} {json_value}
EXPIRE config:{config_key} 3600

# 配置版本
SET config:version:{config_key} {version}

# 配置变更通知
PUBLISH config:updates {event_json}
```

### 2. 市场数据缓存

```redis
# 市场列表 (按交易所)
SADD markets:venue:{venue_id} {instrument_id}

# 市场详情
HSET market:{instrument_id}
    venue_id {venue}
    symbol {symbol}
    is_active {true/false}
    ...

# 活跃市场
SADD markets:active {instrument_id}
```

### 3. 配对缓存

```redis
# 活跃配对列表
SADD pairs:active {pair_id}

# 配对详情
HSET pair:{pair_id}
    market_a {instrument_id_a}
    market_b {instrument_id_b}
    confidence {0.95}
    ...
```

### 4. 实时行情

```redis
# 最新报价
HSET quote:{instrument_id}
    bid {bid_price}
    ask {ask_price}
    ts {timestamp}
EXPIRE quote:{instrument_id} 60

# 价差缓存
HSET spread:{pair_id}
    spread {value}
    spread_pct {pct}
    ts {timestamp}
```

### 5. 套利机会

```redis
# 当前机会 (Sorted Set by spread)
ZADD opportunities:active {spread_pct} {opportunity_id}

# 机会详情
HSET opportunity:{opportunity_id}
    pair_id {pair}
    spread_pct {spread}
    estimated_profit {profit}
    detected_at {timestamp}
    expires_at {timestamp}
EXPIRE opportunity:{opportunity_id} 300

# 机会推送通道
PUBLISH opportunities:new {opportunity_json}
```

### 6. 订单状态

```redis
# 活跃订单
SADD orders:active {order_id}

# 订单详情
HSET order:{order_id}
    status {status}
    filled_qty {qty}
    avg_price {price}
    updated_at {timestamp}

# 按执行分组
SADD orders:execution:{execution_id} {order_id}
```

### 7. 服务状态

```redis
# 服务心跳
HSET service:{service_name}
    status {RUNNING/STOPPED}
    last_heartbeat {timestamp}
EXPIRE service:{service_name} 30

# 服务列表
SADD services:active {service_name}
```

---

## 数据流与服务关系

```
┌─────────────────────────────────────────────────────────────────────┐
│                         数据写入流向                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ConfigService ───────→ system_configs (PostgreSQL)                │
│        │                config:{key} (Redis)                       │
│        └──────────────→ config_audit_log (PostgreSQL)              │
│                                                                     │
│  DiscoveryService ────→ markets (PostgreSQL)                       │
│        │                market:{id}, markets:venue:* (Redis)       │
│        └──────────────→ venues (PostgreSQL)                        │
│                                                                     │
│  MatcherService ──────→ market_pairs (PostgreSQL)                  │
│        │                pair:{id}, pairs:active (Redis)            │
│        └──────────────→ match_rules (PostgreSQL)                   │
│                                                                     │
│  StrategyService ─────→ arbitrage_opportunities (PostgreSQL)       │
│        │                opportunity:*, spread:* (Redis)            │
│        └──────────────→ opportunities:active (Redis)               │
│                                                                     │
│  ExecutionService ────→ arbitrage_executions (PostgreSQL)          │
│        │                orders (PostgreSQL)                        │
│        └──────────────→ order:*, orders:active (Redis)             │
│                                                                     │
│  RiskService ─────────→ risk_events (PostgreSQL)                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 数据保留策略

| 数据类型 | 存储位置 | 保留期限 |
|----------|----------|----------|
| 系统配置 | PostgreSQL | 永久 |
| 配置审计日志 | PostgreSQL | 90天 |
| 市场信息 | PostgreSQL + Redis | 永久 + 1小时缓存 |
| 配对信息 | PostgreSQL + Redis | 永久 + 1小时缓存 |
| 套利机会 | PostgreSQL + Redis | 30天 + 5分钟缓存 |
| 执行记录 | PostgreSQL | 永久 |
| 订单记录 | PostgreSQL + Redis | 永久 + 24小时缓存 |
| 实时行情 | Redis | 1分钟 |
| 性能统计 | PostgreSQL | 永久 |

---

## 索引优化建议

```sql
-- 高频查询优化
-- 活跃市场查询
CREATE INDEX idx_markets_active_venue ON markets(venue_id) WHERE is_active = true;

-- 活跃配对查询
CREATE INDEX idx_pairs_active_type ON market_pairs(match_type) WHERE is_active = true;

-- 最近套利机会
CREATE INDEX idx_opp_recent ON arbitrage_opportunities(detected_at DESC)
WHERE status IN ('DETECTED', 'EXECUTING');

-- 待处理订单
CREATE INDEX idx_orders_pending ON orders(created_at DESC)
WHERE status IN ('PENDING', 'SUBMITTED', 'PARTIAL');
```

---

## 备份策略

### PostgreSQL

```bash
# 每日全量备份
pg_dump -Fc arbitrage_db > backup_$(date +%Y%m%d).dump

# WAL 持续归档
archive_command = 'cp %p /backup/wal/%f'
```

### Redis

```bash
# RDB 快照
save 900 1
save 300 10

# AOF 持久化
appendonly yes
appendfsync everysec
```
