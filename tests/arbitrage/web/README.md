# web 测试(占位)

待 Step 7 启动时展开。

对应章节: `refactor.md §5.7`

## 锁定的关键性约束

- `WebGatewayActor(Actor)` 与 NT TradingNode 同进程同 loop,FastAPI uvicorn 协程在 `on_start` 启动
- HTTP 路由 → MessageBus 命令 → 对应 Actor 处理 → 响应回 HTTP
- WebSocket 推送 → 订阅 NT MessageBus 上的 `OrderBookDelta` / `MatchedPair` / **`AccountState`** → 转 JSON 推前端
- **承担余额数据推送**(替代独立的 BalanceMonitorActor):浏览器订阅 AccountState 自己显示余额数字,余额低 / 熔断由用户看着判断
- 配置类 HTTP POST(如修改 `refresh_interval`)→ publish `config.{venue}.refresh_interval` 命令 → `InstrumentRefresher` 收到更新

## 预期用例(摘要)

- web-7.1: HTTP `GET /matched_pairs` 通过 MessageBus request/response 拿数据
- web-7.2: WebSocket 推送 `OrderBookDelta`(NT 类型转 JSON)
- web-7.3: **WebSocket 推送 `AccountState`**(NT 类型转 JSON,前端显示余额) ← 替代 BalanceAlert
- web-7.4: 修改 `refresh_interval` 通过 MessageBus 通知 Refresher,运行时生效(Q3)
- web-7.5: FastAPI uvicorn 与 NT loop 共存,优雅停机
- web-7.6: HTTP `GET /positions/{pair_id}` → 调 `portfolio.way_rebate(pair_id)` + `way_rebates_by_venue(pair_id)` → 序列化 JSON 推前端(Q14,§6.9)
- web-7.7: HTTP `GET /positions/global_min_rebate_sum` → 调 `portfolio.global_min_rebate_sum()` → JSON 数字(Q14)
