# 横切:Sports Event Anchor / PMSPORTS Discovery 详细设计

> **定位**:详细设计。本文是把比赛事件发现 / 生命周期从可交易 PM instruments 中剥离出来的单一真理源。
> **成熟度**:部分落地(2026-07-02):`.PMSPORTS` synthetic discovery、PairRegistry
> anchor/tradable 分离、MatchedPair 新字段、Strategy OBD/snapshot 隔离、MatchingActor
> PMSPORTS anchor 聚合、ended eviction、launcher/config 独立 data source enablement 已落地并有离线测试;
> target competitions 默认复用 `discovery.polymarket.sports`;`data_sources.sports_status.sports`
> 仅作为可选覆盖。#250/#322:CustomData 状态管线 + per-(game,channel) 订阅 + 归零回收已落地
> (`game_id` 是赛事路由键，`channel` 是变化通知维度)。
> **归属判据**:该机制同时约束 PMSPORTS data source、matching、PairRegistry、Strategy 输入边界与
> launcher/config,没有单一组件能完整拥有不变量,按 P11 放在横切章节。

---

## 1. 目标与非目标

目标是让 `PMSPORTS` 也执行市场发现,产出 `.PMSPORTS` 后缀的**非交易 anchor instruments**,
参与市场匹配,但不进入套利流。

**目标**:

- 保留现有 PM discovery。`POLYMARKET` 仍继续产出可交易 PM `BinaryOption` instruments。
- `PMSPORTS` 新增 discovery:复用公开 Gamma `/sports` + `/events/keyset?series_id=...` 游标分页拉比赛事件。
- `PMSPORTS` 产出 `.PMSPORTS` synthetic instruments,只表达比赛事件/生命周期 anchor,不表达可交易腿。
- Matching 从旧 `PM tradable instruments × enabled tradable venues` 迁到
  `PMSPORTS event anchor × enabled tradable venues`。
- `.PMSPORTS` 可以进 NT Cache 和 Matching,但必须被 Strategy/Risk/Execution 视为 non-tradable。
- PM/OE/SE 都作为 tradable venue 参与同一个 `.PMSPORTS` anchor 下的匹配。

**非目标**:

- 不删除 PM discovery。
- 不让 `PMSPORTS` 生成 `.POLYMARKET` instruments。
- 不让 `PMSPORTS` 参与下单、账户、余额、venue liveness、Risk 或 Execution。
- 不在第一版做第三方 sports data provider 抽象;当前 provider 仍可先叫 Polymarket sports/Gamma。
- 不把 `.PMSPORTS` 假腿塞进 strategy candidate,也不让它订 order book。

---

## 2. 核心不变量

| 不变量 | 说明 |
|---|---|
| `PMSPORTS` 是 data source / event venue | 它不是 trading venue,无 exec client,无 account,无 position |
| `.PMSPORTS` instruments 非交易 | `info["tradable"] == False`,不能进入 Strategy snapshot 的 tradable instruments |
| Matching 可读 `.PMSPORTS` | Matching 用它作为 canonical event anchor,读 matching key + `game_id` |
| PairRegistry 区分 anchor 与 tradable | 下游从 pair 查 instruments 时必须能只取 tradable ids |
| Strategy 只消费 tradable ids | MatchedPair 触发 OBD 订阅与 snapshot 构造时跳过 anchor ids |
| Eviction 仍归 matching | Sports `ended` 事件驱动 matching unregister;PMSPORTS 只是生产 event/update |
| 详细状态与阶段分家 | `SportsGameStateStore` 保存 PMS 完整 payload；`SportsPhaseStore` 是 PMS/OE/SE 聚合后的阶段真理源 |
| `(game_id, channel)` 即订阅键 | 每场每通道独立 DataType/topic；matching 与 strategy 当前均订 `phase`，全部通道订阅汇合归零后回收 Store 条目；OBD book 仍按原订阅生命周期回收 |
| phase 单调推进 | PRE → IN_PLAY → POST；OE/SE false 不得把已确认的 IN_PLAY/POST 回退，POST 只认 PMS ended |

---

## 3. Synthetic PMSPORTS Instrument

第一版建议每场比赛产出**一个 event-level synthetic instrument**:

