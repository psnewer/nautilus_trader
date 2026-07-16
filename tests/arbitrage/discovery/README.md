# discovery 测试

覆盖**市场发现**capability,涵盖:
- Step 1 InstrumentProvider(PM 用上游 + OE/SE 自写)—— `refactor.md §5.1` / `architectures/sharpexch/architecture.md`
- Step 2 周期发现已迁回 DataClient 原生 `_update_instruments`—— `refactor.md §5.2.3/#59`
- 锁定决定: Q1 (InstrumentId 命名) / Q3 (refresh_interval mutable) / Q4 (单 venue 失败) / Q6 (NT 持久化) / Q8 (调度归 Refresher) / Q9 (异构 instrument 归一)

> **#59(slice A)架构反转**:`InstrumentRefresher` Actor 与 `InstrumentsRefreshed` 事件已退役删除。周期发现迁回 **DataClient 原生 `_update_instruments`**(PM 上游已自带,arb factory 补 `load_all=True`;OE/SE 自写 DataClient 各自维护 `_send_all_instruments_to_data_engine` + `_update_instruments` task;PMSPORTS data-only client 同形首抓 + 周期重抓)。Matching 不再订 discovery 事件,而是 timer 读 cache。旧 refresher 相关 skipped 测试/README 条目不再作为当前验收。
> **2026-06-29 overnight 修;2026-07-10 SE 对齐**:PM/OE/SE DataClient 的 `_update_instruments` 每轮单独吞普通异常并继续下一轮,避免一次断网 / DNS / Playwright `goto` / SE context CSRF 暂未就绪杀死整个 60min 周期 discovery task。验收落在 PM/OE/SE adapter 测试: `test_update_instruments_continues_after_provider_error` / `test_connect_initial_load_failure_still_starts_periodic_retry`。
> **2026-07-02 venue/data-source keyed context**:PM/OE/SE Data factory 只从 `ArbContext` keyed map 读取 discovery/session/alias 相关配置并回写 `instrument_provider_by_venue`;PMSPORTS factory 只读取 `target_competitions_by_data_source["PMSPORTS"]` / `competition_to_sport_by_data_source["PMSPORTS"]`;专属 `pm_*` / `oe_*` / `se_*` ArbContext 字段已删除。验收落在 PM/OE/SE adapter factory 测试。

**落地状态(2026-07-08)**:发现路径以 adapter DataClient + Provider 测试为准:
- ✅ `test_orbitexch_provider.py`(1.4.a-f:三方向/两方向腿构造、matching info、InstrumentId 含 market+selection、load_all_async 接 mock scraper、空返回不抛)
- ✅ `test_orbitexch_discovery_scraper.py`(1.4.i:discovery 独立浏览器注入 document visibility + IntersectionObserver spoof,避免 OE competition 页懒加载只发现首屏 20 场)
- ✅ SE discovery/provider/data 相关测试在 `tests/arbitrage/adapters/sharpexch/` 下维护,覆盖 `sport/details` 分页、Provider、discovery 不开页不登录而等待共享 BrowserContext `CSRF-TOKEN`、price frame 路由与 DataClient 生命周期。
- ⬜ PM Provider 冷启动 / 字段完整度 / API 失败保 cache 需 Gamma/CLOB live smoke 验;不保留 skipped pytest 空壳。
- ⬜ DataClient 原生发现 → cache → matching timer → MatchedPair 的全节点路径仍归上层 e2e/live smoke 验;不再以 Refresher/msgbus 事件作为验收对象。

**2026-06-14 最小下单元数据修正;2026-06-30 fx 口径校准**:PM 最小值是 share 数量,Provider 产物 `BinaryOption.min_quantity=5`;OE 最小值是 stake 7 GBP,但 adapter 外部 OE quantity 是 USD stake,Provider 产物 `BettingInstrument.min_notional=Money(7 * arbitrage.fx, USD)`。Risk 组件不维护 venue 常量,由 NT 父类读取这些 instrument 字段做本地门控。

