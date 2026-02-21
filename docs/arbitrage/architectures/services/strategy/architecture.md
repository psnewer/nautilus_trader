# 策略服务（Strategy）消息架构

## 消息发布

策略服务通过 NautilusTrader MessageBus 发布以下消息：

1. 套利机会消息
   - 主题：`arbitrage.opportunity.{pair_id}`
   - 数据：`OpportunityMessage`
   - 发布位置：`src/arbitrage/services/strategy/service.py::_publish_opportunity`
   - 触发条件：策略评估通过且风控检查允许

## 消息订阅

1. 赔率更新（来自 OddsSubscription）
   - 订阅主题：`arbitrage.odds.*`
   - 处理：触发赔率相关信号计算（rebate, mean_rebate, multi-way）

2. 比赛状态（来自 OddsSubscription）
   - 订阅主题：`arbitrage.match_status.*`
   - 处理：触发状态相关信号计算（live, pre-match）

3. 持仓返水率（来自 RiskService）
   - 订阅主题：`arbitrage.way_rebate.*`
   - 处理：更新 `_way_rebate_cache`，策略评估时从缓存读取

## 同步依赖（DI）

- `risk_service.check_risk()` — 风控门控，同步调用，保留 DI 注入
- `odds_service` — 用于获取持仓数据

## 说明

- StrategyService 不再通过回调通知 ExecutionService，改为通过 MessageBus 发布 `OpportunityMessage`。
- way_rebate 不再每次赔率更新时主动读取 RiskService，改为由 RiskService 推送、StrategyService 缓存。
