# matching 测试

覆盖**市场匹配**capability(Step 3)。

对应章节: `refactor.md §5.3, §6.4`;详细设计 `architectures/matching/architecture.md`;**#34** 修正 pair_id 来源

**落地状态(2026-07-02)**:`src/arbitrage/matching/{events,normalizer,engine,actor}.py` + `src/arbitrage/common/pair_registry.py` 全落:
- ✅ `test_pair_registry.py`(9:register/get/unregister/同 pair 覆盖/隔离/pair→legs 反查 + #127 anchor ids 分槽登记/覆盖)
- ✅ `test_matched_pair_event.py`(7:Data 子类、anchor/tradable/venue 字段、keyed venue map 主通路、dict roundtrip、旧 pm/oe payload 不回填主字段、旧 payload 即使带后缀也不推断主字段、Arrow map roundtrip)
- ✅ `test_normalizer.py`(7:`normalize_team_name` + `events_from_instruments` 反推/分组/Venue Registry 后缀解析/info 缺失跳过/venue 缺失跳过且不读 `info["venue"]` 兜底/group_key)
- ✅ `test_engine.py`(9:同组队名匹/跨 competition 隔/相似度近似匹/全候选最高 confidence 优先贪心/confidence denominator/`competition_max_matches`/零 confidence 过滤/空输入)
- ✅ `test_actor.py`(12:timer 驱动 + cache-非空 latch —— PM 单边 cache 空不配 / 未显式配置 anchor/tradable venue 时不做 PM/OE 兜底 / 显式 PM tradable anchor 下 PM↔OE 双边都有→匹配+register+publish / `_on_alert` 触发匹配+重排 / 不同 competition 不配 / **SE opt-in 多 tradable venue**:OE 缺失但 SE 存在时 PM↔SE 可匹配、OE+SE 同场在显式 PM tradable anchor 路径产两个不同 pair_id / **#127 `anchor_venue`+`tradable_venues` 配置覆盖显式 PM tradable anchor** / **#127 PMSPORTS non-tradable anchor 聚合 PM+OE 可交易腿到同一 pair** / **#127 PMSPORTS anchor ended eviction** / **#60 `test_sports_ended_evicts_pair`**(`SportsGameUpdate.ended` 经 gameId 查 pair set → unregister + 不再 re-match)/ **#60 `test_sports_update_non_ended_ignored`**(live 不触发))
  > **#59→#60 演进**:旧 `on_data(InstrumentsRefreshed)`+2×window gate(#52)退役 → matching 自 clock timer 读 cache(#59,refresher 退役);eviction 从 #59 的 expiration 扫描换成 **#60 sports `ended` 事件驱动**(用户判 gamma expiration 不准)。`PairRegistry` key 归一 str(#58),#116 增 `instrument_ids_for_pair` 供 Portfolio 读取完整 outcome 集合。
- ⬜ 全链路 wiring(DataClient 原生发现 → cache → matching timer → MatchedPair)经 /live-test 验:**#59 smoke10 已验**(PM Loaded 114 + MatchedPair mensik-zverev,refresher 未参与)

**抓出的 bug(已修)**:
1. `_both_recent` gate 用 `.get(v, 0)` 在 TestClock t=0 时假阳性放行 → 改为"两 venue 都必须有过 refresh 事件"。
2. NT Actor 属性是 `self.cache` / `self.clock`(public readonly cdef),不是 `self._cache` / `self._clock`。

## 锁定决定

- **Q9**: PM `BinaryOption` + OE `BettingInstrument` 异构,通过 `instrument.info` dict 6 个统一 key 归一(`sport` / `competition` / `home_team` / `away_team` / `start_ts` / `selection_role`)
- **venue 解析**:`events_from_instruments` 先经 `venue_id_from_instrument_id()` 解析真实 venue id,再兼容测试 fixture 的 `instrument.id.venue`;不从 `instrument.info["venue"]` 兜底,缺 venue 直接跳过。
- **触发**: #59 后由 NT clock timer 周期读 cache,不再订 `InstrumentsRefreshed`
- **tradable venues**:dispatcher 当前输出 PMSPORTS anchor + `tradable_venues=enabled_tradable_venue_ids(cfg)`;`MarketMatchingConfig` 旧 `pm_venue` / `external_venues` / `oe_venue` 输入字段已删除。
- **MatchedPair 主字段**:`venue_instrument_ids` / `tradable_instrument_ids` 是唯一主通路;事件层不再携带 `pm_instrument_ids` / `oe_instrument_ids`,也不会由旧字段或 instrument id 后缀反推主字段。Strategy 等下游只读 `tradable_instrument_ids`,Web 展示分组只读 `venue_instrument_ids`,不再自行 fallback 到 PM/OE 字段。
- **PMSPORTS event anchor(#127)**:`MarketMatchingConfig.anchor_venue/tradable_venues`、`PairRegistry.anchor_instrument_ids` 分槽、`.PMSPORTS` non-tradable synthetic event instruments、PMSPORTS anchor 聚合 PM/OE 可交易腿、以及 `MatchedPair` 的 `anchor_instrument_ids` / `tradable_instrument_ids` / `venue_instrument_ids` 明确 schema 已落地离线测。后续仍需 live smoke 与 Risk 跳过 anchor 的端到端验证。详细设计见 `docs/arbitrage/architectures/_cross-cutting/sports-event-anchor.md`。
- **近期窗口**: `2 × refresh_interval`(Q5) 已退役;cache 非空 latch 取代
- **算法**:`EventNormalizer` + `MatchEngine` 从 `services/market_matching/` 平移;NT 路径字段命名已泛化为 anchor/tradable(`MatchResult.anchor_event/tradable_event`)。`home_confidence` / `away_confidence` = `get_similar` 命中 token 数 / 两侧较长 token 数,`total_confidence = home_confidence + away_confidence`。组内匹配计算所有 anchor×tradable 候选后按 `(total_confidence,total_matched_chars)` 降序贪心分配。

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_normalizer.py` | 名称归一化算法 + instrument → `NormalizedEvent` 反推 |
| `test_engine.py` | 匹配算法(输入为 `NormalizedEvent` 列表,输出 anchor/tradable `MatchResult`) |
| `test_actor.py` | Actor timer 触发、cache latch、PairRegistry 注册、PMSPORTS anchor 聚合、ended eviction |
| `test_matched_pair_event.py` | `MatchedPair` schema / keyed map / Arrow roundtrip / 旧 PM/OE 字段拒绝回填 |
| `test_pair_registry.py` | pair→legs / anchor 分槽 / refresh interval 热改 |

早期 `test_event_normalizer.py` / `test_match_engine.py` / `test_heterogeneous_normalization.py` skipped 摘要文件已删除;真实验收集中在上表可执行测试。

## 用例(摘要)

### matching-1.x: EventNormalizer 单 venue 归一化

平移自 `tests/arbitrage/services/market_matching/` 中的现有用例(如有)。算法不变,只是引用路径改成 `src/arbitrage/matching/normalizer.py`。

### matching-2.x: MatchEngine 跨 venue 配对

输入: anchor `NormalizedEvent` 列表 + tradable `NormalizedEvent` 列表
期望: 匹配引擎输出 `MatchResult` 列表,主字段为 `anchor_event` / `tradable_event`
验收: `test_engine.py` 覆盖同名匹配 / 跨 competition 不配 / 模糊匹配 / 全候选最高 confidence 优先贪心 / confidence denominator / cap / 零 confidence 过滤 / 空输入 / anchor/tradable 字段。

### matching-3.1: MarketMatchingActor timer 读 cache 触发匹配

前置: Actor 启动,cache 已有 anchor venue 与至少一个 tradable venue 的可归一 instruments。
输入: 触发 `_maybe_match` / actor timer。
期望: Actor 从 cache 读取当前 instrument 快照,经 normalizer + match engine 生成并 publish `MatchedPair`。
验收:`test_timer_tick_matches_when_anchor_and_tradable_cache_present` 等 `test_actor.py` 用例覆盖;不再依赖 `InstrumentsRefreshed`。

### matching-3.2: MarketMatchingActor PM 单边失败时不触发

前置: Actor 启动,cache 只有 PM instruments,没有任何 tradable counterparty venue instruments
输入: 触发 `_maybe_match`
期望: `_do_match` 从未被调用
验收: 没有 `MatchedPair` 被 publish,即使 PM 数据完整

### matching-3.se.1: SE tradable venue 可单独匹配

前置:`MarketMatchingConfig(tradable_venues=("ORBITEXCH","SHARPEXCH"))`,cache 有 PM+SE 同场 instruments,OE 缺失。
输入:触发 `_maybe_match`
期望:发布一个 PM↔SE `MatchedPair`;`pair_id` 追加 `|SHARPEXCH`;`venue_instrument_ids["SHARPEXCH"]` 承载 SE legs,事件层不携带 `oe_instrument_ids`。
验收:`test_sharpexch_tradable_venue_matches_without_orbitexch`。

### matching-3.se.2: 显式 PM tradable anchor 下 OE 与 SE 同场产不同 pair_id

前置:`tradable_venues=("ORBITEXCH","SHARPEXCH")`,cache 有 PM+OE+SE 同场 instruments。
输入:触发 `_maybe_match`
期望:发布 PM↔OE 与 PM↔SE 两个 pair;两个显式 PM-anchor pair 都带 venue 后缀(`|ORBITEXCH` / `|SHARPEXCH`);同一 `game_id` 映射到 pair_id set,ended 时可一起清理。
验收:`test_multiple_tradable_venues_emit_distinct_pairs_for_same_pm_game`。

> 当前默认 dispatcher 已切到 PMSPORTS non-tradable anchor + tradable venues 聚合路径;本用例覆盖显式 `anchor_venue="POLYMARKET"` 的可配置路径,不是旧字段兜底。PMSPORTS anchor 下同一比赛只发布一个聚合 `MatchedPair`,真实 venue 归属看 `venue_instrument_ids`。

### matching-pmsports-anchor.0:anchor/tradable 配置与 registry 分槽(已落地,#127 slice A)

**前置**:`MarketMatchingConfig(anchor_venue="POLYMARKET", tradable_venues=("ORBITEXCH",))`;PairRegistry 可接收 `anchor_instrument_ids`。这是显式 PM tradable anchor 用例,不是当前 dispatcher 默认路径。

**输入**:触发 `MarketMatchingActor._maybe_match()`;另以纯 registry 注册 `anchor_instrument_ids=["777.PMSPORTS"]`。

**期望**:
- 新字段能表达显式 PM tradable anchor + OE tradable:`venue_instrument_ids` 按 venue 分组,`tradable_instrument_ids` 为可交易腿全集,且不改变 `MatchedPair` 旧展示字段输出。
- anchor id 可通过 `get()` 反查 pair,但 `instrument_ids_for_pair()` 默认不返回 anchor id。

**验收**:
- `test_anchor_and_tradable_venue_fields_preserve_current_pm_anchor_behavior`。
- `test_anchor_ids_are_registered_but_not_returned_as_tradable_legs_by_default`。
- `test_register_same_pair_drops_stale_anchor_ids`。

### matching-pmsports-anchor.1:PMSPORTS anchor 可匹配 tradable venues(已落地,#127)

**前置**:Cache 含一条 `{game_id}.PMSPORTS` synthetic event instrument,以及同场 OE/SE 或 PM/OE tradable instruments。

**输入**:触发 `MarketMatchingActor._maybe_match()`。

**期望**:
- Matching 以 `.PMSPORTS` event 为 anchor。
- PM/OE/SE 均作为 tradable venue 匹配到该 anchor。
- Matching 聚合同一 anchor event 下的 tradable venues,发布一个带 anchor/tradable/venue 分组字段的兼容 `MatchedPair`。
- 聚合路径的 `pair_id` 使用基础格式,不追加某个 tradable venue suffix,也不借用 OE 作为哨兵。
- `PairRegistry` 区分 `anchor_instrument_ids` 与默认可交易 `instrument_ids_for_pair()`。

**验收**:
- `test_pmsports_anchor_aggregates_enabled_tradable_venues_into_one_pair`。
- PairRegistry 默认反查给 Strategy/Risk 的 instrument ids 不包含 `.PMSPORTS`。

### matching-pmsports-anchor.2:PMSPORTS 不进入套利订阅/快照(已落地,#127;submit 路径待 smoke)

**前置**:`MatchedPair` 含 `.PMSPORTS` anchor id + 至少两个 tradable venue ids。

**输入**:StrategyEvaluator 收到该 MatchedPair。

**期望**:
- 只对 tradable instrument ids 调 `subscribe_order_book_deltas`。
- `build_snapshot(pair_id)` 只包含 tradable instruments。
- `.PMSPORTS` 不会经订阅/快照进入套利评估输入;最终 submit spec 仍需端到端 smoke 验证。

**验收**:
- `test_matched_pair_obd_subscription_uses_tradable_ids_not_anchor_ids`。
- `test_snapshot_uses_tradable_pair_ids_not_anchor_ids`。

### matching-pmsports-anchor.3:Sports ended 清理 PMSPORTS anchor pair(已落地,#127)

**前置**:Matching 已基于 `{game_id}.PMSPORTS` 注册 pair。

**输入**:收到 `SportsGameUpdate(game_id=..., ended=True)`。

**期望**:
- PairRegistry unregister 该 pair。
- `_ended_games` 记录该 `game_id`,后续不会重新 match。
- 清理同时覆盖 anchor/tradable 映射。

**验收**:`test_pmsports_anchor_ended_evicts_aggregated_pair`。

### matching-3.3: 异构 instrument 归一(Q9 关键)

前置: Cache 含 PM `BinaryOption` 列表 + OE `BettingInstrument` 列表
步骤:
1. Actor 触发 `_do_match`
2. 内部调 MatchEngine
3. MatchEngine 读 `instrument.info`,不 isinstance 检查具体类型
期望: 没有 `isinstance(inst, BinaryOption)` / `isinstance(inst, BettingInstrument)` 的代码路径

验收(代码扫描): `MatchEngine` / `EventNormalizer` 中不出现具体类型 import,只 import `Instrument` 抽象类(供类型注解)

## 控制台命令 consumer(#119)
- `command.arb.refresh_interval`:`MarketMatchingActor.on_start` 内 subscribe → 热改 `_refresh_interval_secs`。用例 `test_pair_registry.py::test_refresh_interval_command_hot_updates` / `test_refresh_interval_command_rejects_nonpositive`。契约见 web §8.3。
