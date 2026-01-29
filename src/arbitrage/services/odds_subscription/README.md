# 赔率订阅服务

实时订阅 Polymarket 和 OrbitExch 的赔率数据。

## 功能特性

- ✅ Polymarket 赔率订阅（通过 Gamma API 和 WebSocket）
- ✅ OrbitExch 赔率监控（通过 Playwright 长期浏览器）
- ✅ 数据超时监控和自动刷新
- ✅ Web UI 实时显示
- ✅ 支持胜平负（Win-Draw-Win）和胜负（Win-Lose）市场

## 使用流程

### 1. 市场发现
```
访问 Web UI → Market Discovery 标签页 → Run Discovery
```
这会抓取 Polymarket 和 OrbitExch 的比赛信息（包括 sport_id 和 competition_id）。

### 2. 市场匹配
```
Market Matching 标签页 → Run Matching
```
匹配相同的比赛，生成 matched_pairs。

### 3. 赔率订阅
```
Odds Monitor 标签页 → Subscribe to Odds
```
基于 matched_pairs 启动赔率订阅：
- **Polymarket**: 使用 WebSocket 订阅价格更新（当前为 HTTP 轮询占位符）
- **OrbitExch**: 打开浏览器并持续监听页面数据变化（浏览器保持打开）

### 4. 查看赔率
订阅成功后，赔率数据会自动更新（默认每 5 秒刷新）。

显示格式：
- **Polymarket**: bid / ask（买价 / 卖价）
- **OrbitExch**: back / lay（支持价 / 反对价）
- **更新时间**: 绿色(<2分钟) / 黄色(2-5分钟) / 红色(>5分钟)

## API 端点

### GET /api/odds/status
获取订阅状态
```json
{
  "running": true,
  "subscriptions_count": 5
}
```

### GET /api/odds/subscriptions
获取订阅列表
```json
{
  "subscriptions": [
    {
      "pair_id": "poly_123_orbit_456",
      "sport": "Soccer",
      "competition": "English Premier League",
      "polymarket_event_id": "123",
      "last_update_sec_ago": 10,
      "is_stale": false
    }
  ]
}
```

### GET /api/odds/latest?pair_id={id}
获取最新赔率（可选指定 pair_id）
```json
{
  "odds": {
    "poly_123_orbit_456": {
      "polymarket": {
        "home": {"bid": 0.52, "ask": 0.53, "timestamp": 1234567890},
        "away": {"bid": 0.47, "ask": 0.48, "timestamp": 1234567890}
      },
      "orbitexch": {
        "home": {"back": 1.92, "lay": 1.95, "timestamp": 1234567890},
        "away": {"back": 2.10, "lay": 2.15, "timestamp": 1234567890}
      }
    }
  }
}
```

### POST /api/odds/subscribe
启动订阅
```json
{
  "status": "started",
  "pairs_count": 5
}
```

### POST /api/odds/unsubscribe
停止订阅
```json
{
  "status": "stopped"
}
```

## 配置参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `staleness_timeout_sec` | int | 300 | 数据超时时间（秒），超时后自动刷新 |
| `orbitexch_zoom_level` | float | 0.8 | OrbitExch 页面缩放比例 |
| `orbitexch_page_refresh_sec` | int | 600 | 定期刷新页面间隔（秒） |
| `orbitexch_username` | str | "" | OrbitExch 账号（需要） |
| `orbitexch_password` | str | "" | OrbitExch 密码（需要） |

## 注意事项

1. **OrbitExch 浏览器**: 浏览器会长期保持打开状态，持续监听数据更新
2. **登录要求**: OrbitExch 需要提供账号密码
3. **Polymarket WebSocket**: 当前使用 HTTP 轮询占位符，未来会替换为真实 WebSocket
4. **数据新鲜度**: 超过 5 分钟未更新的数据会触发自动刷新
5. **浏览器刷新**: OrbitExch 超时时只刷新页面，不关闭浏览器

## 架构设计

```
OddsSubscriptionService
├── PolymarketOddsClient
│   ├── 查询 event tokens (Gamma API)
│   └── 订阅价格更新 (WebSocket / HTTP 轮询)
└── OrbitExchOddsClient
    ├── 启动浏览器（长期打开）
    ├── 登录 OrbitExch
    ├── 导航到 competition 页面
    ├── 设置页面缩放
    └── 持续监听赔率变化
```

## 数据流

```
1. 用户点击 "Subscribe to Odds"
   ↓
2. Web Gateway 调用 POST /api/odds/subscribe
   ↓
3. OddsSubscriptionService.subscribe_matched_pairs(matched_pairs)
   ↓
4. 为每个 pair:
   - PolymarketOddsClient.subscribe_event(event_id)
   - OrbitExchOddsClient.subscribe_competition(sport_id, competition_id)
   ↓
5. 客户端通过回调更新 latest_odds 缓存
   ↓
6. Heartbeat 监控数据新鲜度，超时则刷新
   ↓
7. Frontend 定期轮询 GET /api/odds/latest 显示数据
```

## 待改进

- [ ] Polymarket: 实现真实 WebSocket（替换 HTTP 轮询）
- [ ] OrbitExch: 优化页面选择器（适应页面结构变化）
- [ ] 添加 WebSocket 推送到前端（替换 HTTP 轮询）
- [ ] 集成 MessageBus 事件驱动架构
- [ ] 历史赔率数据存储
