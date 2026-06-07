# discovery 测试

覆盖**市场发现**capability,涵盖:
- Step 1 InstrumentProvider(PM 用上游 + OE 自写)—— `refactor.md §5.1`
- Step 2 InstrumentRefresher Actor(调度 + 持久化)—— `refactor.md §5.2.2`
- 锁定决定: Q1 (InstrumentId 命名) / Q3 (refresh_interval mutable) / Q4 (单 venue 失败) / Q6 (NT 持久化) / Q8 (调度归 Refresher) / Q9 (异构 instrument 归一)

> **#59(slice A)架构反转**:`InstrumentRefresher` Actor **已退役** —— 周期发现迁回 **DataClient 原生 `_update_instruments`**(PM 上游已自带,arb factory 补 `load_all=True`;OE `adapters/orbitexch/data.py` 新增 `_send_all_instruments_to_data_engine` + `_update_instruments` task)。Q8"调度归 Refresher"被验证为重造 NT 原生而反转(refactor.md §5.2.3/#59)。下方 `test_instrument_refresher.py` / `test_instruments_refreshed_event.py` 现测 **dead code**(refresher.py/events.py 暂留,smoke 验后删);新增「DataClient 周期发现 + on_instrument 灌 cache」用例**待补**(#59 已 live smoke10 验:PM `initialize` Loaded 114 + matching timer 出 MatchedPair,refresher 未参与)。

**落地状态(2026-05-23)**:`src/arbitrage/discovery/{events,oe_provider,refresher}.py` 已落,**20 passed, 3 PM skipped**:
- ✅ `test_instruments_refreshed_event.py`(3.1/3.2/3.3:Data 子类、字段时间戳、roundtrip)
- ✅ `test_orbitexch_provider.py`(1.4.a-f:三方向/两方向腿构造、info 6-key、InstrumentId 含 market+selection、load_all_async 接 mock scraper、空返回不抛)
- ✅ `test_instrument_refresher.py`(2.1-2.11:on_start 调度、on_stop 取消、on_save/on_load 持久化 + 损坏值/<min 夹下界、msgbus 命令运行时改值、tick 成功 publish / 0 不 publish / 异常静默不卡死)
- ⬜ `test_polymarket_provider.py`(1.1/1.2/1.3 上游构造需链上 creds,/live-test 验)
- ⬜ 浏览器抓取失败处理(1.7)、双 venue Refresher 隔离(2.7 整端到端,要起 node)、`InstrumentsRefreshed` msgbus 全链路(3.2)—— 经 /live-test 或上层 e2e 验

**仍待 Step 1**:scraper DOM 抽 `start_ts`(现 Provider 暂置 0);PM info 6-key 真 extraction(`#35` 已落 seam:`adapters/polymarket/arb_provider.py:enrich_pm_six_key_info`,**TODO** 实写需 gamma `/events/{id}` 调用 + ticker 拆解;参旧 `odds_client.py:255+`)。

**#35(2026-05-24)Step 2 + PM enricher**:OE DataClient 整体重写 + PM ArbProvider seam(详见 data architecture.md §3)。

**#68(2026-06-07)OE DataClient competition 页订阅模型**:OE OBD 订阅从单 `inplay/highlights` 页改为每 competition 一页;同一 `MatchedPair` 会同时订阅 home/away 两腿,因此 `_ensure_competition_page` 用 `_comp_pages_lock` 防止并发双开同一 competition 页。`tests/arbitrage/adapters/orbitexch/test_data_client_step2.py` 覆盖顺序订阅去重 + 并发订阅去重,并断言 routed price frame / `OrderBookDeltas` publish 计数。live smoke 复验:同一 `2_12803182` competition 只 open 一次,两腿均 subscribed,且出现 `OE price frame routed` + `OE OrderBookDeltas published`;PM proxy 透传修复后已验证双边 OBD 触发 StrategyEvaluator 重评。