**OE discovery 当前状态**:NT 路径已迁移到 `OrbitExchDiscoveryClient` + `sport/details` API;旧 `OrbitExchScraper` 仅供旧 services 栈使用。2026-07-10 起 OE discovery fetcher 与 SE 同构:不开 `oe-discovery` 页、不登录、不持锁,等共享 BrowserContext `CSRF-TOKEN`(exec 登录写入)后用 context request 调 `sport/details`;首轮失败 warning 不杀 DataClient(`test_data_factory_provider_wiring.py` 覆盖 fetcher 不开页不登录,`test_data_client_step2.py` 覆盖首轮失败仍启动周期重试)。OE `start_ts` 已由 `marketStartTime` / `event.openDate` 解析并用于 NT instrument 时间字段,不写入 `instrument.info`。PM matching info 已由 `ArbPolymarketInstrumentProvider` 经 Gamma `/sports` + `/events?series_id=...` 写入,不再是旧 enricher seam 待办。

**#35(2026-05-24)Step 2 + PM enricher**:OE DataClient 整体重写 + PM ArbProvider seam(详见 data architecture.md §3)。

**#68(2026-06-07)OE DataClient competition 页订阅模型**:OE OBD 订阅从单 `inplay/highlights` 页改为每 competition 一页;同一 `MatchedPair` 会同时订阅 home/away 两腿,因此 `_ensure_competition_page` 用 `_comp_pages_lock` 防止并发双开同一 competition 页。`tests/arbitrage/adapters/orbitexch/test_data_client_step2.py` 覆盖顺序订阅去重 + 并发订阅去重,并断言 routed price frame / `OrderBookDeltas` publish 计数。live smoke 复验:同一 `2_12803182` competition 只 open 一次,两腿均 subscribed,且出现 `OE price frame routed` + `OE OrderBookDeltas published`;PM proxy 透传修复后已验证双边 OBD 触发 StrategyEvaluator 重评。

**2026-06-07 PM WS base URL 修复**:上游 `PolymarketWebSocketClient` 要求 `base_url_ws=.../ws/`,内部自行拼 `market` / `user`;旧配置沿用 `.../ws/market` 会导致 DataClient / ExecClient 目标变成 `.../ws/marketmarket` / `.../ws/marketuser`。dispatcher 已兼容旧 full endpoint 并统一归一化为 base URL;对应配置测试见 `tests/arbitrage/config/test_dispatcher.py`。

