# 数据库设计文档

## 文档信息

| 属性 | 值 |
|------|-----|
| 版本 | 1.0 |
| 创建日期 | 2026-01-16 |
| 最后更新 | 2026-01-16 |
| 状态 | 草稿 |

---

## 概述

本文档定义套利系统的数据存储方案，包括当前的 JSON 文件存储和计划中的数据库迁移。

---

## 当前存储方案（JSON 文件）

### 文件结构

```
output/
├── polymarket_events.json     # Polymarket 事件数据
├── orbitexch_events.json      # OrbitExch 事件数据
└── market_matches.json        # 匹配结果
```

### polymarket_events.json 结构

```json
{
  "events": [
    {
      "id": "string",
      "title": "string",
      "slug": "string",
      "category": "string",
      "end_date": "ISO8601 datetime",
      "markets": [
        {
          "id": "string",
          "question": "string",
          "outcomes": ["Yes", "No"],
          "prices": {
            "Yes": 0.65,
            "No": 0.35
          },
          "volume": 123456.78,
          "liquidity": 98765.43
        }
      ]
    }
  ],
  "updated_at": "ISO8601 datetime"
}
```

### orbitexch_events.json 结构

```json
{
  "events": [
    {
      "id": "string",
      "name": "string",
      "category": "string",
      "start_time": "ISO8601 datetime",
      "markets": [
        {
          "id": "string",
          "name": "string",
          "selections": [
            {
              "id": "string",
              "name": "string",
              "back_price": 1.85,
              "lay_price": 1.90,
              "back_liquidity": 1000.00,
              "lay_liquidity": 800.00
            }
          ]
        }
      ]
    }
  ],
  "updated_at": "ISO8601 datetime"
}
```

### market_matches.json 结构

```json
{
  "matches": [
    {
      "id": "string",
      "confidence": 0.95,
      "polymarket": {
        "event_id": "string",
        "market_id": "string",
        "question": "string"
      },
      "orbitexch": {
        "event_id": "string",
        "market_id": "string",
        "selection_id": "string",
        "name": "string"
      },
      "matched_at": "ISO8601 datetime"
    }
  ],
  "updated_at": "ISO8601 datetime"
}
```

---

## 计划中的数据库方案

### 数据库选型

| 选项 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| PostgreSQL | 成熟稳定、ACID、丰富功能 | 部署复杂 | 生产环境 |
| SQLite | 轻量、零配置 | 并发限制 | 开发/测试 |
| Redis | 高性能、实时 | 数据持久化复杂 | 缓存层 |

**推荐方案**: PostgreSQL + Redis 组合

### 表结构设计

#### platforms（平台表）

```sql
CREATE TABLE platforms (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    api_base_url VARCHAR(255),
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 初始数据
INSERT INTO platforms (name, api_base_url) VALUES
('polymarket', 'https://gamma-api.polymarket.com'),
('orbitexch', 'https://api.orbitexch.com');
```

#### events（事件表）

```sql
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    platform_id INTEGER REFERENCES platforms(id),
    external_id VARCHAR(100) NOT NULL,
    title VARCHAR(500) NOT NULL,
    category VARCHAR(100),
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    status VARCHAR(20) DEFAULT 'active',
    raw_data JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform_id, external_id)
);

CREATE INDEX idx_events_platform ON events(platform_id);
CREATE INDEX idx_events_category ON events(category);
CREATE INDEX idx_events_status ON events(status);
```

#### markets（市场表）

```sql
CREATE TABLE markets (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    external_id VARCHAR(100) NOT NULL,
    question VARCHAR(500),
    outcomes JSONB,
    status VARCHAR(20) DEFAULT 'active',
    raw_data JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, external_id)
);

CREATE INDEX idx_markets_event ON markets(event_id);
CREATE INDEX idx_markets_status ON markets(status);
```

#### prices（价格表）

```sql
CREATE TABLE prices (
    id SERIAL PRIMARY KEY,
    market_id INTEGER REFERENCES markets(id),
    outcome VARCHAR(100) NOT NULL,
    price DECIMAL(10, 6) NOT NULL,
    volume DECIMAL(20, 2),
    liquidity DECIMAL(20, 2),
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_prices_market ON prices(market_id);
CREATE INDEX idx_prices_recorded ON prices(recorded_at);

-- 分区表（按时间）
CREATE TABLE prices_history (
    LIKE prices INCLUDING ALL
) PARTITION BY RANGE (recorded_at);
```

#### market_matches（匹配表）

```sql
CREATE TABLE market_matches (
    id SERIAL PRIMARY KEY,
    source_market_id INTEGER REFERENCES markets(id),
    target_market_id INTEGER REFERENCES markets(id),
    confidence DECIMAL(5, 4) NOT NULL,
    match_method VARCHAR(50),
    status VARCHAR(20) DEFAULT 'pending',
    verified_by VARCHAR(50),
    verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_market_id, target_market_id)
);

CREATE INDEX idx_matches_confidence ON market_matches(confidence);
CREATE INDEX idx_matches_status ON market_matches(status);
```

#### arbitrage_opportunities（套利机会表）

```sql
CREATE TABLE arbitrage_opportunities (
    id SERIAL PRIMARY KEY,
    match_id INTEGER REFERENCES market_matches(id),
    opportunity_type VARCHAR(50),
    expected_profit DECIMAL(10, 4),
    required_capital DECIMAL(20, 2),
    source_price DECIMAL(10, 6),
    target_price DECIMAL(10, 6),
    status VARCHAR(20) DEFAULT 'detected',
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expired_at TIMESTAMP
);

CREATE INDEX idx_opportunities_status ON arbitrage_opportunities(status);
CREATE INDEX idx_opportunities_profit ON arbitrage_opportunities(expected_profit);
```

---

## 数据迁移计划

### 阶段1：准备

- [ ] 部署 PostgreSQL 服务
- [ ] 创建数据库和表结构
- [ ] 编写数据迁移脚本

### 阶段2：迁移

- [ ] 导入历史 JSON 数据
- [ ] 验证数据完整性
- [ ] 更新服务代码使用数据库

### 阶段3：清理

- [ ] 移除 JSON 文件依赖
- [ ] 归档历史 JSON 文件
- [ ] 更新文档

---

## 查询示例

### 获取平台所有活跃事件

```sql
SELECT e.*, p.name as platform_name
FROM events e
JOIN platforms p ON e.platform_id = p.id
WHERE e.status = 'active'
ORDER BY e.end_time;
```

### 获取高置信度匹配

```sql
SELECT
    mm.*,
    sm.question as source_question,
    tm.question as target_question
FROM market_matches mm
JOIN markets sm ON mm.source_market_id = sm.id
JOIN markets tm ON mm.target_market_id = tm.id
WHERE mm.confidence >= 0.9
ORDER BY mm.confidence DESC;
```

### 获取套利机会

```sql
SELECT
    ao.*,
    mm.confidence,
    sm.question
FROM arbitrage_opportunities ao
JOIN market_matches mm ON ao.match_id = mm.id
JOIN markets sm ON mm.source_market_id = sm.id
WHERE ao.status = 'detected'
AND ao.expected_profit > 0.01
ORDER BY ao.expected_profit DESC;
```

---

## 相关文档

- [系统架构](architecture.md)
- [测试策略](testing-strategy.md)