**2026-06-07 PM WS base URL 修复**:上游 `PolymarketWebSocketClient` 要求 `base_url_ws=.../ws/`,内部自行拼 `market` / `user`;旧配置沿用 `.../ws/market` 会导致 DataClient / ExecClient 目标变成 `.../ws/marketmarket` / `.../ws/marketuser`。dispatcher 已兼容旧 full endpoint 并统一归一化为 base URL;对应配置测试见 `tests/arbitrage/config/test_dispatcher.py`。

## 文件分布

| 文件 | 范围 |
|---|---|
| `test_polymarket_provider.py` | PM Provider 行为(用上游版本,验证可满足需求) |
| `test_orbitexch_provider.py` | OE Provider 行为(自写,含 Q9 info dict 字段) |
| `test_instrument_refresher.py` | InstrumentRefresher Actor 调度/持久化/事件契约 |
| `test_instruments_refreshed_event.py` | `InstrumentsRefreshed` Data 类型与 MessageBus 契约 |

---

## 用例

### discovery-1.1: PM Provider 冷启动加载

**前置**: NT TradingNode 启动,`PolymarketInstrumentProviderConfig` 配置好 PM API key/sport 过滤

**输入**: 调 `provider.load_all_async()`(模拟一次启动加载)

**步骤**:
1. Provider 调 PM Gamma API + CLOB API 拉取活跃市场
2. 把每个 outcome 转成 `BinaryOption` 实例
3. 调 `provider.add(instrument)` 入 Cache

**期望**:
- `cache.instruments(venue=POLYMARKET)` 返回非空列表
- 每个 instrument 是 `BinaryOption` 实例(不是 BettingInstrument)
- InstrumentId 格式严格匹配 `{condition_id}-{token_id}.POLYMARKET`(用 `get_polymarket_instrument_id` helper 验证可逆)

**验收标准**: instrument 列表非空 AND 任取一个 instrument 调 `get_polymarket_condition_id(inst.id)` / `get_polymarket_token_id(inst.id)` 能正确反解

---

### discovery-1.2: PM Provider 字段完整度

**前置**: discovery-1.1 通过

**输入**: 取一个 `BinaryOption` instrument

**期望**: 以下字段非空且符合预期类型
- `price_increment` (Price)
- `size_increment` (Quantity)
- `min_quantity` / `max_quantity`
- `taker_fee` / `maker_fee`(从 Gamma API fee schedule 富化得到)
- `info` dict(用于 §6.4 异构归一,见 Q9 章节)

**验收标准**: 所有字段非空 AND fee_schedule 富化生效(taker_fee 不为 0)

---

### discovery-1.3: PM Provider API 失败处理

**前置**: 模拟 PM Gamma API 返回 5xx 或超时

**输入**: 调 `load_all_async()`

**期望**:
- 不抛异常(失败被 catch)
- 已加载的 instruments 不被清空
- 日志记录失败
- `cache.instruments(venue=POLYMARKET)` 返回上次成功的快照

**验收标准**: API 异常不污染 Cache 状态

---

### discovery-7B.1(slice 7B,#53):PM `enrich_pm_six_key_info` 真写

**前置**: `ArbPolymarketInstrumentProvider._parse_instrument` 截 BinaryOption 创建后,调 `enrich_pm_six_key_info(market_info, outcome)` 补 info 6-key

**输入**: PM gamma `market_info`(含嵌 events[0].ticker / slug / outcomes / startDate / category)

**期望**(详 `tests/arbitrage/adapters/polymarket/test_arb_provider_enricher.py` 14 tests):
- ticker `{comp}-{home}-{away}-YYYY-MM-DD` 解析:competition 大写,home/away 由 outcomes index 或 abbr 决定
- selection_role 由 market_slug 推 / sub-markets(first-set-winner / match-total / set-totals)返空 → matching 跳
- 非 match-level events(`2026-mens-french-open-winner`)→ 返空,不阻塞 matching
- sport 推断走 ticker 前缀 map(`atp→Tennis`),unmapped 用 `market_info["category"]` 兜底
- 容错:events 缺 / outcomes 是 JSON 字符串 / outcome 不在 outcomes 列表

