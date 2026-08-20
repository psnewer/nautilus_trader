# Common 详细设计

> **定位**:`src/arbitrage/common/` 存放跨组件共享的轻量领域契约 / 注册表 / 工具。本文只记录有跨组件语义的 common 模块;纯工具函数见 `utils_api.md`。
> `utils.py` 不承载 venue 业务门控:应用层 `MIN_SIZE_*` / `check_min_size` /
> `adjust_share_by_liquidity` 已删除;最小下单额由 instrument 元数据 + NT RiskEngine 检查,
> 深度缩放属于 Strategy/Action 局部逻辑。

## 1. Opportunity Metadata 契约(#106,已落地,2026-06-14)

`src/arbitrage/common/opportunity.py` 是 Strategy / Risk / Execution 共用的 opportunity metadata 单一解析实现。

| API | 用途 |
|---|---|
| `OpportunityMeta` | `opportunity_id / pair_id / leg_key / expected_legs / positions_digest / intent / venue_required_balance / enable_timeout / market`；`positions_digest` 是 Strategy 评估开始时该 pair 的仓位基线（#317:open_orders_digest 已删）；`enable_timeout`、`market` 都是三态字段，缺省不写 tag |
| `new_opportunity_id()` | `PlaceBetsAction` 为一次 action fire 生成机会 ID |
| `tags_from_meta(meta)` | submitter 把 spec metadata 写入 `Order.tags` |
| `meta_from_order(order)` / `meta_from_tags(tags)` | Risk / Execution 从 `Order.tags` 读取 metadata |
| `order_requests_market(order)` | 仅当订单 metadata 显式为 `market=true` 时返回真，供 venue 最终提交边界选择市价语义 |
| `order_intent(order)` | Risk 读取 `arb:intent`,默认 `arbitrage` |
| `RISK_LEG_DENIED_TOPIC` | `risk.opportunity.leg_denied` topic 常量 |
| `CancelOpportunityMeta` | `opportunity_id / pair_id / cancel_key / expected_cancels`;只描述一组应同步进入 ExecutionClient 的标准撤单命令，不携带 submit tracking 策略 |
| `cancel_params_from_meta(meta)` / `cancel_meta_from_command(command)` | Strategy 经 `CancelOrder.params["arb_cancel_opportunity"]` 写入、Execution barrier 读取 grouped cancel metadata |

**约束**:
- 下单 metadata 的权威载体是 `Order.tags`,不是 `SubmitOrder.params`,因为 Risk deny 和 SubmitOrder barrier 都以 `order` 为入口。
- grouped cancel 不伪造 Order/tag,也不经过 Risk；其 metadata 权威载体是进程内
  `CancelOrder.params["arb_cancel_opportunity"]`。未带该字段的普通 `CancelOrder` 保持 NT 原生直通。
- `expected_cancels` 必须是非空、无重复的 `client_order_id` 集合，并包含本命令的
  `cancel_key`；格式无效时 fail-closed 为 `OrderCancelRejected`。
- `expected_legs` 只包含真实下单腿;不发送 0 qty 空单。
- 同一 opportunity 的所有真实腿必须携带相同 `positions_digest`（#317:open_orders_digest 已删）。
- `enable_timeout=true/false` 分别写为对应 submit tag；字段缺失或非法值按“未配置”处理，
  保持旧订单兼容。只有显式 `false` 表示禁用 timeout 等待、ACK 后结束 submit session；
  缺失或 `true` 均继续既有跟踪。普通 CancelOrder/cancel-only 不读取原订单 tag，只有
  grouped cancel 的 command params 显式携带该字段时才应用；Risk 不改变门控。
- `market=true/false` 分别写为对应 submit tag；只有显式 `true` 请求市价提交，缺失、非法值或
  `false` 均按限价提交。该字段只影响 venue adapter 最终服务端提交，不改 NT LimitOrder 的
  计划 price/qty，也不参与 Risk/barrier 判断。
- common 模块只负责解析 / 构造,不维护 opportunity 状态；SubmitOrder 与 CancelOrder 共用的
  grouped-command 状态机归 Execution barrier。

Strategy 记录与 Execution 重算的执行状态基线是 `pair_positions_digest`（详见 §9）。
**#317**:原 `pair_open_orders_digest`（pair-level open-order 摘要）已删除,barrier 不再做 order-digest 校验（承 #316 per-pair ≤1）；集合级底层 `orders_digest` 保留,仅 reconciliation 用。

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
- registry 不在每次 submit 前翻 false。启动/周期 reconciliation 的远端查询正常返回即写 alive、抛异常即写 dead；本地 report 应用成败不参与判定。单笔 in-flight QueryOrder 不写 liveness。OE/SE 常规完整 `CURRENT_BETS` 帧可实时标活。
- key 统一为 venue 大写字符串;可传 `Venue` 对象或字符串。

