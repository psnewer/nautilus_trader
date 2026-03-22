# 风控服务（Risk）消息架构

## 消息发布

风控服务通过 NautilusTrader MessageBus 发布以下消息：

1. 持仓返水率消息
   - 主题：`arbitrage.way_rebate.{pair_id}`
   - 数据：`WayRebateMessage`
   - 发布位置：`src/arbitrage/services/risk/service.py::_publish_way_rebate`
   - 触发条件：
     - 会话完成后 `_refresh_all_way_rebates()` 刷新持仓
     - `load_historical_positions()` 加载历史持仓
     - 健康检查循环中 `_run_health_check()` 刷新 way_rebate

2. Pair 活跃互斥（pair_id 锁定）
   - 主题：`arbitrage.pair_activity.{pair_id}`
   - 数据：`PairActivityMessage`
   - 发布位置：`src/arbitrage/services/risk/service.py::_publish_pair_activity`
   - 触发条件：
     - 收到 `session_complete` 后进入 post-session 刷新流程时发送 `is_active=true`
     - 解锁依赖 OddsSubscription 的超时清理（Risk 不主动发送 `is_active=false`）

## 消息订阅

1. 会话完成（来自 ExecutionService）
   - 订阅主题：`arbitrage.session_complete.*`
   - 处理：查询 odds_service 获取持仓数据 → 刷新持仓 → 发布 way_rebate
   - 订阅位置：`src/arbitrage/services/risk/service.py::set_msgbus`

## 同步依赖（DI）

- `odds_service` — 用于查询 Polymarket/OrbitExch 持仓数据和映射
- `execution_service` — 用于健康检查访问 Polymarket client（`get_ok()`）和 OrbitExch pages（CSRF token）
- `check_risk()` — 被 StrategyService 同步调用，作为机会发布前的门控

## 健康检查

RiskService 承担全局健康检查职责，通过后台循环定期检测三项指标：

1. **Polymarket CLOB**：调用 `execution_service._polymarket_executor._client.get_ok()`
2. **OrbitExch CSRF**：遍历 `execution_service._orbitexch_executor._pages` 检查 CSRF-TOKEN cookie
3. **Way Rebate 刷新**：调用 `odds_service.refresh_all_positions_and_orders()` + `_refresh_all_way_rebates()`

三项全部通过时 `_health_ok = True`，否则 `check_risk()` 在 `execution_enabled` 检查之后、风控逻辑之前直接拒绝机会。

配置：`RiskConfig.health_check_interval_sec`（默认 30s）

接线位置：`AppState.ensure_execution_registered()` 末尾调用 `risk_service.set_execution_service()` 和 `risk_service.start_health_check_loop()`。

## 说明

- RiskService 不再被 ExecutionService 直接调用 `refresh_pair_position()`，改为订阅 `session_complete` 消息后自行刷新。
- way_rebate 值在 session 完成和健康检查循环中刷新，由 RiskService 主动推送到 MessageBus。
- StrategyService 缓存 way_rebate，不再每次赔率更新时主动读取。
- 健康检查从 ExecutionService 迁移到 RiskService，利用已有的 `check_risk()` 门控链路在上游拒绝机会，不在 ExecutionService 中阻塞等待。
