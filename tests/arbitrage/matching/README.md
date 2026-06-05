# matching 测试

覆盖**市场匹配**capability(Step 3)。

对应章节: `refactor.md §5.3, §6.4`;详细设计 `architectures/matching/architecture.md`;**#34** 修正 pair_id 来源

**落地状态(2026-05-24)**:`src/arbitrage/matching/{events,normalizer,engine,actor}.py` + `src/arbitrage/common/pair_registry.py` 全落,**26 passed**:
- ✅ `test_pair_registry.py`(6:register/get/unregister/同 pair 覆盖/隔离)
- ✅ `test_matched_pair_event.py`(3:Data 子类、字段、roundtrip)
- ✅ `test_normalizer.py`(5:`normalize_team_name` + `events_from_instruments` 反推/分组/info 缺失跳过/group_key)
- ✅ `test_engine.py`(7:同组队名匹/跨 competition 隔/相似度近似匹/贪心/`competition_max_matches`/`min_similarity` 过滤/空输入)
- ✅ `test_actor.py`(6:timer 驱动 + cache-非空 latch —— 单边 cache 空不配 / 双边都有→匹配+register+publish / `_on_alert` 触发匹配+重排 / 不同 competition 不配 / **#60 `test_sports_ended_evicts_pair`**(`SportsGameUpdate.ended` 经 gameId 查 pair → unregister + 不再 re-match)/ **#60 `test_sports_update_non_ended_ignored`**(live 不触发))
  > **#59→#60 演进**:旧 `on_data(InstrumentsRefreshed)`+2×window gate(#52)退役 → matching 自 clock timer 读 cache(#59,refresher 退役);eviction 从 #59 的 expiration 扫描换成 **#60 sports `ended` 事件驱动**(用户判 gamma expiration 不准)。`PairRegistry` key 归一 str(#58)。
- ⬜ 全链路 wiring(DataClient 原生发现 → cache → matching timer → MatchedPair)经 /live-test 验:**#59 smoke10 已验**(PM Loaded 114 + MatchedPair mensik-zverev,refresher 未参与)

**抓出的 bug(已修)**:
1. `_both_recent` gate 用 `.get(v, 0)` 在 TestClock t=0 时假阳性放行 → 改为"两 venue 都必须有过 refresh 事件"。
2. NT Actor 属性是 `self.cache` / `self.clock`(public readonly cdef),不是 `self._cache` / `self._clock`。

## 锁定决定

- **Q9**: PM `BinaryOption` + OE `BettingInstrument` 异构,通过 `instrument.info` dict 6 个统一 key 归一(`sport` / `competition` / `home_team` / `away_team` / `start_ts` / `selection_role`)
- **触发**: 订阅 `InstrumentsRefreshed`,两家 venue 都有近期成功 refresh 才触发匹配(Q4)
- **近期窗口**: `2 × refresh_interval`(Q5)
- **算法**: `EventNormalizer` + `MatchEngine` 从 `services/market_matching/` 平移,**代码不动**

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_event_normalizer.py` | 名称归一化算法(平移自原 service,接口不变) |
| `test_match_engine.py` | 匹配算法(平移自原 service,但输入改为 NT instrument 列表) |
| `test_market_matching_actor.py` | Actor 触发逻辑(订阅 + gating + 防抖) |

## Slice 10d 修(#52):msgbus 直订替代 subscribe_data

`MarketMatchingActor.on_start` 改用 `self._msgbus.subscribe(topic=f"data.{InstrumentsRefreshed.__name__}", handler=self.on_data)` 替代 `subscribe_data(DataType(InstrumentsRefreshed))`。**原因**:NT `subscribe_data` 强制走 SubscribeData cmd 经 DataEngine 路由,需 `client_id` 或 `instrument_id`(slice 10c live smoke 见 3 个 ERROR);custom Actor-to-Actor 事件无 venue/instrument 归属,正确路径是 msgbus 直订(`publish_data` 内部就是 msgbus.publish 到 `data.<TypeName>` topic)。**slice 10d live smoke 验:0 ERROR + OE refresh 3 次正常推进**。
| `test_heterogeneous_normalization.py` | 跨 venue 异构 instrument 归一(Q9 验收) |

---

## 用例(摘要)

### matching-1.x: EventNormalizer 单 venue 归一化

平移自 `tests/arbitrage/services/market_matching/` 中的现有用例(如有)。算法不变,只是引用路径改成 `src/arbitrage/matching/normalizer.py`。

### matching-2.x: MatchEngine 跨 venue 配对

输入: PM `BinaryOption` 列表 + OE `BettingInstrument` 列表
期望: 匹配引擎输出 `MatchedPair` 列表,每对的 `info` dict 字段语义对齐
验收: 两个不同类型 instrument 通过 `info["home_team"]` 等 key 完成匹配,引擎不 isinstance

### matching-3.1: MarketMatchingActor 在两家都有近期 refresh 时触发匹配

前置: Actor 启动,订阅 `DataType(InstrumentsRefreshed)`
输入: 先收到 PM 的 `InstrumentsRefreshed`,再收到 OE 的 `InstrumentsRefreshed`(都在 `fresh_window` 内)
期望: 第二条事件后立即触发 `_do_match`,publish `MatchedPair` 数据
验收: matched 数量 > 0(测试用 fixture 提供可匹配的 instrument 数据)

### matching-3.2: MarketMatchingActor 单 venue 失败时不触发

前置: Actor 启动,只收到 PM 的 `InstrumentsRefreshed`,OE 的从未到达
输入: 等 `fresh_window` 过完
期望: `_do_match` 从未被调用
验收: 没有 `MatchedPair` 被 publish,即使 PM 数据完整

### matching-3.3: 异构 instrument 归一(Q9 关键)

前置: Cache 含 PM `BinaryOption` 列表 + OE `BettingInstrument` 列表
步骤:
1. Actor 触发 `_do_match`
2. 内部调 MatchEngine
3. MatchEngine 读 `instrument.info`,不 isinstance 检查具体类型
期望: 没有 `isinstance(inst, BinaryOption)` / `isinstance(inst, BettingInstrument)` 的代码路径

验收(代码扫描): `MatchEngine` / `EventNormalizer` 中不出现具体类型 import,只 import `Instrument` 抽象类(供类型注解)
