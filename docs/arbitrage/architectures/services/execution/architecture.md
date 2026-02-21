# 执行服务（Execution）消息架构

## 消息发布

执行服务通过 NautilusTrader MessageBus 发布以下消息：

1. 会话完成消息
   - 主题：`arbitrage.session_complete.{pair_id}`
   - 数据：`SessionCompleteMessage`
   - 发布位置：`src/arbitrage/services/execution/service.py::_publish_session_complete`
   - 触发条件：ExecutionOrchestrator 会话结束

## 消息订阅

1. 套利机会（来自 StrategyService）
   - 订阅主题：`arbitrage.opportunity.*`
   - 处理：解析消息，调度 `on_opportunity()` 异步执行
   - 订阅位置：`src/arbitrage/services/execution/service.py::set_msgbus`

## 同步依赖（DI）

- `odds_service` — 用于获取 token_id、selection_id、页面引用等
- `arbitrage_config` — 用于计算订单大小

## 说明

- ExecutionService 不再直接引用 RiskService，会话完成后通过 MessageBus 发布 `SessionCompleteMessage`。
- RiskService 订阅该消息后自行查询持仓并刷新。