## 3. PairRegistry(已落地,#34 / #116 / #127 slice A 补充)

`src/arbitrage/common/pair_registry.py` 是 matching 写、risk/portfolio/strategy/session 读的 pair 映射 registry。语义归属在 matching 组件,common 只承载共享实现。

| API | 用途 |
|---|---|
| `register(pair_id, instrument_ids, *, anchor_instrument_ids=(), game_id=None)` | matching 成功后注册该 pair 的可交易 leg instrument id;PMSPORTS event anchor 等 non-tradable id 单独传 `anchor_instrument_ids`;`game_id`(#250)登记 sports 事件路由键 |
| `get(instrument_id)` | 下游从任一可交易 instrument id 或 anchor id 反查 pair_id |
| `instrument_ids_for_pair(pair_id, *, tradable_only=True)` | Portfolio/Strategy/Risk 反查该 pair 的可交易 instrument id;`tradable_only=False` 才包含 anchor id |
| `anchor_ids_for_pair(pair_id)` | matching/lifecycle/test 读取该 pair 的 anchor instrument id |
| `pair_ids_for_game(game_id)` | **#250 已落地**:按 game_id 反查当前注册的全部 pair;供 PMSPORTS sports update 扇出(3-way 同场多 pair;PM-anchor 路径 pair 无 PMSPORTS anchor,按 game_id 才全覆盖)|
| `game_id_for_pair(pair_id)` | **#250**:pair → game_id;strategy 在 MatchedPair 到达时反查,发起该场 per-game sports 订阅 |
| `unregister_pair(pair_id)` | 结束/eviction 时清理该 pair 的映射 |
| `all_pair_ids()` | 低频观测 / 扫描用 |

**约束**:
- register/get 两侧都用 `str(instrument_id)` 归一,避免 matching 写字符串、consumer 传 `InstrumentId` 对象导致 miss。
- `instrument_ids_for_pair` 默认返回**可交易腿**字符串集合;读取 instrument 详情的一方负责转回 `InstrumentId` 并从 NT Cache 取 instrument。
- PMSPORTS synthetic event anchor 等 non-tradable id 只用于 matching/lifecycle 反查,默认不暴露给 Strategy/Risk/Portfolio,避免进入套利快照 / risk gate / execution。
- `get(anchor_id)` 的单值兼容行为不能用于 PMSPORTS strategy event 路由;#250 使用多值
  `pair_ids_for_game`,防止同场的 3-way pair 只触发排序后的第一个。
- MatchingActor 是唯一写者;其它组件只读或按 matching/eviction 归属调用 `unregister_pair`。

### 3.1 PairPriceStore(已落地,#323/#341)

`src/arbitrage/common/pair_prices.py` 在 NT Cache 通用对象区保存 market-level pair 的 PM
参考价格，key 为 `arb:pair_price:{pair_id}`。采集时机和 PM 盘口完整性规则归 Strategy
§3.8.2；common 只定义状态与原子写入接口。

| 字段 / API | 语义 |
|---|---|
| `first_price` | outcome → PM best ask 概率；初始化为空字典，首次完整写入后不可覆盖 |
| `start_price` | outcome → PM best ask 概率；初始化时每个 outcome 为 `0.6`，首次完整写入后不可覆盖 |
| `up_price` | outcome → 满足 commission 区间的历史最高 PM best ask 概率；首个有效向量初始化，后续逐 outcome 取 `max` |
| `down_price` | outcome → 满足 commission 区间的历史最低 PM best ask 概率；首个有效向量初始化，后续逐 outcome 取 `min` |
| `initialize(pair_id, outcomes)` | 幂等初始化；已有记录保持不变，重新收到 MatchedPair 不重置参考价 |
| `capture_first` / `capture_start` | 比较并整组写入；禁止逐 outcome 更新造成混合时点向量 |
| `update_extremes` | 接收完整同刻价格向量并原子更新 `up_price/down_price`；commission 完整性校验由 Strategy 采集方拥有 |
| `delete(pair_id)` | Strategy 在比赛 ended 的最后一轮评估完成后删除记录 |

Store 不保存独立的 captured 标志：`first_price` 是否为空、`start_price` 是否仍全部为默认值、
`up_price/down_price` 是否为空就是各自的写入/可读判据。读取旧 schema 时缺失的极值字段按空字典兼容，
等下一个有效 PM 向量自然初始化。所有 compare-and-write 均在 Strategy Actor 同一同步回调内完成，中间没有 `await`。

## 4. 控制台命令(`control.py`,#119)

`src/arbitrage/common/control.py` = Web 控制台 → 组件的运行时控制命令(topic 常量 + frozen dataclass):
`SetTradingStateCommand` / `SetRiskParamsCommand` / `SetArbitrageParamsCommand` / `SetRefreshIntervalCommand`。**契约真理源在 web §8.3**(WebGatewayActor 单一生产者 publish,risk/strategy/matching 订阅 apply);本目录只承载共享类型定义。

`SetRiskParamsCommand` 使用 `None` 表示不覆盖该字段;当前热字段包括 `match_tp` / `match_sl` / `min_probability` / `max_probability`。`SetArbitrageParamsCommand` 承载普通套利运行默认值 `share` / `max_leg_share` / `fx` / `evaluate_on_depth_change`;最后一项由 StrategyEvaluator 决定纯深度 OBD 是否唤醒评估。这些字段不归 Risk 配置所有。

## 5. Venue 配置 dataclass(`venue_configs.py`)

`src/arbitrage/common/venue_configs.py` 承载 discovery / adapter factory 共享的轻量 venue 配置:
`BrowserConfig`、`SportConfig`、`PolymarketVenueConfig`、`OrbitExchVenueConfig`、`SharpExchVenueConfig`。

SharpExch 第一阶段只新增与 OE 同形的 `SharpExchVenueConfig(enabled, browser, sports)`。真实 SE
浏览器/API/WS 接线仍归 `architectures/sharpexch/architecture.md`;common 只提供跨模块传参类型,不定义
SE discovery 算法。

## 6. ArbContext factory 注入字段

`src/arbitrage/bootstrap.py:ArbContext` 是 NT factory 固定签名之外的进程级依赖注入通道。PM/OE/SE
factory 只读取 venue/data-source keyed map;旧 `ctx.pm_*` / `ctx.oe_*` / `ctx.se_*`
投影字段已删除,避免测试或 runtime 被旧字段兜底掩盖。后续新 adapter/factory
应以 `ctx_map_get` / `ctx_map_require` / `ctx_map_set` + keyed map 为入口。

| 字段 | 用途 |
|---|---|
| `session_timeout_secs_by_venue` | enabled trading venue → execution session timeout;当前 dispatcher 从 `execution.tracking_timeout_sec` 派生 |
| `discovery_config_by_venue` | venue → discovery config;当前包含 enabled 且 discovery enabled 的 OE/SE |
| `sport_aliases_by_venue` / `competition_aliases_by_venue` | enabled trading venue → Provider 写 Q9 字段前的 alias 表 |
| `instrument_provider_by_venue` | venue → Data factory 回写 provider 的通用槽位;PM/OE/SE Data factory 已写入 |
| `browser_manager_by_venue` / `browser_lock_by_venue` | venue → 浏览器共享件的通用槽位;OE/SE factory 读取/写入 browser manager;browser lock 仅 SE exec factory 写(旧 `se_login(..., browser_lock=...)` 兼容入口),discovery 不用锁 |
| `target_competitions_by_data_source` / `competition_to_sport_by_data_source` | data source → PMSPORTS 目标 competition 与 competition→sport map |
| `realized_pnl_ledger` | #282:PM Execution reconcile / settlement 写，Portfolio 读的同一份 `RealizedPnlLedger` |
| `ctx_map_get` / `ctx_map_require` / `ctx_map_set` / `ctx_map_get_or_create` | keyed map 读写 helper;session timeout 等必需项用 `ctx_map_require` fail-fast,provider/browser 等共享件用 `ctx_map_set` 或 `ctx_map_get_or_create` 回写 |

这些字段只承载 factory 注入状态;默认 `venues.sharpexch.enabled=false` 时不改变现有 PM/OE
runtime 流程。

## 7. Venue Registry / Capability(`venues.py`,落地中 Q28)

`src/arbitrage/common/venues.py` 是第二阶段 venue 插拔化的静态 registry 与 capability helper。
横切机制真理源见 `_cross-cutting/venues.md`;本节只记录 common API 落位。

| API | 用途 |
|---|---|
| `VenueDescriptor` | 记录真实 venue id、config key、display group、instrument/odds model、config/discovery builder、factory 和 PM 专属能力 |
| `DataSourceDescriptor` | 记录 data-only source id、client id、provider、config builder 和 factory |
| `VENUE_REGISTRY` | 当前支持的静态可交易 venue 表:`POLYMARKET` / `ORBITEXCH` / `SHARPEXCH`;各 venue 的 discovery config builder 也挂在 descriptor 上,由 dispatcher 遍历派生 keyed map |
| `DATA_SOURCE_REGISTRY` | 当前支持的静态 data source 表:`sports_status` → `PMSPORTS` |
| `resolve_factory(path)` | launcher 按 descriptor 中的 import path 延迟加载 factory class |
| `is_venue_enabled(cfg, venue)` | 通过 descriptor `config_key` 判断 `venues.<key>.enabled`,调用方不直接写 config 字段路径 |
| `enabled_venues(cfg)` / `enabled_venue_ids(cfg)` | 按 `venues.*.enabled` 返回 runtime venue |
| `enabled_tradable_venues(cfg)` / `enabled_tradable_venue_ids(cfg)` | 返回 enabled 可交易 venue;当前等价于 runtime venue,但明确不包含 PMSPORTS 这类 sports client |
| `enabled_settlement_venues(cfg, settlement_kind)` | 返回开启且声明了指定 settlement capability 的 venue;launcher 以此触发 PM CTF settlement,不直接按 `POLYMARKET` 常量判断 |
| `enabled_data_sources(cfg)` / `enabled_data_source_client_ids(cfg)` | 返回 enabled data-only sources;当前为 `PMSPORTS` |
| `enabled_sports_client_ids(cfg)` | 当前等价于 enabled data source client ids;launcher validation 用它判断是否有 sports anchor data source |
| `is_known_venue(venue)` | 只检查 venue 是否存在于静态 registry;供上层过滤未知 venue,不顺便表达 odds/role 语义 |
| `venue_id_from_instrument_id(instrument_id)` | 优先从 NT `InstrumentId.venue.value` 读取真实 venue id;兼容旧测试字符串后缀 |
| `venue_id_from_leg_key(leg_key)` | 从 opportunity `leg_key` 前缀解析真实 venue id;兼容 `pm/oe/se` 旧缩写和完整 config key |
| `is_decimal_odds_venue(venue)` / `is_probability_odds_venue(venue)` | 查询 descriptor odds model,供策略 / 风控 / portfolio 公式分支使用 |
| `venue_preference_rank(venue)` | 同概率/同价时的稳定排序 key:probability venue 先于 decimal venue,再按 registry 顺序 |
| `probability_from_price(venue, price)` | PM probability price / decimal odds venue 的统一概率转换入口 |
| `qty_from_share(venue, share, price)` | PM share qty / decimal odds stake qty 的统一推导入口 |
| `outcome_for_position(venue, outcomes, selection_role, claim, position_side)` | 将 NT 当前净 Position 归属到 pair 的经济 outcome;decimal SHORT 映射到二元 complement,其它无法确认的 side fail-closed |
| `LegEconomics` / `leg_economics(venue, price, size, is_lay=False)` | 统一计算 probability BACK、decimal BACK/LAY 的 `share_if_wins/profit_if_wins/loss_if_loses` |
| `order_liability(venue, quantity, price, is_lay=False)` | 统一返回订单最大本金占用:probability=`qty×price`,decimal BACK=`qty`,decimal LAY=`qty×(price−1)`;Risk 余额门控与 Execution accepted 预扣共用 |
| `normalize_order_price(venue, price, side)` | 将 decimal venue 价格按共用分段赔率梯度量化；BUY/BACK 向上、SELL/LAY 向下；probability venue 原值返回。完整档位契约见 `_cross-cutting/venues.md §4.2` |

**约束**:
- registry 不抹平真实 venue identity;account、instrument、position、liveness 仍按真实 venue 记录。
- 第一版是静态表,不是动态插件系统。
- 当前 matching 默认消费 `PMSPORTS` anchor + enabled tradable venues;PMSPORTS 注册走
  `DATA_SOURCE_REGISTRY`,不是 PM tradable anchor 语义。
- data source enablement 同时要求 `enabled=true` 且用户配置的 `provider` 与 registry provider 匹配;
  provider 写错时不静默回退到 polymarket sports,launcher validation 会按缺 PMSPORTS source 报错。
- descriptor 存 factory import path,不在 common 顶层 import factory class;launcher 通过 `resolve_factory`
  lazy-load,避免 `common.venues → factory → bootstrap/risk → common.venues` 循环。
- strategy 从 instrument id 取 venue 时调用 `venue_id_from_instrument_id`,不维护
  `.POLYMARKET/.ORBITEXCH/.SHARPEXCH` 后缀列表。
- Risk 从 opportunity `expected_legs` 推导 required venues 时调用 `venue_id_from_leg_key`,不维护
  `pm/oe/se` → venue 的私有映射。
- strategy/risk 消费 helper,不再维护 `{ORBITEXCH, SHARPEXCH}` 集合;同概率 tie-break 等稳定性规则也经
  registry helper 表达,不在策略里写具体 venue 名。
- Portfolio 与 recovery 不自行解释 `Position.side`:统一先调用 `outcome_for_position`,再调用
  `leg_economics`。PM NO token 的 LONG 直接归 `no`;decimal LAY 的 SHORT 归二元 complement。
  probability SHORT、未知 side 和非二元 SHORT 返回 `None`,调用方跳过该腿,禁止猜测。

## 8. 已实现盈亏调整账本(`#282`)

`src/arbitrage/common/realized_pnl.py:RealizedPnlLedger` 是 Execution 与 Portfolio 共享的轻量
进程内账本，不替代 NT 的订单/持仓记账：

- 正常 `OrderFilled` 仍由 NT Position/Portfolio 产生 instrument realized PnL。
- PM reconcile 写入 `external_realized - native_realized` 的 instrument 基线差，使重启后
  Data API 历史值与本进程 NT 增量能相加而不重复。
- PM `/positions + /closed-positions` 的 `realizedPnl` 是 per-asset 累计快照，已包含 SELL 与历史
  merge；两端重叠 instrument 以 current 覆盖 closed、只计一次，closed-only instrument 保留。
  merge 成功时不另写 condition 调整、不伪造 Fill。
- Portfolio 经 PairRegistry 聚合 pair 下全部 instrument 的 NT realized 与账本基线差。
- `replace_instrument_snapshot(only_instruments=...)`(#318/#331):支持**per-instrument 选择性更新** —— 只更新
  通过 reconcile 摘要校验的 instrument 的 offset,其余保留(offset 公式 `external-native` 不变)。
  deferred payload 自身的 instrument 也参与 scope 收集，因此 closed-only 腿无需 PositionReport 也能在校验后提交。
  `only_instruments=None` 仍是整账户全量替换(旧语义)。
- ⚠️ **#318**:账户级单调 `revision` 曾参与 execution §4.6 的 position batch 快照(realized_revision),
  该用途**已删** —— reconcile 快照收窄为 per-pair、realized staleness 由 position 摘要的 realized_pnl 覆盖。
  `revision()` 方法保留但当前无消费者。

账本不保存 position/share/liability，也不接管 FillReport。其详细生产语义见 execution §4.6，
消费公式见 risk §4.1。

## 9. Pair execution-state 摘要(`#266/#284`；#317 barrier 侧仅留 position)

`src/arbitrage/common/positions.py::pair_positions_digest` 是 Strategy 和 Execution barrier
共用的纯函数(评估基线 + release 前重算)；底层 `positions_digest(positions)` 可对任意已筛选集合
生成同口径摘要。二者将业务相关字段投影成稳定排序的 JSON 后计算 SHA256，不保存 Position 对象引用。

- position 摘要覆盖 position/account/instrument/strategy 身份、side/quantity、
  `avg_px_open/avg_px_close/realized_pnl`、`event_count/ts_last`。
- position 使用 `cache.positions()` 而非只读 open positions，保证 SELL 全平后的 closed
  position 变化仍参与比较。
- **#317**:`pair_open_orders_digest`(pair-level open-order 摘要)已删除 —— barrier 不再做 order-digest 校验(承 #316 per-pair ≤1,见 synchronization §8.4bis)。底层 `orders_digest(orders)`(集合级)**保留**,仅 execution §4.6 的 reconciliation 乐观并发校验使用。

本节只定义 helper 落点与字段；Strategy → metadata → Risk → Execution 的一致性协议和
fail-closed 时序以 `_cross-cutting/synchronization.md §8.4bis` 为单一真理源。
各 venue reconciliation 使用集合级 helper 的乐观并发校验由 execution §4.6 定义；它与 pair barrier
是两个消费场景，不共享生命周期。
