# Web 组件详细设计(占位 / 暂不迁移)

> **状态:占位**。`WebGatewayActor` 在初设 `refactor.md §5.7` 仅占位,且**当前不属迁移范围**(用户 2026-05-21)。
> 对应初设 Step 7。

## 预期职责(方向,未细化)

- FastAPI 与 `TradingNode` 同进程同 asyncio loop;HTTP 路由桥接 MessageBus。
- **行情/状态格式适配**:订阅 NT `OrderBookDelta` / `MatchedPair` / `AccountState` 等 → 转 JSON 推前端。
- 替代旧 `BalanceMonitorActor`:推 `AccountState` 给前端,余额低/熔断**由用户看着判断**(系统层不告警)。
- 配置类 HTTP POST(如 `refresh_interval`、strategy/signal 配置)→ publish MessageBus → 对应 Actor 收。

## 待启动展开

- 前端进程是否与 TradingNode 解耦(同进程 vs Redis MsgBus 独立进程)
- way_rebate HTTP 查询(调 `portfolio.way_rebate`)
- strategy/signal 面板配置写入路径(关联 Strategy 配置模型)
- 标准模板 7 节
