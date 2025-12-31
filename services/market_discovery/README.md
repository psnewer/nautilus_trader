# 市场发现服务

独立的市场发现服务，支持多平台。

## 架构

\\\
MarketDiscoveryService (主服务)
     OrbitExchAdapter (OrbitExch 平台)
     PolymarketAdapter (Polymarket 平台)
     ...更多平台
\\\

## 运行

\\\ash
cd services/market_discovery
python main.py
\\\

## 输出

市场数据保存在: \data/markets/\
- \latest_markets.json\ - 最新的完整快照
- \{platform}_{timestamp}.json\ - 按平台和时间的历史数据

## 添加新平台

1. 创建新的适配器类继承 \PlatformAdapter\
2. 实现 \discover_markets()\ 方法
3. 在 \main.py\ 中注册适配器