```text
{game_id}.PMSPORTS
```

`instrument.info` 必须包含:

```python
{
    "sport": "Tennis",
    "competition": "ATP",
    "home_team": "...",
    "away_team": "...",
    "start_ts": 1782532943243581000,
    "game_id": 5843495,
    "source": "polymarket_sports",
    "tradable": False,
    "anchor": True,
}
```

约束:

- 不需要 `selection_role`。这是 event anchor,不是 outcome leg。
- 若现有 `events_from_instruments()` 暂时要求 legs,应改 normalizer 支持 event-only anchor,
  不要制造 home/away 假腿来迁就旧接口。
- Instrument 类型可先用最轻量的 NT instrument 类型承载,但必须保证不会通过最小下单 / 盘口订阅路径。
  若 NT 要求具体 instrument class,应在 data source 内封装构造,并在 matching/strategy 以
  `info["tradable"]` 防误用。

---

## 4. PMSPORTS Data Source

`PMSPORTS` 目标上是一个独立 data-only client:

```text
client_id = "PMSPORTS"
venue     = "PMSPORTS"
exec      = None
account   = None
```

职责:

1. Discovery:
   - 拉公开 Gamma `/sports`。
   - 按配置目标 competitions 过滤。
   - 拉 `/events/keyset?series_id=...&closed=false&active=true&limit=20`，沿 cursor 取全量。
   - 生成 `.PMSPORTS` synthetic event instruments 并送入 DataEngine/Cache。

2. Lifecycle:
   - 连接公开 `wss://sports-api.polymarket.com/ws` firehose。
   - 解析 `SportsGameUpdate`。
   - **#250/#322 已落地**:经 data architecture §3.4.1/§3.4.2 的 StateStore/Processor,
     兴趣门控(该场无任何通道订阅则不存不推)→ 先写最新状态 → 按变化发布到该场
     `phase`/`score` topic；payload 均为完整 `SportsGameUpdate`。