**验收**: 14 测试通过 + live smoke 用 `series_slug=atp` filter 35s load 100 events → 2026 instruments

### discovery-7B.2(slice 7B,#53):PM `event_slug_builder` 路径

**前置**: `PolymarketInstrumentProviderConfig.event_slug_builder = "nautilus_trader.adapters.polymarket.arb_provider:build_pm_event_slugs_from_arb_context"`;`ArbContext.pm_event_slug_tags = ["atp"]`(launcher 经 dispatcher 从 `cfg.discovery.polymarket.sports[].competitions` 派生)

**输入**: 启动 PM Provider,upstream `_load_from_event_slugs` 触发 callable

**期望**:
- `build_pm_event_slugs_from_arb_context` sync 调 gamma `/events?series_slug=atp&closed=false&limit=500` 返 100 events 的 slug 列表
- 比起 `_load_all_using_gamma_markets` 路径(全量 crawl 50K+ markets / 5+ 分钟):event_slug_builder 路径 **35s 完成 100 events 加载** → 产 2026 instruments
- 关键 audit:**`tag_slug=atp` 在 gamma `/events` 错配**(返 outright winners);**`series_slug=atp`** 才对(返 match-level)

**验收**: live smoke 实测 PM 35s 完成 + InstrumentRefresher tick fire("refresh ok count=2026")

### discovery-10d.E(slice 10d,#52):InstrumentRefresher clean shutdown

**前置**: launcher 启 + InstrumentRefresher 注册 + tick 已 fire 至少一轮(`_tick_task` 已存)

**输入**: `node.dispose()` 触发 actor on_stop

**期望**:
- `on_stop` cancel 未完成的 `_tick_task`(避免 "Task was destroyed but it is pending" warning)
- `_cancel_alert` 也照常执行