**2026-06-10 PM CLOB V2 SDK 迁移(#97)**:PM factory 统一构造 `py_clob_client_v2.ClobClient`,并注入 DataClient / ExecClient / Provider。discovery/data 侧的验收重点是“启动路径不再导入 V1 `py_clob_client`、Provider/DataClient 可用同一个 V2 client 构造”;提交/查询/撤单 surface 由 PM adapter README `pm-adapter-5.1b` 和 `tests/arbitrage/execution/test_polymarket_client.py::test_polymarket_execution_uses_py_clob_client_v2_surface` 锁定。

**2026-06-10 PM CLOB REST 路由约束(#98)**:同一个 `py_clob_client_v2.ClobClient` 现在由 factory 显式配置 `venues.polymarket.proxy_url` 到 CLOB REST transport;Provider/Discovery 的行为语义不变,只要求发现侧 CLOB 读取与 PM WS/Exec REST 使用同一路由。geoblock 仅作为 PM Execution 真下单 preflight,不阻断 discovery 只读市场发现。验收见 PM adapter README `pm-adapter-2.3b` / `5.1c`。

**2026-07-02 PMSPORTS event anchor(#127,已落地 provider slice)**:`PMSPORTS` 执行公开 Gamma discovery,
产出 `.PMSPORTS` non-tradable synthetic event instruments,供 matching 做 event anchor。PM discovery
保留并继续产出可交易 `.POLYMARKET`;PMSPORTS discovery 不产出可交易腿,也不接 Risk/Execution。详细设计见
`docs/arbitrage/architectures/_cross-cutting/sports-event-anchor.md`。

## 文件分布

| 文件 | 范围 |
|---|---|
| `../adapters/polymarket/test_arb_provider.py` | PM sports moneyline provider 解析 + matching info 写入 |
| `test_orbitexch_provider.py` | OE Provider 行为(自写,含 Q9 info dict 字段) |
| `../adapters/sharpexch/test_discovery_client.py` / `test_provider.py` | SE 第一阶段 discovery parser + Provider 行为(含 Q9 info dict 字段) |
| `../adapters/sharpexch/test_message_parser.py` / `test_data.py` | SE 第一阶段 price frame parser + runner → `OrderBookDeltas` 纯映射 |

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

**验收标准**: instrument 列表非空 AND 任取一个 instrument 调 `get_polymarket_condition_id(inst.id)` / `get_polymarket_token_id(inst.id)` 能正确反解。该用例需要真实 Gamma/CLOB 网络与凭证,归 live smoke;不保留 skipped pytest 空壳。

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

**验收标准**: 所有字段非空 AND fee_schedule 富化生效(taker_fee 不为 0);`min_quantity` 应等于 PM `minimum_order_size`(当前默认 5 shares),由 `tests/arbitrage/adapters/polymarket/test_parsing_min_size.py::test_parse_polymarket_instrument_sets_min_quantity_from_order_min_size` 锁定。

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

### discovery-1.4.i:OE competition 页懒加载禁用

**前置**:`OrbitExchScraper` 使用独立 Playwright browser 做 discovery,不复用 Data/Exec 登录浏览器。

**输入**: 调 `_setup_stealth()` 初始化 browser context。

**步骤**:
1. 向 context 注入反检测脚本。
2. 同时注入可见性欺骗:固定 `document.hidden=false`、`visibilityState="visible"`、`document.hasFocus()=true`。
3. 拦截 `IntersectionObserver`,让被观察元素立即以 `isIntersecting=true` / `intersectionRatio=1.0` 回调。

**期望**:
- OE competition 页不再只渲染首屏约 20 个 `role="row"`。
- `extract_matches()` 仍沿用原 selector,不新增滚动/分页采集逻辑。

**验收标准**:`test_orbitexch_discovery_scraper.py::test_setup_stealth_installs_visibility_spoof_for_lazy_loaded_rows` 通过;live probe 对 Wimbledon competition 直达页从 20 rows/events 提升到 96 rows/events。

### discovery-1.4.se:SE API discovery + Provider

**前置**:SE `POST /customer/api/sport/details?page={n}&size=60` fixture。

**输入**:`events_from_sport_details(payload)` + `SharpExchInstrumentProvider._build_legs(event)`。

**期望**:
- `sport_details_request` 为 Tennis/Wimbledon 构造 `POST /customer/api/sport/details?page=0&size=60`,body `id="2"`。
- `SharpExchDiscoveryClient` 默认不联网;只有显式注入 `sport_details_provider` / `json_fetcher` 时才拉取 payload。
- `json_fetcher` 路径分页请求,直到短页、空页、下一页无新 `marketId`,或 100 页保护上限。
- `SharpExchLiveDataClientFactory` 在 discovery config 存在时注入 browser `json_fetcher`;该 fetcher 不创建 page、不登录、不导航,只等待共享 BrowserContext 中的 `CSRF-TOKEN`,随后用 context request 执行 `sport/details`。
- 只保留 `Match Odds` market,按目标 competition 过滤。
- 2 runner 映射 `home/away`;3 runner 映射 `home/draw/away`。
- Provider 产 `BettingInstrument`,venue 为 `SHARPEXCH`,info 含 matching key。
- `min_notional = Money(12, USD)` 作为 SE 第一阶段默认最小 stake。

**验收标准**:`tests/arbitrage/adapters/sharpexch/test_discovery_client.py`、`test_provider.py`、`test_factories.py` 通过;2026-07-01 zero-order probe 实测 `sport/details` 分页返回 242 个 Tennis events,其中 `Men's Wimbledon 2026` 为 64 个。

### discovery-pmsports-anchor.1:PMSPORTS synthetic event instruments(已落地,#127)

**前置**:`data_sources.sports_status.enabled=true`;常规配置可不写 `data_sources` 段,目标
competitions 默认继承 `discovery.polymarket.sports`;dispatcher 写入
`target_competitions_by_data_source["PMSPORTS"]`;只有 PMSPORTS 目标需要和 PM
tradable discovery 分离时,才显式配置 `data_sources.sports_status.sports`。

**输入**:PMSPORTS discovery 拉公开 Gamma `/sports` + `/events?series_id=...`。

**期望**:
- 每场比赛产出一个 `{game_id}.PMSPORTS` synthetic instrument。
- `instrument.info` 含 `sport/competition/home_team/away_team/game_id`。
- `instrument.info["tradable"] is False` 且 `instrument.info["anchor"] is True`。
- 不产出 `.POLYMARKET` instrument,不写 PM token/order book/min order 字段。

**验收**:
- `tests/arbitrage/adapters/polymarket/test_sports.py::test_sports_provider_builds_non_tradable_anchor_instrument`。
- Cache 可读到 `venue=PMSPORTS` instruments(待 live smoke)。
- Strategy / Risk / Execution 相关测试确认 `.PMSPORTS` 不进入套利流(后续端到端补充)。

---

### discovery-7B.1(slice 7B,#53/#57):PM moneyline provider 写 matching info

**前置**: `ArbPolymarketInstrumentProvider.load_all_async` 走 Gamma `/sports` 取 series/order，再走 `/events?series_id=...` 拉内嵌 teams + markets;`_load_moneyline_market` 创建每个 PM token 后补 matching info。

**输入**: PM Gamma event + moneyline market(含 `teams`/`ticker`/`markets[].sportsMarketType=moneyline` / `clobTokenIds` / `outcomes`)。

**期望**:
- `info["sport"]` 来自 `competition_to_sport_by_data_source["PMSPORTS"]`
- `info["competition"]` 使用 PM venue competition alias 标准化后的名字
- `home_team` / `away_team` 优先来自 `event["teams"]`,缺失时 fallback title 解析
- `selection_role` 按 moneyline slug + competition ordering + team abbreviation 解析为 `home/draw/away`
- `game_id` 等于 Gamma event `gameId`;`start_ts` 不写入 matching info。

**验收**:`tests/arbitrage/adapters/polymarket/test_arb_provider.py` 覆盖 teams/title 解析、role 解析和 `_load_moneyline_market` 写入完整 matching info;完整 Gamma HTTP 路径仍归 live smoke。

### discovery-7B.2(slice 7B,#57):PM series-based discovery 路径

**前置**: `ArbContext.target_competitions_by_data_source["PMSPORTS"] = ["atp"]`(launcher 经 dispatcher 从 `data_sources.sports_status.sports` 或继承的 `cfg.discovery.polymarket.sports[].competitions` 派生)

**输入**: 启动 PM Provider,`ArbPolymarketInstrumentProvider.load_all_async` 原生执行。

**期望**:
- 先调 Gamma `/sports`,按目标 competition 找到 series id 与 ordering。
- 再调 `/events?series_id={id}&closed=false&active=true&limit=500`,一次拉该 series 内嵌 teams + markets。
- 只取 `sportsMarketType=moneyline` 主市场生成 PM 可交易 legs;不再走上游 `event_slug_builder` 或全量 `_load_all_using_gamma_markets`。

**验收**: `_load_moneyline_market` 离线测试锁定 info 写入;完整 Gamma HTTP 路径仍由 live smoke 验。

---

### discovery-2.A.1(slice 8A,#47,#59修订):Provider 回写 ArbContext

**前置**: `node.build()` 已运行,data factory 在 `create` 内回写 `ArbContext.{pm,oe,se}_instrument_provider = provider`

**输入**: launcher `add_actors(node, cfg, pair_registry=...)` 在 build 后调用

**期望**:
- PM `ArbPolymarketLiveDataClientFactory.create` 完成后,`ctx.instrument_provider_by_venue["POLYMARKET"] is provider`(same instance)
- OE `OrbitExchLiveDataClientFactory.create` 完成后,`ctx.instrument_provider_by_venue["ORBITEXCH"] is provider`
- SE `SharpExchLiveDataClientFactory.create` 完成后,`ctx.instrument_provider_by_venue["SHARPEXCH"] is provider`
- launcher `add_actors` 不再构造 InstrumentRefresher;发现周期由各 DataClient 原生 `_update_instruments` 负责。Provider 回写仅作为共享对象/测试/后续 adapter 迁移入口。

**provider 缺失场景**: Data factory 未跑 / discovery 禁用时 provider map 可为空,launcher 不依赖 provider map 决定 actor 数。
**SE 状态**: `SharpExchLiveDataClientFactory` 已回写 keyed provider,并在 discovery config 存在时注入 browser 分页 `json_fetcher`;launcher 仅在 `venues.sharpexch.enabled=true` 时 opt-in 注册 SE factory。

**测试**:provider keyed 回写由 PM/OE/SE adapter factory 测试覆盖;`tests/arbitrage/launchers/test_arb_node.py` 覆盖 add_actors 只装 Matching + Strategy,Web 开启时额外装 WebGateway。

---

### discovery-1.4.g/h(slice 7A,#46):OE Provider aliases 注入

**前置**: `OrbitExchInstrumentProvider(scraper, sport_aliases={...}, competition_aliases={...})` 构造

**输入**: scraper 返一个 MatchEvent(`sport="soccer"`, `competition="Men's Wimbledon 2026"`)

**期望**:
- `info["sport"]` = `sport_aliases.get("soccer", "soccer")` (alias 命中则规范名,否则原值透传)
- `info["competition"]` = `competition_aliases.get("Men's Wimbledon 2026", ...)`
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
- 默认 `fx=1` 时 `min_notional=7 USD`;非默认 fx 时为 `Money(7 * fx, USD)`(例如 `fx=1.3` → `9.1 USD`),表达 OE 最小 stake 的 USD 口径门控。由 `test_build_legs_sets_orbitexch_min_stake` / `test_build_legs_sets_orbitexch_min_stake_with_fx` 锁定

### discovery-pm-minimum.2:PM BUY-only 最小金额(#234)

- PM `minimum_order_size` 继续映射为 `BinaryOption.min_quantity`，同时 parsing 在 instrument info 写 `min_buy_notional=1.0`。
- 不写通用 `BinaryOption.min_notional`，因为 1 USD 仅约束 BUY、SELL 只受最小 shares 约束。
- 验收：`tests/arbitrage/adapters/polymarket/test_parsing_min_size.py`。

**验收标准**: instrument 列表非空 AND helper 可逆 AND page name 是 `"discovery"`(不是 `"data"` / `"execution"`)

---

### discovery-1.5: OE Provider info dict 必含 matching key (Q9)

**前置**: discovery-1.4 通过

**输入**: 取一个 OE `BettingInstrument`

**期望**: `instrument.info` dict 必须包含以下 key(全部非空):
- `info["sport"]` (str) —— 运动类型
- `info["competition"]` (str) —— 联赛名
- `info["home_team"]` (str)
- `info["away_team"]` (str)
- `info["selection_role"]` (str: "home" / "draw" / "away")

**验收标准**: matching key 全部存在且类型正确(`MarketMatchingActor` 跨 venue 归一依赖此)。`start_ts` 是 discovery event / NT instrument 时间字段,不属于 matching info。

**对应章节**: `refactor.md §6.4`

---

### discovery-1.6: OE Provider 与 PM Provider 共享匹配字段

**前置**: discovery-1.1 / 1.4 都通过

**输入**: 任取 PM `BinaryOption` 与 OE `BettingInstrument`

**期望**: 两者的 `info` dict 都含 `sport` / `competition` / `home_team` / `away_team` / `selection_role` key,语义一致(MatchEngine 不需要 isinstance 区分类型即可读)

**验收标准**: 取 matching key 不抛 KeyError;PM/OE/SE `selection_role` 均使用 {"home", "draw", "away"} 语义集合(二元盘不含 draw)。

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

### discovery-2.x: DataClient 原生周期发现

**前置**: 对应 venue/data source enabled,DataClient 已构造 Provider 并接入 DataEngine。

**输入**: DataClient `_connect` 首抓,以及后续 `_update_instruments` 周期任务。

**期望**:
- 首抓和周期重抓都调用 Provider 加载当前 instruments。
- 成功结果经 `_handle_data` 进入 DataEngine/cache,并触发 NT 原生 `on_instrument`。
- 单轮普通异常只记录 warning,下一轮继续;`CancelledError` 仅在组件 stop 时退出。
- MatchingActor 不订 discovery 事件,由 timer 读 cache。

**验收标准**: PM/OE/SE/PMSPORTS 各 adapter 的 DataClient/Provider 测试分别覆盖;全节点链路由 matching/e2e smoke 验证。

## #228:3-way 每 selection 产 yes/no 两腿(2026-07-15)

- `test_orbitexch_provider.py::test_build_legs_three_way`:OE 3-way = 6 条腿((home/draw/away)×(yes/no));合成 no 使用负 selection + null handicap 的非 composite identity，真实 selection 存 `venue_selection_id`，并携带 `claim=no/quote_claim=no/exec_instrument_id`。
- `test_build_legs_two_way_drops_missing_draw`:2-way 只产真实 home/away 两腿，但 claim 统一为 yes/no，且无执行重定向。
