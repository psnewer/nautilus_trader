# 风控服务（Risk）消息架构

## 消息发布

风控服务通过 NautilusTrader MessageBus 发布以下消息：

1. 持仓返水率消息
   - 主题：`arbitrage.way_rebate.{pair_id}`
   - 数据：`WayRebateMessage`
   - 发布位置：`src/arbitrage/services/risk/service.py::_publish_way_rebate`
   - 触发条件：
     - 会话完成后 `refresh_pair_position()` 刷新持仓
     - `load_historical_positions()` 加载历史持仓

## 消息订阅

1. 会话完成（来自 ExecutionService）
   - 订阅主题：`arbitrage.session_complete.*`
   - 处理：查询 odds_service 获取持仓数据 → 刷新持仓 → 发布 way_rebate
   - 订阅位置：`src/arbitrage/services/risk/service.py::set_msgbus`

## 同步依赖（DI）

- `odds_service` — 用于查询 Polymarket/OrbitExch 持仓数据和映射
- `check_risk()` — 被 StrategyService 同步调用，作为机会发布前的门控

## 说明

- RiskService 不再被 ExecutionService 直接调用 `refresh_pair_position()`，改为订阅 `session_complete` 消息后自行刷新。
- way_rebate 值只在 session 完成导致 legs 变化后才改变，由 RiskService 主动推送到 MessageBus。
- StrategyService 缓存 way_rebate，不再每次赔率更新时主动读取。