3. 数据准入与发布:
   - data source 的主准入门是订阅本身(#250 兴趣门控);字段级 filter seam 留后续设计。
   - **#252 修订**:matching 的订阅面 = candidate 集合(非 anchor 宇宙)。防赛后配对由概率校验门
     (死盘无健康双边 book,PENDING 永不 PASS)+ 每 tick candidate 差集清理(卡死 PENDING 回收)
     承担;gamma closed=false 延迟的死比赛因配不出 candidate 而零状态。
   - 通过 filter 的有效数据先写 Cache;publish policy 再独立决定发布目标,可以返回空集合形成 cache-only。
   - matching/strategy 当前均订 `phase`，以保证 `ended` 可达；`score` 通道已具备但当前无消费者。
     完整接口与错误边界只在 data architecture §3.4.1/§3.4.2 定义,本文不复述。

4. 多源 phase(#365):
   - PMS 的 `live/ended`、OE/SE market WS 的 `inPlay` 共同写 Cache-backed
     `SportsPhaseStore(game_id)`；完整比分等字段仍只由 PMS 写 `SportsGameStateStore`。
   - OE/SE 的 `game_id` 来自 Matching/Strategy market 订阅 params，不从 venue event id 猜测。
   - `inPlay` 缺失不更新；false 只能建立 PRE，true 推进 IN_PLAY；POST 只由 PMS ended 推进。
   - 所有源都未给出有效 phase 时 Store 无记录，语义为 UNKNOWN；消费者不得用
     `NOT in_game` 将 UNKNOWN 反推为 PRE。显式 PRE 与 IN_PLAY 分别由 `pre_game` / `in_game` 查询。
   - OE/SE phase 变化不建立第二条事件管线；现有 OBD/PMS 唤醒后读取最新 phase。

当前代码 `adapters/polymarket/sports.py` 已经是独立 client 形态。注册生命周期由
`DATA_SOURCE_REGISTRY["sports_status"]` 与 `data_sources.sports_status.enabled` 控制,
不再依赖 `venues.polymarket.enabled`。包路径仍在 `adapters/polymarket/` 下,这是 provider
实现来源命名,不是 trading venue enablement。target competitions 优先读
`data_sources.sports_status.sports`,为空时继承 `discovery.polymarket.sports`;
常规配置可不显式写 `data_sources` 段。

---

## 5. Matching 语义

旧语义:

```text
POLYMARKET tradable event × enabled tradable venue
```

当前/目标语义:

```text
PMSPORTS event anchor × enabled tradable venues
```

匹配流程:

1. 从 Cache 读取 `.PMSPORTS` anchor instruments。
2. 从 Venue Registry 读取 enabled tradable venues:PM/OE/SE。
3. 对每个 tradable venue,从 instruments 反推 venue event。
4. 以 `(sport, competition)` 分组,按队名相似度把 tradable event 匹配到 PMSPORTS anchor event。
5. 每个 `pair_id` 由 anchor event 决定,同一 anchor 下不同 tradable venue 共享同一个 event identity。
   受 #228 影响(已落地 2026-07-15):anchor event 决定的是 **`event_key`**;3-way 由 event
   拆出 3 个 market 级 `pair_id`(matching §4.2.2),同一条 PMSPORTS 锚在每个拆分 pair 的
   `anchor_instrument_ids` 重复登记,`game_id → pair_ids` 一对多。2-way 仍是一 event 一 pair。

`pair_id` 建议:

```text
{competition}|{home_normalized}|{away_normalized}|PMSPORTS:{game_id}
```

这样:

- 同一场比赛 PM/OE/SE 都归到同一个 anchor identity。
- 不依赖 PM tradable instrument 是否启用。
- 同联赛同队名重复 fixture 时,`game_id` 防碰撞。

---

## 6. MatchedPair / PairRegistry 边界

旧 `MatchedPair(pm_instrument_ids, oe_instrument_ids)` 是 PM anchor 时代的展示形态,
Matching 事件层已删除这两个旧投影字段。PMSPORTS anchor 当前只区分 anchor ids、
tradable ids 与 venue 分组。

当前事件形态:

```python
MatchedPair(
    pair_id=...,
    sport=...,
    competition=...,
    anchor_instrument_ids=["5843495.PMSPORTS"],
    tradable_instrument_ids=[...],  # 可交易腿全集,Strategy/Risk/Portfolio 消费
    venue_instrument_ids={
        "POLYMARKET": [...],
        "ORBITEXCH": [...],
        "SHARPEXCH": [...],
    },
    confidence=...,
)
```

旧字段边界:

- `MatchedPair` 事件不再携带 `pm_instrument_ids` / `oe_instrument_ids`。
- `MatchedPair` 不从旧字段或 instrument id 后缀反推 `tradable_instrument_ids` / `venue_instrument_ids`。
- Web `/matched_pairs` 不再输出 PM/OE/external 旧展示字段,只暴露 `venue_instrument_ids` / `tradable_instrument_ids` / `anchor_instrument_ids` / `venue_teams`。
- consumer 必须使用 `tradable_instrument_ids` / `venue_instrument_ids` 或 PairRegistry tradable API。

PairRegistry 需要扩展:

```python
register(
    pair_id,
    tradable_instrument_ids=[...],
    anchor_instrument_ids=[...],
)
instrument_ids_for_pair(pair_id, tradable_only=True)
anchor_ids_for_pair(pair_id)
```

约束:

- Risk/Portfolio/Strategy snapshot 默认只取 `tradable_only=True`。
- Matching eviction unregister 整个 pair 时同时清 anchor/tradable 映射。
- 通过 anchor id 反查 pair 允许 matching/lifecycle 使用,但 submit/risk 不应使用 anchor id。

---

## 7. Strategy / Risk / Execution 隔离

`PMSPORTS` anchor 不能进入套利流:

- `StrategyEvaluator._ensure_obd_subscribed` 只订阅 tradable ids。
- `build_snapshot` 只收集 tradable ids。
- #250(已落地):Strategy 在 MatchedPair 到达时按场 `subscribe_data`;收到事件后按
  **`game_id`** 查当前注册的**全部** pair(PM-anchor 路径 pair 同样覆盖),逐 pair 调度评估;
  ended 分发完毕后释放本场 sports + 各腿 OBD 订阅。事件只唤醒,
  `OpportunitySnapshot.sports_state` 从 data 层 Store 冻结,不直接信事件 payload。
- Check/Action 的 `ctx.scratch["legs"]` 只能包含 tradable venue。
- `PlaceBetsAction` 若收到 `tradable=False` leg,必须拒绝/跳过并记录 error。
- Risk required venues 从 `expected_legs` 推导时不会看到 `PMSPORTS`。
- `VenueExecutionLiveness` 初始化不包含 `PMSPORTS`。
- Web 可以展示 anchor event,但账户/余额/下单按钮不能把 PMSPORTS 当 venue。

---

## 8. 配置与 Registry

`data_sources.sports_status` 是可选覆盖段,不要塞进 `venues.polymarket`。常规配置可以不写,
此时 PMSPORTS discovery / sports firehose 目标复用 `discovery.polymarket.sports`:

```json
{
  "data_sources": {
    "sports_status": {
      "enabled": true,
      "provider": "polymarket_sports",
      "ws_url": "wss://sports-api.polymarket.com/ws",
      "sports": [
        {"sport": "Tennis", "competitions": ["atp"]}
      ]
    }
  }
}
```

第一版也可先保留现有 `PMSPORTS` client id,但 launcher 注册条件应从
`venues.polymarket.enabled` 改为 `data_sources.sports_status.enabled`。

Venue Registry 仍只描述 trading venues。若需要 registry,新增 `DATA_SOURCE_REGISTRY`,不要把
`PMSPORTS` 混进 `VENUE_REGISTRY` 的 tradable venue 集合。

---

## 9. 落地状态

| 项 | 状态 |
|---|---|
| 增加 `.PMSPORTS` synthetic instrument discovery,锁住字段和 non-tradable 标记 | 已落地 |
| PairRegistry 增 anchor/tradable 区分 API,默认 consumer API 返回 tradable ids | 已落地 |
| `MatchedPair` 增 anchor/tradable/venue 字段,并删除旧 PM/OE 投影字段 | 已落地 |
| StrategyEvaluator OBD 订阅与 snapshot 改只读 tradable ids | 已落地 |
| MatchingActor 增 `anchor_venue="PMSPORTS"` 路径,用 PMSPORTS anchors 匹配 enabled tradable venues | 已落地 |
| Eviction 从 `game_id -> pair_id` 继续工作,game_id 来源改为 anchor instrument | 已落地 |
| launcher/config 将 PMSPORTS 注册从 PM descriptor 下移到 data source enablement | 已落地 |
| SportsGameUpdate 最新状态 Store + per-(game,channel) CustomData 订阅 + 归零回收 | 已落地(#250/#322) |
| Strategy 按 game_id 触发同场全部 pair，并在评估时查询 Store 当前状态；ended 释放全部订阅 | 已落地(#250/#266/#322) |
| PM/OE、PM/SE、OE/SE、PM/OE/SE skip smoke | 待第二阶段 smoke;OE/SE-only 离线注册/dispatcher 已可表达 |

---

## 10. 验收

离线验收:

- PMSPORTS discovery 产出 `.PMSPORTS` instrument,`info["tradable"] is False`。
- PMSPORTS anchor 不触发 OBD subscribe。
- PairRegistry `instrument_ids_for_pair(pair_id)` 默认不返回 `.PMSPORTS`。
- Matching 可用 `PMSPORTS + enabled tradable venues` 生成 pair;PMSPORTS provider
  已独立于 `POLYMARKET` trading venue 注册,OE+SE-only 的 launcher/dispatcher 离线路径已可表达。
- Strategy snapshot 不包含 `.PMSPORTS`。
- Risk required venues 不包含 `PMSPORTS`。
- Sports `ended` 能 unregister 以 PMSPORTS anchor 建出的 pair。
- #250:未订阅比赛不存不推;发布的更新在 consumer handler 运行前已可从 Store 读取。
- #250:同场 3-way 多个 pair 时,一次 sports update 触发全部注册 pair。
- #250:双侧退订汇合归零 → Store 条目回收 + OBD book 回收;evict 后不重订。

Smoke 验收:

- `PMSPORTS + PM + OE`、`PMSPORTS + PM + SE` 能启动并产出 MatchedPair。
- `PMSPORTS + OE + SE` launcher/dispatcher 离线路径已可表达;真实 smoke 待后续执行。
- `skip_execution=true` 下只对 tradable venues 生成 submit intent。