**验收**: slice 10d live smoke 实测 0 pending task warning(对比 #51 1 条),log tail 干净 dispose

---

### discovery-2.A.1(slice 8A,#47):Provider 共享机制(回写 ArbContext + Refresher 读)

**前置**: `node.build()` 已运行,data factory 在 `create` 内回写 `ArbContext.{pm,oe}_instrument_provider = provider`

**输入**: launcher `add_actors(node, cfg, pair_registry=...)` 在 build 后调用

**期望**:
- PM `ArbPolymarketLiveDataClientFactory.create` 完成后,`ctx.pm_instrument_provider is provider`(same instance)
- OE `OrbitExchLiveDataClientFactory.create` 完成后,`ctx.oe_instrument_provider is provider`
- launcher 构造 `InstrumentRefresher(deps=RefresherDeps(provider=ctx.{pm,oe}_instrument_provider, loop=asyncio.get_event_loop()))`,**与 DataClient 用同一 provider 实例**(cache add 视图一致)

**provider 缺失场景**: `ctx.pm_instrument_provider is None`(PM data factory 未跑 / discovery 禁用) → launcher 跳过该 venue 的 Refresher 装载,不 raise

**测试**: `tests/arbitrage/launchers/test_arb_node.py` 6 新增(4 actors when both providers / skip PM when missing / skip both / Strategy gets portfolio from kernel / refresher uses ctx provider / bootstrap calls add_actors)

---

### discovery-1.4.g/h(slice 7A,#46):OE Provider aliases 注入

**前置**: `OrbitExchInstrumentProvider(scraper, sport_aliases={...}, competition_aliases={...})` 构造

**输入**: scraper 返一个 MatchEvent(`sport="soccer"`, `competition="Men's Roland Garros 2026"`)

**期望**:
- `info["sport"]` = `sport_aliases.get("soccer", "soccer")` (alias 命中则规范名,否则原值透传)
- `info["competition"]` = `competition_aliases.get("Men's Roland Garros 2026", ...)`
- 无 aliases 参 / 默认 `None` → 原值透传(向后兼容)

**测试**: `test_orbitexch_provider.py` 4 新增(sport alias 命中 / competition alias 命中 / miss 透传 / 无参默认)

---

### discovery-1.4: OE Provider 冷启动加载

**前置**: NT TradingNode 启动 + `PlaywrightBrowserManager` 启动 + 登录态 cookie 已持久化

**输入**: 调 `provider.load_all_async()`

**步骤**:
1. Provider 通过 `browser_manager.create_page("discovery")` 拿专属 page
2. 通过 Playwright 抓取 OE 赛事列表页
3. 解析后,每个 selection 转成 `BettingInstrument`
4. 调 `provider.add(instrument)` 入 Cache

**期望**:
- `cache.instruments(venue=ORBITEXCH)` 返回非空 `BettingInstrument` 列表
- InstrumentId 格式严格匹配 `{market_id}-{selection_id}.ORBITEXCH`
- 配套 helper `get_orbitexch_market_id(inst.id)` / `get_orbitexch_selection_id(inst.id)` 可逆解析

**验收标准**: instrument 列表非空 AND helper 可逆 AND page name 是 `"discovery"`(不是 `"data"` / `"execution"`)

---

### discovery-1.5: OE Provider info dict 必含 6 个统一 key (Q9)

**前置**: discovery-1.4 通过

**输入**: 取一个 OE `BettingInstrument`

**期望**: `instrument.info` dict 必须包含以下 6 个 key(全部非空):
- `info["sport"]` (str) —— 运动类型
- `info["competition"]` (str) —— 联赛名
- `info["home_team"]` (str)
- `info["away_team"]` (str)
- `info["start_ts"]` (int, ns timestamp)
- `info["selection_role"]` (str: "home" / "draw" / "away")

**验收标准**: 6 个 key 全部存在且类型正确(`MarketMatchingActor` 跨 venue 归一依赖此)

**对应章节**: `refactor.md §6.4`

---

### discovery-1.6: OE Provider 与 PM Provider 共享匹配字段

**前置**: discovery-1.1 / 1.4 都通过

**输入**: 任取 PM `BinaryOption` 与 OE `BettingInstrument`

**期望**: 两者的 `info` dict 都含 `sport` / `competition` / `home_team` / `away_team` / `start_ts` / `selection_role` 6 个 key,语义一致(MatchEngine 不需要 isinstance 区分类型即可读)

**验收标准**: 取 6 个 key 不抛 KeyError;PM `selection_role` ∈ {"YES", "NO"} 或 {"home"/"away"}(取决于 PM 市场表达,需 Step 1 实施时确认);OE `selection_role` ∈ {"home", "draw", "away"}

---

### discovery-1.7: OE Provider 浏览器抓取失败处理

**前置**: 模拟 Playwright page 加载超时 / 元素找不到

**输入**: 调 `load_all_async()`

**期望**:
- 不抛异常
- Cache 不被清空
- 日志记录失败
- 不调 `manager.close()`(失败时不能关 browser,Provider 不拥有 manager 生命周期)

**验收标准**: 失败不污染 Cache 也不破坏 BrowserManager 状态

---

### discovery-2.1: Refresher 周期触发

**前置**: `InstrumentRefresher` Actor 启动,`refresh_interval = 5s`(测试用短间隔)

**输入**: 启动 + 等待 12 秒

**步骤**:
1. Refresher `on_start` 启动 `_run` 后台任务
2. `_run` 进入循环,每次 `await asyncio.sleep(self._refresh_interval)`
3. 每次唤醒后调 `provider.load_all_async()`

**期望**: 12 秒内 `provider.load_all_async` 被调用 2 次(t=5s, t=10s)

**验收标准**: mock provider 的调用次数 == 2(允许 ±1 容忍调度 jitter)

---

### discovery-2.2: refresh_interval 通过 MessageBus 命令运行时可变 (Q3)

**前置**: Refresher 启动,初始 `refresh_interval = 30s`

**输入**: 通过 MessageBus publish 命令 `config.{venue}.refresh_interval = 5`,等 7 秒

**期望**: 改后下一次循环用新 interval(7 秒内 `load_all_async` 至少触发 1 次,因为新 interval 是 5s)

**验收标准**: 改值生效不需要重启

---

### discovery-2.3: refresh_interval 通过 NT on_save/on_load 持久化 (Q6)

**前置**: Refresher 启动,通过 MessageBus 改 `refresh_interval = 60`

**输入**:
1. 触发 NT TradingNode 保存(模拟 stop)→ 验 `_shared_redis['actors:{actor_id}:state']` 含 `refresh_interval=60`
2. 重启 Refresher(传同 actor_id)→ NT 调 `on_load(state)` → Refresher `_refresh_interval = 60`

**期望**: 重启后 `refresh_interval` 自动恢复为 60(不是 default 30)

**验收标准**: NT 持久化机制 + on_save / on_load 正常工作 AND `RuntimeConfig` 默认值在持久化值存在时不覆盖

---

### discovery-2.4: 成功 refresh 后发布 InstrumentsRefreshed 事件 (Q4)

**前置**: Refresher 启动,订阅者(测试 Actor)订阅 `DataType(InstrumentsRefreshed)`

**输入**: 等 Refresher 跑完一次成功 refresh

**期望**: 订阅者收到一条 `InstrumentsRefreshed`,字段:
- `venue` == 该 Refresher 的 venue
- `count` == provider.list_all() 返回的 instrument 数量
- `ts_init` == 该 refresh 完成时刻

**验收标准**: 事件字段完整,venue 正确

---

### discovery-2.5: refresh 失败时不发事件 (Q4)

**前置**: Refresher 启动,模拟 `provider.load_all_async()` 抛异常

**输入**: 等一次 refresh 周期

**期望**:
- 异常被 Refresher 捕获
- 不 publish `InstrumentsRefreshed`
- 日志记录失败
- 下一周期照常尝试

**验收标准**: 失败 venue 静默,matching 自然 gate 住(由 MatchingActor 的 fresh_window 保证)

---

### discovery-2.6: MIN_INTERVAL 强制下限 (Q3)

**前置**: Refresher 启动

**输入**: 通过 MessageBus 命令 `config.{venue}.refresh_interval = 0`(或负数)

**期望**: Refresher 内部 clamp 到 `MIN_INTERVAL`(避免误设把场馆刷挂),记日志警告

**验收标准**: `self._refresh_interval >= MIN_INTERVAL` 始终成立

---

### discovery-2.7: 双 venue Refresher 隔离

**前置**: PM Refresher + OE Refresher 同时跑

**输入**: 模拟 PM Provider 抛异常,OE Provider 正常

**期望**:
- OE Refresher 继续正常发 `InstrumentsRefreshed(venue=ORBITEXCH)`
- PM Refresher 静默,不发事件
- 两者互不影响

**验收标准**: 单 venue 失败不波及另一 venue

---

### discovery-3.1: InstrumentsRefreshed Data 类型注册

**前置**: NT TradingNode 启动

**输入**: import `InstrumentsRefreshed` Data 类

**期望**:
- 通过 `@customdataclass` 装饰器注册
- 含字段 `venue: Venue`, `count: int`, `ts_event: int`, `ts_init: int`
- 可通过 `DataType(InstrumentsRefreshed)` 用于 `subscribe_data`

**验收标准**: 类型可注册可订阅可发布

---

### discovery-3.2: InstrumentsRefreshed MessageBus topic 契约

**前置**: discovery-3.1 通过

**输入**: 一个 Refresher publish `InstrumentsRefreshed(venue=POLYMARKET, ...)`

**期望**: MessageBus topic 路径符合 NT 标准 data topic 命名(具体路径 Step 2 启动时与 NT 实现对齐确认)

**验收标准**: 订阅 `DataType(InstrumentsRefreshed)` 的 actor 能收到事件
