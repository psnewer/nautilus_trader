# 赔率订阅服务（OddsSubscription）消息架构

## 消息发布

赔率订阅服务通过 NautilusTrader MessageBus 发布以下消息：

1. 赔率更新消息
   - 主题：`arbitrage.odds.{venue}.{pair_id}.{market_type}`
   - 数据：`OddsUpdateMessage`
   - 发布位置：`src/arbitrage/services/odds_subscription/service.py::_publish_odds_update`

2. 比赛状态消息
   - 主题：`arbitrage.match_status.{pair_id}`
   - 数据：`MatchStatusMessage`
   - 发布位置：`src/arbitrage/services/odds_subscription/service.py::_on_match_status_update`

## 消息订阅

1. StrategyService（策略服务）
   - 订阅主题：`arbitrage.odds.*`、`arbitrage.match_status.*`
   - 赔率消息触发信号：`rebate`、`mean_rebate`、`multi-way`
   - 比赛状态消息触发信号：`live`、`pre-match`
   - 订阅位置：`src/arbitrage/services/strategy/service.py::set_msgbus`

2. 其他订阅者
   - 当前暂无其他订阅者
   - 如后续新增风控或监控模块，可订阅同主题复用消息流

## 说明

- OddsSubscriptionService 仅负责发布消息，不再保留回调注册方式。
- StrategyService 通过订阅消息触发信号计算与策略评估。
