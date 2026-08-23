# 横切：订单簿源帧与二元市场边界

> **定位**：跨 DataClient、DataEngine、Matching、Strategy 的行情一致性契约。
> **状态**：2026-08-23 已落地并完成离线测试，尚未 live 验证。
> **决策理由**：见 `refactor.md #357/#358`。本文只定义现行机制。

## 1. 身份边界

| 身份 | 含义 | OE/SE 三项盘示例 |
|---|---|---|
| `source_market_id` | venue 一条上游行情帧的物理市场 ID | Match Odds `marketId` |
| `binary_market_id` | Strategy 使用的互斥 yes/no 市场 ID | `{marketId}:{selectionId}` |
| `selection_id` | venue 的真实 runner | home、draw 或 away selection |
| `claim` | 该 instrument 对 selection 的投影 | `yes` / 合成 `no` |

二项盘的两个真实 selection 共同组成一个二元市场，故二者
`binary_market_id = source_market_id`。三项盘的 home/draw/away 不是六腿互斥市场；
每个真实 selection 与其合成 no 独立组成一个二元市场，故产生三个
`binary_market_id`。`instrument.market_id` 继续保存 venue 的
`source_market_id`，`instrument.info["binary_market_id"]` 保存策略身份。

PM condition 本身就是一个二元市场，因此两种 ID 相同。

## 2. 事件契约

- `MarketOrderBookDeltas`：一个 `binary_market_id` 在本轮实际变化的 instrument。
- `OrderBookFrameDeltas`：一条 `source_market_id` 上游消息内，零散变化归组后的全部
  `MarketOrderBookDeltas`。
- Strategy / Matching 只订阅前者；后者是 DataClient 到 DataEngine 的内部源帧信封。
- 一个逻辑市场事件可只含本帧变化的成员，未变化成员沿用 Cache 当前 book；同一批内
  不允许重复 instrument，不允许跨 venue、跨已登记成员。

## 3. 应用与发布顺序

```text
DataClient：解析一条源消息 → 按 binary_market_id 分组 → OrderBookFrameDeltas
DataEngine：校验整帧 → 应用整帧所有 inner OBD → 逐 binary market 发布事件
Consumer：收到 MarketOrderBookDeltas → 从已更新完成的 Cache 读取 pair 报价
```

DataEngine 在任何写入前验证整帧的 data type、source 映射、订阅成员与 managed book；
任一市场非法则整帧拒绝，不能部分更新。全部 inner OBD 应用完成后才发布逻辑市场事件，
因此同一 OE/SE Match Odds 源帧里的 home/draw/away 更新对所有消费者同时可见，但只变化
home 时只发布 home 的二元市场事件，不误触发 draw/away 策略评估。

MessageBus handler priority 不承担跨 instrument 原子性；它只排序同一次 publish 的同步
handler。本契约在 publish 之前由 DataEngine 建立完整 Cache 可见性。

## 4. 订阅与组件职责

- Provider：写入 `binary_market_id`；2-way 共用源 market ID，3-way 按 selection 派生。
- `market_book_subscriptions`：只对调用方给出的 pair 腿按
  `(venue, binary_market_id, source_market_id)` 分组，不扫描同一物理 market 的其它 selection。
- DataClient：验证订阅成员的 source/binary 身份；把一条 WS 消息打包成一个源帧。
- DataEngine：创建 managed books、保存逻辑成员与 source 映射、整帧校验/应用、逐逻辑市场发布。
- Matching / Strategy：按 `binary_market_id` 订阅，不直接订阅 `OrderBookFrameDeltas`。
- Execution：venue IO 始终使用真实 `instrument.market_id` / `selection_id`；不得把
  `binary_market_id` 发给 venue。

PM snapshot、snapshot 夹增量、单腿 resnapshot 与 tick-size reset 仍由 PM DataClient
原有状态机处理，最终都通过同一个源帧出口进入 DataEngine。

## 5. 验收

- DataEngine：同源帧的多个二元市场全部更新后才开始回调；非法成员整帧无部分写入。
- OE/SE：三项完整帧生成三个二元市场；只含 home 的帧只生成 home 市场。
- Provider/Matching：二项盘形成一组，三项盘形成三组，每组恰好 yes/no 两腿。
- PM：同 condition 仍是一组，snapshot/retry/reset 行为不退化。

对应离线测试见 DataEngine unit tests、各 adapter README，以及 Matching/Strategy README
中的 #357/#358 用例；真钱 live 验证不在本次范围。
