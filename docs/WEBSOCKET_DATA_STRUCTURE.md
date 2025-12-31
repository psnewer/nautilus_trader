# OrbitExch WebSocket 数据结构

## 消息格式总结

### 赔率消息 (Price Update)

**顶层字段**:
- id: Market ID (例如: "1.252123015")
- mainEventId: Event ID (例如: "35093863")
- mainEventName: 赛事名称
- marketNameWithParents: 市场类型 ("Match Odds", "Over/Under 2.5 Goals")
- status: 市场状态 ("OPEN", "SUSPENDED", "CLOSED")
- inPlay: 是否进行中
- bettingEnabled: 是否可以下注
- tv: 总成交量

**Runners (rc) - 选手/结果**:
每个 runner 包含:
- id: Selection ID
- bdatb: Back prices (可买入) - 数组，按价格排序
  - index: 档位 (0=最佳价格)
  - odds: 赔率
  - amount: 可用金额
- bdatl: Lay prices (可卖出) - 数组，按价格排序
  - index: 档位
  - odds: 赔率
  - amount: 可用金额
- tv: 该选手的成交量
- locked: 是否锁定

**Market Definition**:
- marketType: 市场类型
- numberOfWinners: 赢家数量
- numberOfActiveRunners: 活跃选手数
- status: 状态
- runners: 选手定义数组
  - selectionId: 选手 ID
  - status: 状态 ("ACTIVE", "REMOVED")

## 关键数据点

### 最佳价格
- Best Back: rc[i].bdatb[0].odds - 可以立即买入的最佳赔率
- Best Lay: rc[i].bdatl[0].odds - 可以立即卖出的最佳赔率

### 深度
每个选手有 3 档价格:
- Index 0: 最佳价格
- Index 1: 次佳价格
- Index 2: 第三档价格

## 套利检测示例

检测同一赛事不同市场的套利机会:
主队 Back @2.26 vs 客队 Lay @3.05
如果 1/2.26 + 1/3.05 < 1，存在套利机会

计算:
overround = (1/2.26) + (1/3.05)
if overround < 1:
    profit_margin = (1 - overround) * 100
    # 套利机会存在
