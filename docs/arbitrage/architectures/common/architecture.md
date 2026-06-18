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
