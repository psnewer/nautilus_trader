# 横切：订单簿源帧与二元市场边界

> **定位**：跨 DataClient、DataEngine、Matching、Strategy 的行情一致性契约。
> **状态**：2026-08-31 已落地并完成离线测试，尚未 live 验证。
> **决策理由**：见 `refactor.md #357/#358/#362/#363/#366`。本文只定义现行机制。

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

### 3.1 行情连接失效帧（#359/#360）

OE/SE 的 prices feed 发生 `close:prices` 或 `liveness_timeout` 且通过既有
shutdown、reload-in-flight、page existence 与 cooldown 门控后，DataClient 必须在
competition 页 reload 前发布失效帧：

1. 按断线 `page_key` 从订阅路由中选出其负责的 `source_market_id`；
2. 按 `binary_market_id` 分组，并为该逻辑市场的全部已订阅成员生成 NT 原生
   `OrderBookDelta.clear(...)`；
3. 每个 source market 只发布一个 `OrderBookFrameDeltas`。DataEngine 先清空其中全部
   managed books，再逐 binary market 发布 `MarketOrderBookDeltas`；
4. 完成同步 CLEAR 后才调度 reload。新完整快照到达前，Cache 的 best bid/ask 均为空，
   其它事件即使唤醒 Strategy 也不能复用断线前赔率。

失效范围以订阅路由为准，不扫描全 venue，也不直接改 Cache。一个 competition 页断线
不得清空其它页。reload task 排队前先置 reload-in-flight，重复 close/timeout 不重复发帧。

PM 由 NT Rust `WebSocketClient` 在连接状态成功从 `Active` 转为 `Reconnect` 时触发
`post_disconnection`；Python binding 将回调安全调度到 event loop。PM WS wrapper 按
`client_id` 提供该分片的 token 集，DataClient 随即发布 CLEAR。PM 多连接池的失效范围是
**断线分片**而非整个 venue：仅清该分片 token 对应的 local/DataEngine books；一个二元市场
的两腿若跨分片，健康腿不清。主动 shutdown 不触发回调。重连后的重新订阅与 snapshot 恢复
沿用原状态机；不得延后至 `post_reconnection` 才清空。

### 3.2 源市场 single-flight 与完成回执（#362）

DataEngine 的 `data_queue` 是无损 FIFO；行情洪峰时不能靠丢队列项、扫描队列或在消费者中
批次让权来限流。每个 DataClient 因此在进入 DataEngine 前按
`(venue, source_market_id)` 使用 `MarketFrameConflater`：同一 source market 最多有一个
带 `frame_id` 的 `OrderBookFrameDeltas` 在途；在途期间到达的新帧仍全部进入 venue adapter
的解析/状态更新路径，但只把各 instrument 的最新完整快照保留在 pending 中。前一帧完成后，
有 pending 则立即组成下一帧发送，没有则回到 idle。不同 source market 互不阻塞。

pending 只能保存可独立重放的完整快照。OE/SE runner 帧本来就是
`CLEAR + ADD` 全深度快照；双边无档时完整快照退化为单独 `CLEAR`，不能因“无 ADD”而跳过。
PM 普通增量在合流前必须从 `_local_books` 重建本次变化 instrument
的当前完整 `CLEAR + ADD` 快照。相邻普通 pending 按 instrument last-write-wins 合并，再按
`binary_market_id` 重新分组；这会省略已经被更新状态覆盖的中间行情，但不会丢失最新订单簿
状态。断线 CLEAR 是显式 barrier，必须独占一个 pending 段，之后到达的恢复快照不得覆盖或
越过它。

DataEngine 对 `frame_id > 0` 的每条源帧，在成功、校验拒绝或异常三个终态都只发布一次
`OrderBookFrameProcessed` 到 venue completion topic。`applied` 表示该帧是否已经完整写入
Cache，而不是 Strategy/Matching 是否成功处理：若 Cache 已写完、随后某个 market 消费者
抛错，回执仍为 `applied=true`。DataClient 只接受当前在途 `frame_id` 的回执；退订会清掉
对应 source 状态，迟到回执不得重新发送旧 pending。`applied=false` 时丢弃 pending 并阻塞
该 source，直到后续订阅生命周期重新激活，避免在未知 Cache 基线上继续合流。

`frame_id=0` 保留旧调用方兼容语义：DataEngine 正常应用，但不发布 completion。该机制不改
`ThrottledEnqueuer`，也不改变 DataEngine 对其它数据类型的队列与调度行为。

### 3.3 行情热路径的订阅真值（#366）

PM/OE/SE DataClient 在 market 自定义订阅的 `_subscribe` / `_unsubscribe` 生命周期内维护
`binary_market_id → instrument members` 的 adapter 本地索引。普通行情组帧与断线 CLEAR
只对该索引做 O(1) 查询；不得在每条 runner/delta 上调用 NT
`subscribed_custom_data()`。后者面向控制面返回排序后的完整订阅列表，在高频路径重复调用会把
订阅规模引入每条行情的时间复杂度，并阻塞与 DataEngine 共用的 event loop。

该索引只是 NT 自定义订阅集合的等价运行时投影，不改变订阅引用计数、MessageBus topic、
DataEngine managed book 或源帧协议。首订成功后写入，最终退订时删除；成员判断同时校验
`binary_market_id` 与 `instrument_id`，防止同一 source market 的其它二元 selection 被误发。

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
- PM：同 condition 仍是一组；断线回调只清对应 WS 分片且主动 shutdown 不误清，
  snapshot/retry/reset 行为不退化。
- single-flight：同 source 在途时只保留最新完整状态；completion 后立即 flush；CLEAR barrier
  不被恢复快照覆盖；退订后的迟到 completion 无副作用；拒绝帧 fail-closed 到重新激活。
- hot path：PM/OE/SE 普通组帧与断线 CLEAR 不扫描/排序完整 custom subscription 集合；
  subscribe/unsubscribe 后本地 market members 与实际 market 订阅同生命周期。

对应离线测试见 DataEngine unit tests、各 adapter README，以及 Matching/Strategy README
中的 #357/#358 用例；真钱 live 验证不在本次范围。
