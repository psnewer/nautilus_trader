# matching 测试

覆盖**市场匹配**capability(Step 3,摘要级别)。详细设计待 Step 3 启动时展开。

对应章节: `refactor.md §5.3, §6.4`

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
