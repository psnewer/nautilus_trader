# 市场匹配服务 - 架构设计

## 概述

市场匹配服务负责将不同平台（Polymarket、OrbitExch）发现的比赛事件进行匹配，识别出相同的比赛。

## 模块结构

```
market_matching/
├── __init__.py           # 模块导出
├── requirements.md       # 需求文档
├── architecture.md       # 架构文档（本文件）
├── config.py             # 配置和映射表
├── normalizer.py         # 名称标准化预处理器
├── engine.py             # 匹配引擎
└── service.py            # 服务入口
```

## 数据流

```
Polymarket Events                  OrbitExch Events
       │                                  │
       ▼                                  ▼
┌──────────────────────────────────────────────────┐
│              Normalizer (预处理)                  │
│  - 标准化 sport 名称                              │
│  - 标准化 competition 名称                        │
│  - 预处理队名（去掉 / 等）                        │
└──────────────────────────────────────────────────┘
       │                                  │
       ▼                                  ▼
   标准化后的 Events              标准化后的 Events
       │                                  │
       └──────────────┬───────────────────┘
                      │
                      ▼
         ┌───────────────────────────────┐
         │       MatchEngine (匹配)       │
         │  1. 按 sport+competition 分组  │
         │  2. 组内队名匹配 (get_similar) │
         └───────────────────────────────┘
                      │
                      ▼
              MatchedPair 列表
```

## 匹配规则

### 1. 预处理规则

**Sport 名称标准化**：
| 原始名称 | 标准名称 |
|---------|---------|
| Football | Soccer |
| football | Soccer |

**Competition 名称标准化**：
| 原始名称 | 标准名称 |
|---------|---------|
| EPL | English Premier League |
| sea | Italian Serie A |
| La Liga | Spanish La Liga |

**队名预处理**：
- 去掉 `/` 符号：`Muhammad/Routliffe` → `MuhammadRoutliffe`

### 2. 匹配规则

1. **sport 和 competition 必须完全相等**（预处理后）
2. **队名匹配**使用 `get_similar` 函数
3. **选择最佳匹配**：
   - 优先选择 `get_similar` 返回值最大的
   - 返回值相等时，选择匹配字符数最多的

## 数据模型

```python
@dataclass
class NormalizedEvent:
    """标准化后的比赛事件"""
    venue: str                    # 平台标识
    sport: str                    # 标准化后的 sport
    competition: str              # 标准化后的 competition
    home_team: str                # 原始主队名
    away_team: str                # 原始客队名
    home_team_normalized: str     # 预处理后的主队名（用于匹配）
    away_team_normalized: str     # 预处理后的客队名（用于匹配）
    original_event: MatchEvent    # 原始事件数据

@dataclass
class MatchedPair:
    """匹配的市场对"""
    pair_id: str                  # 唯一标识
    sport: str                    # 标准 sport
    competition: str              # 标准 competition
    polymarket_event: NormalizedEvent
    orbitexch_event: NormalizedEvent
    home_similarity: int          # 主队相似度分数
    away_similarity: int          # 客队相似度分数
    confidence: float             # 置信度 (0-1)
```

## 配置结构

```python
@dataclass
class MarketMatchingConfig:
    enabled: bool = True
    sport_aliases: dict[str, str]       # sport 别名映射
    competition_aliases: dict[str, str] # competition 别名映射
    min_similarity: int = 1             # 最小相似度阈值
```
