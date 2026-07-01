# Common 详细设计

> **定位**:`src/arbitrage/common/` 存放跨组件共享的轻量领域契约 / 注册表 / 工具。本文只记录有跨组件语义的 common 模块;纯工具函数见 `utils_api.md`。

## 1. Opportunity Metadata 契约(#106,已落地,2026-06-14)

`src/arbitrage/common/opportunity.py` 是 Strategy / Risk / Execution 共用的 opportunity metadata 单一解析实现。

| API | 用途 |
|---|---|
| `OpportunityMeta` | `opportunity_id / pair_id / leg_key / expected_legs / intent` 结构化视图 |
| `new_opportunity_id()` | `PlaceBetsAction` 为一次 action fire 生成机会 ID |
| `tags_from_meta(meta)` | submitter 把 spec metadata 写入 `Order.tags` |
| `meta_from_order(order)` / `meta_from_tags(tags)` | Risk / Execution 从 `Order.tags` 读取 metadata |
| `order_intent(order)` | Risk 读取 `arb:intent`,默认 `arbitrage` |
| `RISK_LEG_DENIED_TOPIC` | `risk.opportunity.leg_denied` topic 常量 |

**约束**:
- metadata 的权威载体是 `Order.tags`,不是 `SubmitOrder.params`,因为 Risk deny 和 Execution barrier 都以 `order` 为入口。
- `expected_legs` 只包含真实下单腿;不发送 0 qty 空单。
- common 模块只负责解析 / 构造,不维护 opportunity 状态;状态机归 Execution barrier。

## 2. VenueExecutionLiveness(已落地,2026-06-15)

`src/arbitrage/common/venue_liveness.py` 是 execution/reconciliation 与 Risk 之间共享的 venue 执行真相可信度 registry。横切机制归属见 `_cross-cutting/synchronization.md §8.5`;本节只记录 common API。

| API | 用途 |
|---|---|
| `mark_order_alive(venue)` / `mark_order_dead(venue)` | 写订单真相可信位 |
| `mark_position_alive(venue)` / `mark_position_dead(venue)` | 写持仓真相可信位 |
| `order_alive(venue)` / `position_alive(venue)` | 分别读取 order / position 位 |
| `venue_alive(venue)` | 派生判断:`order_alive && position_alive` |
| `all_alive(venues)` | Risk 检查 required venues 的便捷入口 |
| `snapshot()` | 低频观测 / 测试断言用 |

**约束**:
- 未知 venue 默认 `false`,启动后 fail-closed。
- 不存第三份 `venue_alive`;它始终由 order/position 两位派生。
- registry 不在每次 submit 前翻 false。只有执行端明确进入 stuck/reconcile 失败等“真相不可信”路径时写 dead。
- key 统一为 venue 大写字符串;可传 `Venue` 对象或字符串。

## 3. PairRegistry(已落地,#34 / #116 补充)

`src/arbitrage/common/pair_registry.py` 是 matching 写、risk/portfolio/strategy/session 读的 pair 映射 registry。语义归属在 matching 组件,common 只承载共享实现。

| API | 用途 |
|---|---|
| `register(pair_id, instrument_ids)` | matching 成功后注册该 pair 的全部 leg instrument id |
| `get(instrument_id)` | 下游从任一 instrument id 反查 pair_id |
| `instrument_ids_for_pair(pair_id)` | Portfolio 反查该 pair 的全部 instrument id,用于从 cache instrument.info 得到完整 outcome 集合 |
| `unregister_pair(pair_id)` | 结束/eviction 时清理该 pair 的映射 |
| `all_pair_ids()` | 低频观测 / 扫描用 |

**约束**:
- register/get 两侧都用 `str(instrument_id)` 归一,避免 matching 写字符串、consumer 传 `InstrumentId` 对象导致 miss。
- `instrument_ids_for_pair` 返回字符串集合;读取 instrument 详情的一方负责转回 `InstrumentId` 并从 NT Cache 取 instrument。
- MatchingActor 是唯一写者;其它组件只读或按 matching/eviction 归属调用 `unregister_pair`。

## 4. 控制台命令(`control.py`,#119)

`src/arbitrage/common/control.py` = Web 控制台 → 组件的运行时控制命令(topic 常量 + frozen dataclass):
`SetTradingStateCommand` / `SetRiskParamsCommand` / `SetArbitrageParamsCommand` / `SetRefreshIntervalCommand`。**契约真理源在 web §8.3**(WebGatewayActor 单一生产者 publish,risk/strategy/matching 订阅 apply);本目录只承载共享类型定义。

`SetRiskParamsCommand` 使用 `None` 表示不覆盖该字段;当前热字段包括 `match_tp` / `match_sl` / `min_probability` / `max_probability`。`SetArbitrageParamsCommand` 承载普通套利运行默认值 `share` / `max_leg_share` / `fx`;这些字段不归 Risk 配置所有。

## 5. Venue 配置 dataclass(`venue_configs.py`)

`src/arbitrage/common/venue_configs.py` 承载 discovery / adapter factory 共享的轻量 venue 配置:
`BrowserConfig`、`SportConfig`、`PolymarketVenueConfig`、`OrbitExchVenueConfig`、`SharpExchVenueConfig`。

SharpExch 第一阶段只新增与 OE 同形的 `SharpExchVenueConfig(enabled, browser, sports)`。真实 SE
浏览器/API/WS 接线仍归 `architectures/sharpexch/architecture.md`;common 只提供跨模块传参类型,不定义
SE discovery 算法。

## 6. ArbContext factory 注入字段

`src/arbitrage/bootstrap.py:ArbContext` 是 NT factory 固定签名之外的进程级依赖注入通道。PM/OE
字段已有生产接线;SharpExch 第一阶段新增同形字段,并由 launcher 按
`venues.sharpexch.enabled` 显式 opt-in 注册 SE factories:

| 字段 | 用途 |
|---|---|
| `se_session_timeout_secs` | `ArbSharpExchLiveExecClientFactory` 注入 SE execution session timeout |
| `se_discovery_config` | `SharpExchLiveDataClientFactory` 判断是否构造 SE discovery/provider |
| `se_sport_aliases` / `se_competition_aliases` | SE Provider 写 Q9 统一字段前的别名映射 |
| `se_instrument_provider` | SE Data factory 构造 provider 后回写,供后续 runtime/测试读取 |
| `se_browser_manager` | SE Data/Exec factory 共享同一个 Playwright browser manager |

这些字段只承载 factory 注入状态;默认 `venues.sharpexch.enabled=false` 时不改变现有 PM/OE
runtime 流程。
