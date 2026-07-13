# SharpExch 接入详细设计

> **状态**: 第一阶段 OE 型接入已落地:配置 schema/env/dispatcher、`sport/details` 解析与 browser fetcher、Provider 产 `BettingInstrument`、DataClient 订阅/WS 路由、ExecutionClient page/WS 生命周期与 place/cancel/fill 边界、factory 与 launcher opt-in 接线、MatchingActor 多 tradable venue、Strategy/Risk 通过 venue capability 识别 SE。
> 2026-07-01 已用独立 zero-order probe 验证真实 SE 登录、customer iframe、`sport/details`、competition prices/general WS;skip node smoke 已验证 PM↔SE matching、SE 订阅、routed price frame、OrderBookDeltas 发布、SE `BALANCE`/`CURRENT_BETS` execution 业务帧与 SHARPEXCH startup reconciliation;真单 place+cancel probe 已通过(BACK@100,size=12,venue_order_id=22157223,随后撤单且兜底活单 0);真成交 fill probe 已通过(BACK@1.01,size=12,offerId=22160783,`sizeMatched=12.0`,`averagePrice=4.5`,`generate_order_filled` 1 次,余量 0)。`venues.sharpexch.enabled` 默认 `false`,显式置 `true` 才注册进 runtime。第二阶段静态 venue capability 已收口到横切设计,SE adapter 内部仍保留真实网站差异。
> 决策理由与历史只在 `docs/arbitrage/refactor.md` 留指针,本文是 SE 接入设计真理源。
> DevTools 探站时间:2026-06-30。

---

## 1. 目标与边界

第一阶段把 SharpExch 当作第三个真实 venue 接进现有套利节点:

| 维度 | 第一阶段决定 |
|---|---|
| venue id | `SHARPEXCH` |
| adapter 位置 | `nautilus_trader/adapters/sharpexch/` |
| instrument 类型 | `BettingInstrument` |
| InstrumentId | `{market_id}-{selection_id}.SHARPEXCH` |
| 运行模型 | Playwright 浏览器 + portal API + SockJS WS,对齐 OE |
| 账户币种 | SE 前端 / `CURRENT_BETS` / place payload 当前均为 USD;adapter runtime 不做 FX 换算。账户余额 runtime 从 HTTP profile/balance response 提取,不信 WS `BALANCE` 帧。 |
| Strategy/Risk | 不新增 SE 特殊分支;继续只吃 NT 标准 order book / account / instrument 元数据 |

第一阶段必须遵循三条硬原则:
- **不改架构**:SE 作为新增 venue adapter 同级接入,不重组 PM/OE、Strategy、Risk、Execution、Matching 的组件边界。
- **不改逻辑**:不改变现有 PM/OE 套利判断、风控门控、barrier 同步、FX 口径、profit/share limit 等业务逻辑;SE 只产出相同 NT 标准数据契约。
- **不改流程**:在 SE data/exec/factory/launcher 未完整接线前,不得把 SE 半成品塞进现有 runtime 启动链路;现有 PM/OE 启动、发现、匹配、下单流程不因 SE 代码存在而改变。

不在第一阶段做:
- 不把 OE/SE 立即抽成通用插件框架。
- 不改 Strategy/Risk 的套利语义。
- 不接 Binance/crypto。
- 不跑实盘下单验证,除非用户明确要求。

第二阶段目标是 venue 可插拔化,但必须等 OE+SE 两套真实 adapter 跑通后再抽象,避免把“看起来类似”的页面细节过早固化成框架契约。

---

## 2. DevTools 实测结论

### 2.1 站点与页面模型

DevTools 实测:
- `https://www.sharpexch.com` 跳转到 `https://sharpxch.com/player/`。
- 外层 `sharpxch.com/player/...` 页面包含登录表单,并内嵌 `https://portal.sharpxch.com/customer...` iframe。
- portal SPA 标题为 `B2B Exchange`,URL 模式与 OE 同源:
  - sport 页:`https://portal.sharpxch.com/customer/sport/{sport_id}`
  - competition 页:`https://portal.sharpxch.com/customer/sport/{sport_id}/competition/{competition_id}`
  - market 页:`https://portal.sharpxch.com/customer/sport/{sport_id}/market/{market_id}`
- Tennis sport id 实测为 `2`;页面左侧 competitions 含 `Men's Wimbledon 2026`。

### 2.2 API 与 WS

DevTools 实测到以下 endpoint:

| 类型 | endpoint | 结论 |
|---|---|---|
| sports | `GET /customer/api/sports` | 与 OE 类似,用于 sport 菜单 |
| competition list | `GET /customer/api/competition/sport/2?showGroups=true` | 返回 `topCompetitions` / `moreCompetitions`,含 competition id/name |
| sport details | `POST /customer/api/sport/details?page={n}&size=60` | 返回 `sportInfo` + `marketCatalogueList.content[]`;每条 market 含 marketId、runners、eventType、competition、event、inPlay、commission |
| general SockJS info | `GET /customer/ws/general/info?...` | 返回 `websocket:true` |
| prices SockJS info | `GET /customer/ws/multiple-market-prices/info?...` | 返回 `websocket:true` |

`sport/details` 请求体实测:

```json
{"viewBy":"POPULARITY","timeFilter":"ALL","id":"2","contextFilter":"EVENT_TYPE"}
```

`sport/details` 响应中的 `marketCatalogueList.content[]` 已足够构建 SE provider,无需第一阶段依赖 DOM 行扫描:
- `marketId`:例如 `1.259502313`
- `marketName`:例如 `Match Odds`
- `marketStartTime`:毫秒时间戳
- `runners[]`:含 `selectionId` / `runnerName` / `handicap`
- `eventType.id/name`
- `competition.id/name`
- `event.id/name/homeTeam/awayTeam/openDate`
- `inPlay`
- `commission`

### 2.3 与 OE 的相似点和差异

| 方面 | OE | SE 实测 |
|---|---|---|
| portal 路径 | `/customer/...` | `/customer/...` |
| sport/competition URL | `/customer/sport/{sport}/competition/{competition}` | 相同 |
| prices WS | `/customer/ws/multiple-market-prices` | 相同命名 |
| general WS | `/customer/ws/general` | 相同命名 |
| competition 列表 | 页面 DOM + API 可见 | API 明确可用 |
| sport details | OE 当前代码主要走 DOM discovery | SE 可优先 API discovery |
| 外层站点 | `www.orbitexch.com` | `sharpxch.com/player` iframe 到 `portal.sharpxch.com` |

第一阶段实现应按 OE adapter 复制结构,但 SE discovery 优先走 portal API,只把 DOM 抓取作为 fallback。这样减少懒加载/滚动/visibility 相关风险。

---

## 3. 第一阶段落地结构

新增目录:

```text
nautilus_trader/adapters/sharpexch/
  __init__.py
  config.py
  discovery_client.py
  providers.py
  browser_manager.py
  data.py
  execution.py
  executor.py
  factories.py
  message_parser.py
  web.py
  websocket_handler.py
```

第一阶段允许从 OE 复制后改名,但复制时必须保留清晰的 SE 边界,不能把 `ORBITEXCH` 常量散落在 SE 子树。

已落地文件(2026-06-30 第一片):
- `config.py`: `SharpExchDataClientConfig` / `SharpExchExecClientConfig`
- `discovery_client.py`: `sport_details_request` + `events_from_sport_details` + 可注入 `SharpExchDiscoveryClient`
- `providers.py`: `SharpExchInstrumentProvider`
- `message_parser.py`: `SharpExchMessageParser.parse_price_message` / `parse_general_frame`
- `data.py`: `se_runner_to_book_deltas`
- `execution.py`: `SharpExchLegacyOrder` / `nt_order_to_legacy_order`
- `executor.py`: `SharpExchExecutor` page-bound place/cancel 薄封装
- `web.py`: SE customer iframe / 登录 / browser fetch helper
- `websocket_handler.py`: `SharpExchWebSocketHandler` SockJS 解包 + prices/general 分发
- `browser_manager.py`: SE adapter 内独立导入入口,当前复用 OE `PlaywrightBrowserManager`
- `factories.py`: `SharpExchLiveDataClientFactory` / `ArbSharpExchLiveExecClientFactory` 离线构造
- `__init__.py`: adapter 包导出

### 3.1 config

```python
class SharpExchDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    username: str
    password: str
    base_url: str = "https://portal.sharpxch.com"
    login_url: str = "https://sharpxch.com/player/"
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: str | None = None
    page_timeout: int = 120000
    update_instruments_interval_mins: int | None = 60
    staleness_timeout_secs: float = 300.0
```

`ArbConfig` 第一阶段增加:
- `discovery.sharpexch: VenueDiscoveryConfig`
- `venues.sharpexch: SharpExchSectionConfig`
- `venues.sharpexch.enabled: bool = False` 作为 runtime opt-in 开关
- env 注入: `SHARPEXCH_USERNAME` / `SHARPEXCH_PASSWORD`

SharpExch 的 `base_url` 指 portal 域,`login_url` 指外层登录页。这样后续如果某些 API 必须从 iframe session 继承 cookie,执行端仍能统一由登录页进入。

已落地:
- `ArbConfig.discovery.sharpexch`
- `ArbConfig.venues.sharpexch`
- `SHARPEXCH_USERNAME` / `SHARPEXCH_PASSWORD` env 注入
- `to_sharpexch_data_client_config` / `to_sharpexch_exec_client_config` / `to_se_discovery_config`
- launcher 按 `venues.*.enabled` 注册 runtime venue;matching 使用 PMSPORTS anchor +
  enabled tradable venues。`venues.sharpexch.enabled=true` 且 `venues.orbitexch.enabled=false`
  时可跑 PM+SE smoke,不会启动 OE。
- 生产 Data/Exec factory 与 SE probes 共用 `SharpExch` 子树导出的 `PlaywrightBrowserManager`。
  该 manager 继承 OE 的反自动化/可见性设置:Chromium `AutomationControlled` 参数、固定
  user-agent、隐藏 `navigator.webdriver`、模拟 plugins、固定 `document.visibilityState`
  为 visible。probe 通过 `--user-data-dir` 复用的人工 Cloudflare 验证 profile 不会自动进入生产;
  生产若要复用,必须显式配置 `venues.sharpexch.user_data_dir` 为同一路径。

### 3.2 discovery/provider

第一阶段不沿用 OE 的 DOM `role=row` 抓取作为主路径,而是新增 API discovery:

```python
class SharpExchDiscoveryClient:
    async def discover_events(self, sport_configs: list[SportConfig]) -> list[SharpExchMarketEvent]:
        # 1. sport_details_request(base_url, sport_config, page=n) 构造分页 sport/details 请求
        # 2. 由 factory/DataClient 后续显式注入 json_fetcher 执行请求
        # 3. 过滤 config 目标 competitions + marketName == "Match Odds"
```

当前已落地的是请求描述、parser 与 factory 注入的 Playwright fetcher:
- `sport_details_request("https://portal.sharpxch.com", SportConfig("Tennis", ["Men's Wimbledon 2026"]))`
  默认生成 `POST /customer/api/sport/details?page=0&size=60`,body 为
  `{"viewBy":"POPULARITY","timeFilter":"ALL","id":"2","contextFilter":"EVENT_TYPE"}`。
- `SharpExchDiscoveryClient` 只有传入 `sport_details_provider` 或 `json_fetcher` 时才会产生事件;无注入时返回空列表。`json_fetcher` 路径会分页请求,直到空页、短页、下一页没有新 `marketId`,或 100 页保护上限。
- `SharpExchLiveDataClientFactory` 在 `discovery_config_by_venue["SHARPEXCH"]` 存在时注入 browser `json_fetcher`:不创建 discovery page、不登录、不导航;只等待共享 `PlaywrightBrowserManager.context` 中出现 `CSRF-TOKEN`(由 execution page 或其它 SE 页面产生),最多等待 `config.page_timeout`,随后用 BrowserContext request API 调 `sport/details`。Data factory 会把该 discovery config 的 `sports` 同时注入 Provider 的 `sport_configs`,Provider `load_all_async()` 调用 `discover_events(sport_configs)`,避免 runtime 退回默认 sport。
- Data discovery fetch 与 Exec login 只共用 `browser_manager_by_venue["SHARPEXCH"]`。Discovery **不持任何锁**:CSRF 等待与 context request 对共享 context 只读;`browser_login_state_by_venue["SHARPEXCH"].lock` 仅串行化 execution 登录(若 discovery 持该锁等 CSRF,会把唯一的 CSRF 生产者——exec 登录——挡在锁外直到超时)。execution 登录仍是唯一会提交账号凭据的路径。order book/WS 后续仍按各 client 自己的事件路径运行。
- 独立 probe 实测该路径可分页取回约 240 个 Tennis events(随 SE 当前赛事集变化);
  其中 `Men's Wimbledon 2026` 为 64 个、`Women's Wimbledon 2026` 为 64 个。

runner → role 规则:
- 2 runner tennis:按 `event.homeTeam` / `event.awayTeam` 与 runnerName 匹配,匹配不到时按顺序 `[home, away]` fallback。
- 3 runner soccer:按 `[home, draw, away]`,draw 名称可为 `Draw` / `X`。
- 若 runner 无法映射 role,该 runner 不产 instrument,并记录低噪声 warning。

`info` 必须填 Q9 统一字段:`sport`、`competition`、`home_team`、`away_team`、`start_ts`、`selection_role`。

最小下单元数据:
- SE 最小 stake 已在 2026-07-01 真单 preflight 中确认为 12 USD,Provider 写入
  `min_notional = Money(12, USD)`。

### 3.3 data client

`SharpExchDataClient` 复用 OE 的 competition 页模型:
- `_connect`:启动共享 browser,首轮 provider load,把 instruments 送入 DataEngine,启动周期发现。
- `_subscribe_order_book_deltas`:注册 `market_id + selection_id -> InstrumentId`,并 eager 打开 competition 页。
- competition 页 URL:`{base_url}/customer/sport/{sport_id}/competition/{competition_id}`。
- WS handler 捕获 `multiple-market-prices`。
- price frame parser 按 SE/OE BIAB payload 兼容实现,支持 `bdatb/bdatl` 的
  dict-of-levels 结构(如 `{"0":[price,size]}`)以及 `batb/batl` 的 list 档结构。
- live 验收锚点对齐 OE:首个已路由行情帧打印 `SE price frame routed`,首次实际发布 deltas 打印 `SE OrderBookDeltas published`;两者均只打印一次,用于区分“WS 已连但未路由”和“已进入 NT order book”。

输出仍是 NT 标准 `OrderBookDeltas`:当前链路只消费 top-of-book,因此源头只发布
BACK 侧最高赔率为 SELL/ask、LAY 侧最低赔率为 BUY/bid,每帧按 snapshot 处理。

当前已落地的是 DataClient runtime 接线与纯映射层:
- `SharpExchDataClient` 已接入 launcher 显式 opt-in runtime:维护
  `market_id -> selection_id -> InstrumentId`、`market_id -> page_key`、
  `page_key -> (sport_id,competition_id)` 与 competition page registry;`_connect`
  启动注入的共享 browser manager、首轮 `load_all_async()`、把 provider instruments 灌入 DataEngine,
  并按配置启动 `_update_instruments`;`_disconnect` 取消周期发现 task、停止已开 competition
  handlers、清空 page registry,但不关闭共享 browser;`_update_instruments` 单轮失败只 log,
  下一轮继续;`_subscribe_order_book_deltas`
  复用 subscription helper 注册状态并调用注入式 open/reload;`_unsubscribe_order_book_deltas`
  复用移除 helper;`_on_price_frame` 复用 `se_handle_price_frame(...)` 发布 deltas 并写 `in_play`;
  `_on_comp_disconnect` 对 `close:prices` / `liveness_timeout` 建 reload task,`_reload_comp_on_disconnect`
  调用同一 open/reload 方法并在失败时只 log + 清 reload-in-flight;订阅开页失败时排
  `_delayed_reopen`,醒来后复用 `se_reopen_missing_page(...)`,失败时再排下一轮。
  竞争条件通过 `_comp_pages_lock` 收口:`_subscribe_order_book_deltas`、`_delayed_reopen`、
  `_reload_comp_on_disconnect` 进入 open/reload 前持锁,同一 competition 的 home/away 并发订阅只允许一条路径真正开页,第二条复用已登记的 page。
  当前已可由 factory 构造并经 launcher 显式 opt-in 进入 runtime。
- `se_routing_entry_from_instrument` / `se_update_market_routing` /
  `se_remove_market_routing` 已落地:从 SE `BettingInstrument` 属性读取 `market_id` /
  `selection_id`,统一转字符串后维护 `market_id -> selection_id -> InstrumentId` routing;
  缺字段时返回 false/None,由后续 DataClient 记录 warning 并跳过。
- `se_subscription_plan_from_instrument(...)` / `se_update_subscription_state(...)` 已落地:
  从一个 SE instrument 同时派生 routing key 与 page ref,并一次性维护
  `market_id -> selection_id -> InstrumentId`、`market_id -> page_key`、
  `page_key -> (sport_id, competition_id)` 三张后续 DataClient 状态表;
  缺任一必要字段时不写状态。
- `se_remove_subscription_state(...)` 已落地:解订时移除目标 instrument 的 selection routing;
  若 market 已无 selection,同步删除 `market_to_page_key`;若该 page_key 已无任何 market 引用,
  再删除 `comp_page_refs`。这避免开页失败重试在解订后误判 page 仍被订阅。
- `se_competition_page_ref_from_instrument` / `se_competition_page_url` 已落地:
  从 `BettingInstrument.event_type_id` / `competition_id` 生成 `(page_key,sport_id,competition_id)`,
  page key 为 `{sport_id}_{competition_id}`,URL 为
  `{base_url}/customer/sport/{sport_id}/competition/{competition_id}`。
- `se_should_reopen_missing_page(...)` 已落地:用于开页失败后的延迟重试醒来时判定是否继续,
  条件是未关停、目标 page 仍未打开、且 `market_to_page_key` 仍有订阅 market 指向该 page_key。
  它只返回 bool,不 sleep、不创建 task。
- `se_reopen_missing_page(...)` 已落地为组合 helper:调用方显式传入 page_key、
  `market_to_page_key`、`comp_page_refs`、`comp_pages` 与 open-page coroutine;helper 先复用
  `se_should_reopen_missing_page(...)`,再从 `comp_page_refs` 找 sport/competition 并调用 open-page。
  它不 sleep、不创建 task,open-page 异常向上抛。
- `se_ensure_competition_page(...)` 已落地为可注入 helper:调用方传入订阅 plan、
  当前 `comp_pages` 与 open-page coroutine;若页已存在则返回 `already_open`,若缺页则调用
  open-page,坏 plan 直接 no-op,open-page 异常向上抛。它不创建 task、不持 lock、不接 NT 类。
- `se_websocket_summary(handler)` 已落地:读取 handler `get_active_websockets()` 与
  `get_frame_counts()` 并生成 `ws_count=N, ws_types={...}, frame_counts={...}` 摘要,
  用于 competition 页 open/reload 日志锚点。
- `SharpExchWebSocketHandler.get_frame_counts()` 已落地:只读暴露每类 WS 入向帧计数,
  供 competition 页生命周期等待与 probe 摘要使用;它不改变 frame 分发语义。
- `se_open_or_reload_competition_page(...)` 已落地为可注入 helper:调用方传入
  `browser_manager`、`comp_pages`、`comp_handlers`、price/disconnect callbacks 后,
  helper 负责新开时 `create_page -> handler.start -> bring_to_front -> goto(domcontentloaded) -> registry 写入`,
  reload 时复用已有 page 并 `bring_to_front -> reload(domcontentloaded)`;与 OE 一致,`domcontentloaded`
  之后不再等 prices 首帧(SockJS feed 由 handler 后续帧与 staleness liveness 自愈),
  新开失败时 stop handler 并 close page。`SharpExchDataClient` 调用时把 NT `clock`、
  `config.staleness_timeout_secs`、`liveness_ws_type="prices"` 注入 handler,因此
  `venues.sharpexch.staleness_timeout_sec` 是 competition prices WS 的静默断流 timeout。
  它不创建 runtime task、不注册 NT client,由 `SharpExchDataClient` 在订阅开页与 reload 路径调用。
- `se_should_reload_on_disconnect(...)` 已落地:只对 `liveness_timeout` / `close:prices`
  触发 reload 判定,并处理 disconnecting、reload-in-flight、页不存在、冷却窗防护;
  允许 reload 时写入 `comp_last_reload_ns`。它只返回 bool,不创建 task。
- `se_reload_competition_on_disconnect(...)` 已落地为组合 helper:调用方显式传入
  `comp_page_refs`、browser/page/handler registry、reload-in-flight set、冷却时间与 callbacks;
  helper 先做 reload 判定,再用 `comp_reloading` 包住 `se_open_or_reload_competition_page(...)`。
  它不创建后台 task、不吞异常,失败时只保证清掉 reload-in-flight 标记。
- `SharpExchMessageParser.parse_price_message(message)` 解析 BIAB/OE 型 price frame,支持
  `bdatb`/`bdatl` dict 档、dict-of-levels 数组值(`{"0":[price,size]}`)和
  `batb`/`batl` list 档,输出 `{market_id, runners[]}` 结构。
- `se_runner_to_book_deltas(instrument_id, runner, ts)` 把单 runner 快照转为 NT
  `OrderBookDeltas`:先 `CLEAR`,再发布 top-of-book:BACK 最高价 `ADD(SELL)`、
  LAY 最低价 `ADD(BUY)`。
- `se_price_message_to_book_deltas(message, routing, ts)` 已落地:把 handler 收到的 price WS
  message 经 `SharpExchMessageParser` 解析后,按 DataClient 维护的
  `selection_id -> InstrumentId` routing 表生成要 publish 的 `OrderBookDeltas` 列表;
  未订阅 runner、空档 runner、非法消息均跳过。
- `se_market_price_message_to_book_deltas(message, market_routing, ts)` 已落地:
  输入 DataClient 实际维护的 `market_id -> selection_id -> InstrumentId` routing,
  先按 price frame 的 `market_id` 找 selection routing,再生成 `{market_id,in_play,runners,
  subscribed_selections,deltas}`。未路由 market / 非法消息返回 `None`;已路由但空档时保留
  frame 元信息并返回空 `deltas`。
- `se_publish_routed_book_deltas(routed_payload, publish, write_in_play=None)` 已落地:
  输入上一条 helper 的 routed payload,逐个 `OrderBookDeltas` 调用注入的 publish 函数,
  并可选按 instrument 写入 `in_play`;返回实际发布数量。空 payload / 空 deltas no-op。
- `se_handle_price_frame(message, market_routing, ts, publish, write_in_play=None)` 已落地:
  组合 market-level routing 与 publish 两步,返回 routed payload 并追加 `published_count`。
  未路由 frame 返回 `None`;已路由但空档时返回 frame 元信息且 `published_count=0`。
- `SharpExchWebSocketHandler` 已落地:监听 Playwright `page.on("websocket")`,按 URL 分型
  `multiple-market-prices`→`prices`、`general`→`orders`,解包 SockJS `a[...]` 业务帧并调用
  price/order callback;`[` 开头的上行 subscribe 帧与 `o`/`h` 心跳不进业务 callback。
  handler 保持 page-bound 分发,不持有 NT client 状态;可选接入 NT clock 做内部 liveness:
  配置了 `clock + liveness_timeout_secs` 时,只有目标 feed(competition 页为 `prices`)的非空入向帧
  刷新 `_last_frame_ns`,lazy self-rescheduling time-alert 到期发现超时则触发
  `on_disconnect("liveness_timeout")`。`orders/general` 心跳不会掩盖 prices WS stale。
- `SharpExchDataClient` 已接 runtime:launcher 显式 opt-in 后可首轮发现 instruments、订阅时开
  competition 页、路由 price frame 并发布 `OrderBookDeltas`;skip node smoke 已验证该路径。

存活:
- 复用 handler 内部 liveness 模型:prices WS 入向帧/心跳刷新 `_last_frame_ns`,timeout 或 close 触发 competition reload;orders/general 心跳不刷新 competition prices liveness。
- 日志前缀必须是 `SE` 或 `SharpExch`,不能继续打印 `OE WS ...`。

### 3.4 execution client

`SharpExchExecutionClient` 第一阶段复制 OE 的执行模型:
- 登录页从 `login_url` 进入,成功后真实 customer app 在 `portal.sharpxch.com/customer` iframe 内。
- general WS 捕获 `BALANCE` / `CURRENT_BETS` 类帧;`CURRENT_BETS` 是 order/fill/reconcile 主输入。
- `BALANCE` parser 保留,但 runtime 不再用 WS `BALANCE` 写 `AccountState`;账户余额改由登录后 page `response` 监听从 HTTP profile/balance API 响应中提取。未捕获时启动期写 0 USD 兜底账户状态。
- `CURRENT_BETS` 入站金额/size 字段按 USD 原值保留。
- 下单 payload 直接使用 NT order quantity 的 USD stake,不除以 fx。
- `nt_order_to_legacy_order` 产物应使用 `Venue.SHARPEXCH` 或新增通用 order model 支持,不能复用 `Venue.ORBITEXCH`。
- `_connect` 登录后最多等待 30s 让两个业务就绪信号到达:HTTP profile/balance response 与
  execution general WS `CURRENT_BETS`。超时 fail-soft 继续启动,但缺失项必须打 warning;余额缺失时仍按
  0 USD 兜底注册账户,`CURRENT_BETS` 缺失则交后续 reload/reconcile 自愈。

当前已落地的是 ExecutionClient runtime 接线、page/WS 生命周期、general 帧解析与 place/cancel 边界:
- `SharpExchExecutionClient` 已接入 launcher 显式 opt-in runtime:继承
  `ArbExecutionSessionMixin + LiveExecutionClient`,账号为 `SHARPEXCH-001`,base currency 为
  `USD`;`_connect` 启动共享 browser manager、创建 `"execution"` page、先挂
  `SharpExchWebSocketHandler` 再执行 `se_login`(见 §3.6 Login 串行化);该 helper 不使用 `networkidle`,而是先检查
  外层登录表单,有表单时必须提交凭据,只有确认没有登录表单时才把 `/customer` iframe
  视为可复用登录态,兼容 `sharpxch.com/player/` 外层页长期不跳转且未授权时也可能预加载
  customer iframe 的真实行为;同一 browser context 内若已有 page 完成登录,后续 page 在
  `SharpExchLoginState` 锁内优先复用 authenticated session,避免 discovery/execution 旧登录页二次提交;
  `postLoginPopup` 弹窗在登录成功、customer app 启动完成后才渲染,因此由 `se_login`
  在登录完成后(锁外)统一按 OE #89 同款策略关闭,总预算 10s,无弹窗时静默继续。
  登录后 SE app 会 detach 重建 customer iframe(表现为 orders/prices WS 连两波),
  dismiss 按 1s 分片、每片重新解析当前 customer context,不持旧 frame 引用。
  `_connect` 使用 Future 等待 profile/balance 与 `CURRENT_BETS`
  两个业务信号,等待上限 30s,不再使用 8s 固定轮询只等余额。
  若余额缺失,连接完成后生成 0 USD 初始 `AccountState` 以注册账户;
  `_disconnect` 停止 WS handler 并清本地 page,不关闭共享 browser。
- `_on_general_frame` 已接 parser:WS `BALANCE` 忽略(实测可能返回 0.00,不作为账户事实);
  `CURRENT_BETS` 调 `_on_current_bets`。
- `_on_current_bets` 已维护 USD 口径 `_current_bets` 快照、刷新 `_last_current_bets_ns`,
  首帧打印一次 `SE CURRENT_BETS routed: bets=N`,可写 `VenueExecutionLiveness` 的
  order/position alive。`sizeMatched` 是累计成交量;`_on_current_bets` 不维护 `prevMatched`,
  而是用累计 `sizeMatched` 与 NT order 当前 `filled_qty` 推出本次 `generate_order_filled.last_qty`。
- `SharpExchWebSocketHandler.on_frame(...)` 已落地:任意非空 WS 帧(含 SockJS 心跳)都会刷新
  ExecutionClient 的 `_last_frame_ns`。SE reconcile 与 OE 一样只信“有 CURRENT_BETS 快照且 general
  WS 新鲜”的状态;否则 `_ensure_exec_snapshot_fresh()` 触发 single-flight `_reload_exec_page()`,
  reload execution page 后等待 `CURRENT_BETS` 重推。超时未重推则 report 返回空/None 并把
  SE order/position liveness 标记 dead。
- `_submit_order` 已接 session gate 与 executor result → `generate_order_accepted/rejected`;
  `_place_via_executor` 从 cache instrument 翻译 `SharpExchLegacyOrder`,再走
  `SharpExchExecutor.place_order`;`_cancel_order` / `_cancel_one` 走 `SharpExchExecutor.cancel_order`;
  `_cancel_residual_one` 复用 `_cancel_one`,因此 cancel-only 检测到 SE 残留挂单时会真实撤
  residual order,而不是只 reject 新单。
  `_modify_order` 固定 reject。
- `generate_order_status_reports` / `generate_order_status_report` 已基于 `_current_bets`
  快照生成 `OrderStatusReport`;无法通过上述 reload-then-report 获得可信快照时标记
  SE order liveness dead 并返回空/None。
- `generate_position_status_reports` 已基于 `current_bets_to_positions(...)` 生成
  `PositionStatusReport`;无法获得可信快照时标记 SE position liveness dead 并返回空。
  真实 zero-order probe 已验证登录/API/WS 与 `BALANCE` / `CURRENT_BETS` 业务帧;runtime 余额事实以持续监听
  HTTP profile/balance response 为准,同值去重后写入 `AccountState`。
- `SharpExchMessageParser.parse_general_frame({"BALANCE": ...})` 输出
  `{"type": "balance", "balance": float|None, "av_balance": float|None}`。
- `SharpExchMessageParser.parse_general_frame({"CURRENT_BETS": ...})` 输出
  `{"type": "current_bets", "bets": list[dict]}`。
- 支持 payload 为 dict/list 或嵌套 JSON 字符串;SE runtime 按 USD 原生口径保留金额/size 字段,不做 FX 换算。
- `se_balance_to_account_balances(balance)` 已落地:在 SE 入站金额已归一成 USD 口径后,
  生成 `AccountBalance(total=free=balance USD, locked=0 USD)`,对齐 OE `BALANCE` 语义。
- Q17 accepted 本地预扣已落地:SE 与其它 tradable venue 共用 execution session helper;
  收到 `OrderAccepted` 后按 Venue Registry `odds_model=decimal` 预扣 `order.quantity`(USD stake),
  不请求 profile/balance。后续持续监听到的 HTTP profile/balance response 会覆盖本地估算。
- `normalize_current_bets_to_usd(bets, fx)` 保留为纯函数;SE runtime 调用时使用 `fx=1.0`,
  因真实 `CURRENT_BETS.currency=USD`。
- `current_bets_to_fills(bets)` 已落地:把非增量 `CURRENT_BETS` 快照转累计成交意图,join key 为 `offerId`,只在 `sizeMatched > 0` 且 `averagePrice > 0` 时产出。
- `bet_order_progress(bet)` 已落地:从单条 bet 派生 `accepted` / `partially_filled` / `filled` / `unknown`,并透出 `market_id` / `selection_id` / `side` / `original_qty` / `filled_qty` / `avg_px` / `price`。
- `current_bets_to_positions(bets)` 已落地:按 `(marketId, selectionId)` 聚合 matched 注单,BACK=LONG、LAY=SHORT,反向 matched 只抵减净额、不进入主方向均价;净额为 0 或 matched 为 0 时跳过。
- reload-then-report 与 launcher opt-in 已接线。真实登录 selector/post-login 行为已由
  zero-order probe 校准,SE execution `BALANCE` / `CURRENT_BETS` empty 快照与 startup reconciliation
  已由 SE skip node smoke 验证。
- SE 是 iframe app,登录后主页面 URL 可能仍停在 `sharpxch.com/player/`。因此 `se_login`
  以 customer iframe 出现作为优先完成信号,再 fallback 主 URL;post-login popup dismiss 的
  `timeout_ms` 是总预算(deadline 制),按 1s 分片轮询,每片用 `se_customer_context`
  重新解析当前 iframe/主页面(登录后 iframe 会 detach 重建,旧引用会失效),
  runtime 默认 10s,probe 场景可传短超时。
- `se_fetch_json` 保留为 page/frame context helper,供 probe 与 page-bound executor 场景使用;
  `se_fetch_json_with_browser_context` 是 discovery runtime 路径,直接使用共享
  BrowserContext request API 和 context cookies 中的 `CSRF-TOKEN`,不依赖 page/frame
  execution context。`sport/details` 被 Cloudflare/网络层挂起时由调用方 fail-fast,
  不允许 discovery/probe 静默卡住而误以为已经进入下单阶段。

当前也已落地 NT order → SE legacy order 纯映射:
- `nt_order_to_legacy_order(nt_order, inst)` 从 `BettingInstrument` 读取 `market_id` / `selection_id` / `selection_handicap`。
- NT `BUY` → SE `BACK`,NT `SELL` → SE `LAY`;`null_handicap` / NaN / None → `0.0`。
- 缺 `market_id` 或 `selection_id` 时返回 `None`。
- 为遵守“不改架构/逻辑/流程”,当前**不改共享** `src.arbitrage.common.order_models.Venue` enum,而是在 SE 子树内定义本地 `SharpExchLegacyOrder(venue="sharpexch")`。等 SE ExecutionClient 真接线时再决定是否推广到共享 order model。
- 不做出站 FX 换算;SE place payload 直接使用 USD stake。
- `se_order_to_place_bets_payload(order, fx)` 已落地:把 SE legacy order 转
  `/customer/api/placeBets` payload,直接使用 USD stake `order.size`;同时生成 `betUuid`、
  夹紧赔率到 `[1.01,1000]`。
  2026-07-01 手动 DevTools capture 校准:SE 前端真实 payload 使用
  `page="competition"`、`persistenceType="LAPSE"`、`showLayOddsEnabled=false`,
  `betUuid` 为 `{market}_{selection}_{handicap}__{timestamp}-{5位随机后缀}`;
  普通单不发送 `fillOrKill=false`,只在 FOK 时发送 `fillOrKill=true`。
  该函数不触发 Playwright/网络,真实 `SharpExchExecutor.place_order` 由
  `scripts/se_place_cancel_probe.py` 做最小 place+cancel live 验证入口。2026-07-01 已通过:
  BACK@100,size=12 → `venue_order_id=22157223` → `CURRENT_BETS` working → cancel 后消失,
  兜底活单数 0。
- `scripts/se_fill_probe.py` 是 Tier 2 真成交验证入口:默认 dry-run,显式 `--confirm`
  才会提交 BUY/BACK@1.01 最小 stake 市价单。脚本验证 `CURRENT_BETS.sizeMatched > 0`、
  `averagePrice > 0` 与 `_on_current_bets -> generate_order_filled` 路径;direct-client probe
  会在 `generate_order_accepted` spy 中把 `venue_order_id` 写入 cache,模拟 NT ExecEngine
  平时 apply accepted event 后建立的 `venue_order_id -> client_order_id` 映射。成交仓位不自动平仓,
  只会兜底撤未成交余量。2026-07-01 已通过:BACK@1.01,size=12 → offerId=22160783 →
  `CURRENT_BETS sizeMatched=12.0 averagePrice=4.5 sizeRemaining=0.0` →
  `generate_order_filled` 触发 1 次,兜底活单数 0。
- `parse_place_bets_response(response, market_id, bet_uuid)` 已落地:解析 OE/SE 同源响应格式,
  成功时从 `offerIds[bet_uuid]` 取 `venue_order_id`,缺精确 key 时 fallback 第一个 offer id,
  再缺则 fallback `bet_uuid`;全局/市场级错误返回失败 message。该函数不修改 NT 订单对象,
  后续 `SharpExchExecutionClient` 只消费结果并生成 NT event。
- `se_order_to_cancel_bets_payload(market_id, venue_order_id)` 已落地:生成
  `/customer/api/cancelBets` request body。2026-07-01 手动 DevTools capture 校准:SE
  前端撤单发送的是完整 open bet 对象;因此 ExecutionClient 若能从 CURRENT_BETS 找到该
  offer,会把完整 bet 透传给 executor,否则 fallback 到 `{offerId,betType}` 最小结构。
  缺 market 或 offer id 返回 `None`,留给后续 ExecutionClient 生成 cancel rejected。
- `parse_cancel_bets_response(response)` 已落地:解析 cancelBets 返回;无 error 的 dict 视为成功,
  空响应/非法响应/`error` 视为失败。
- `SharpExchExecutor` 已落地为 page-bound 薄封装:不创建浏览器、不登录、不持账户状态,
  只在调用方传入 page 后取 `se_customer_context(page)` 并在 customer iframe context 内调用
  `/customer/api/placeBets` / `/customer/api/cancelBets`,复用上述 payload/response 纯函数。
  CSRF token 统一由 Python 侧 `page.context().cookies()` 读取后注入 browser fetch,
  不依赖 iframe `document.cookie`;fetch 若返回非 JSON,会把 HTTP status 与短 raw sample
  包成失败响应,供 live probe/日志定位。
  `SharpExchExecutionClient._connect` 已负责 page 注入;`_place_via_executor` 已从 NT order
  翻译 `SharpExchLegacyOrder` 后传入 executor/page;`_cancel_one` 已用 instrument/current-bets
  解析 `market_id` 后调用 executor cancel。真实 SE 真单 place/cancel 通过
  `scripts/se_place_cancel_probe.py --confirm` 显式授权执行,默认 dry-run。

执行 liveness:
- SE ExecutionClient 写同一个 `VenueExecutionLiveness`。
- Risk 通过 expected legs 推导 required venues 后,PM/OE/SE 都按同一逻辑检查。

### 3.5 factory / launcher / runtime 接线

当前已落地 factory 离线构造与 launcher 显式 opt-in 注册:
- `ArbContext`:SE runtime 依赖只落在 `session_timeout_secs_by_venue["SHARPEXCH"]` /
  `discovery_config_by_venue["SHARPEXCH"]` / `sport_aliases_by_venue["SHARPEXCH"]` /
  `competition_aliases_by_venue["SHARPEXCH"]` / `instrument_provider_by_venue["SHARPEXCH"]` /
  `browser_manager_by_venue["SHARPEXCH"]` /
  `browser_login_state_by_venue["SHARPEXCH"]`。`browser_lock_by_venue["SHARPEXCH"]`
  仅作为旧 `se_login(..., browser_lock=...)` 兼容入口保留。
- `to_arb_context_init_kwargs`:始终注入 `session_timeout_secs_by_venue["SHARPEXCH"]`;仅当
  `venues.sharpexch.enabled=true` 时注入 `discovery_config_by_venue["SHARPEXCH"]`,否则不写入该 venue discovery config。
- `SharpExchLiveDataClientFactory`:复用/回写 `browser_manager_by_venue["SHARPEXCH"]`;若
  `discovery_config_by_venue["SHARPEXCH"]` 存在,构造带 browser `json_fetcher` 的 `SharpExchDiscoveryClient` +
  `SharpExchInstrumentProvider`,并按 `ctx.arbitrage_params.fx` 注入 Provider;否则使用
  fallback `InstrumentProvider()`。browser fetcher 不开页、不登录,只等待共享 browser context
  内的 `CSRF-TOKEN`,再用 context request 调 `sport/details`。创建后回写
  `instrument_provider_by_venue["SHARPEXCH"]`。
- `ArbSharpExchLiveExecClientFactory`:要求 `ctx.venue_liveness` 已准备,复用同一
  `browser_manager_by_venue["SHARPEXCH"]`,注入 `session_timeout_secs_by_venue["SHARPEXCH"]`、
  `ctx.pair_registry`、`ctx.pair_inflight` 与 `ctx.arbitrage_params.fx`。
- `build_trading_node_config` / `register_factories` / `prepare_runtime_state` 均按
  `venues.*.enabled` 注册 data/exec config、factories 与 `VenueExecutionLiveness` 初始
  venue 集合。launcher 校验至少两个 runtime venue 开启,且
  `data_sources.sports_status.enabled=true` 使 `PMSPORTS` sports anchor data source 注册。
  OE+SE-only 的 launcher/dispatcher 离线路径已可表达,不会再因 PM trading venue 关闭而
  fail-fast。MatchingActor 当前已使用 `PMSPORTS` anchor + enabled tradable venues 聚合匹配,
  不再依赖 PM tradable instrument 作为 pair anchor。
- SE skip node smoke 已验证 discovery instruments、routed prices、general first frame、
  balance/current bets 与 SHARPEXCH startup reconciliation;未下真单。

独立 probe(已落地,不接套利链路):
- `scripts/se_probe.py` 是 SE 网站事实探针,只做真登录、`sport/details` fetch、competition 页打开、
  prices/general WS 监听与脱敏样本输出。
- 该脚本不启动 TradingNode,不注册 Matching/Strategy/Risk,不调用 placeBets/cancelBets。
- 默认只打印摘要;只有显式传 `--write-dir` 才写 `se_probe_redacted.json`,用于后续补 parser fixture。
- 2026-07-01 已实跑通过:login ok,关闭登录后弹窗,`sport/details` 分页返回 242 个
  Tennis events,其中 `Men's Wimbledon 2026` 为 64 个;price frame 可解析。general 帧是否含
  balance/current_bets 取决于当次页面/账户状态,不能作为 zero-order probe 硬性成功条件。
- 运行该 probe 仍会打开真实登录浏览器,需要用户明确要求;真单 probe 另行授权。

已完成的 downstream 接线:
- `MatchingActor` 已从硬编码 PM/OE 改为 PMSPORTS event anchor + enabled
  `tradable_venues`;dispatcher 从 Venue Registry 派生 tradable venues,PM/OE/SE 可聚合到同一
  PMSPORTS anchor `MatchedPair`。
- `Strategy` 已完成第一阶段 SE 接线:`mean_rebate` / `one_side_rebate` /
  `mean_rebate_recovery` / `place_bets` / `share_limit` 均把 `SHARPEXCH` 作为 OE 类
  decimal odds venue 处理。该接线不引入第二阶段可插拔 odds-model 抽象。
- `Risk` 已完成第一阶段 SE 接线:`expected_legs` 中 `se:*` / `sharpexch:*`
  会推导 required venue `SHARPEXCH`;SE `BettingInstrument` 走 decimal odds 概率门控;
  SE 余额按 adapter 入站后的 USD free 检查;`ArbitragePortfolio` 保留 SE venue
  identity,不会把 SE 持仓归到 OE。

### 3.6 Login 串行化与 discovery CSRF 等待

**历史问题**:NT 启动时并发连接 Data/Exec clients。旧 discovery 会创建 `se-discovery` 页并调用 `se_login`,Execution 也创建 `execution` 页并登录,两者并发登录会触发 Cloudflare 验证。仅用裸 lock 串行化还不够:第二个 page 可能在第一个 page 登录前已经打开旧登录页,拿到锁后仍会基于旧 DOM 二次提交表单。

**当前方案**:Discovery 不再创建页面、不登录、不导航;只等待共享 BrowserContext 中已有 `CSRF-TOKEN`,最多等 `config.page_timeout`,拿到后用 context request 调 `sport/details`。Execution 仍是唯一会提交账号凭据的路径,并在 `se_login` 中使用 browser context 级 `SharpExchLoginState`:第一个 execution page 成功登录后标记 context 已认证;后续 execution 登录先等待 customer app/session 复用,不直接提交旧登录页。

#### 3.6.1 原理

Browser context 内 cookies/session 是共享的:
1. Execution page 登录成功 → session cookies 与 `CSRF-TOKEN` 写入 context,`SharpExchLoginState.authenticated=True`
2. Discovery `_se_browser_json_fetcher` → 不拿锁,只读 context cookies/request,不调用 `se_login`(持锁等 CSRF 会与步骤 1/3 的登录互相饿死:CSRF 只有登录才会产生)
3. 其它 execution page 调用 `se_login` → 拿同一 lock;若 context 已认证,先等待 customer app/session 复用并跳过表单
4. 若 session 过期或 customer app 不可用,execution 再退回普通表单登录路径

#### 3.6.2 时序

```
NT 并发 connect data/exec clients
    │
    ├─ [Discovery] _se_browser_json_fetcher
    │     ├─ browser_manager.start()
    │     ├─ 等 shared BrowserContext 内 CSRF-TOKEN ← 无锁轮询(cookie 只读)
    │     └─ context.request.post(sport/details)
    │
    └─ [Execution] _connect
          ├─ create_page("execution")
          ├─ 设置 WS handler + response listener
          ├─ se_login(page, config, login_state) ← 等 context 登录锁
          │     └─ 拿到锁 → authenticated=True → 等 customer app → 跳过表单
          └─ 继续 WS 监听
```

#### 3.6.3 页面模型

| 页面 | 用途 | 创建者 |
|---|---|---|
| `execution` | Execution orders/balance/WS | ExecutionClient |
| `comp-{sport}_{competition}` | OrderBook 订阅 | DataClient |

Discovery 不拥有 page;各 page-bound client 保持独立 page。串行化只发生在 execution 登录之间;discovery 的 CSRF 读取无锁。

#### 3.6.4 代码落点

- `web.py`:
  - `SharpExchLoginState`:browser context 级登录锁 + authenticated 标记
  - `se_login(page, config, browser_lock=None, login_state=None)`:优先使用 `login_state` 串行化登录并复用已认证 context;`browser_lock` 仅保留兼容旧调用

- `factories.py`:
  - `_shared_se_login_state(ctx)`:从 `ArbContext.browser_login_state_by_venue["SHARPEXCH"]` 取/建共享状态
  - `_se_browser_json_fetcher()`:等待共享 BrowserContext 的 `CSRF-TOKEN`,用 context request 调 `sport/details`

- `execution.py`:
  - `_connect()`:调用 `se_login(self._page, self._config, login_state=...)`

#### 3.6.5 Session 复用检测

`se_login` 已有 session 复用逻辑:导航后检查登录表单是否存在,无表单则视为已登录。
新增 context authenticated 快路径后,第二个 page 即使仍持有旧登录页 DOM,也会先等待 customer app/session 复用。
第二个 page 登录时,因 context 已有 session cookies,SE 服务端可能:
- 直接跳转到 customer 页面(不显示登录表单)
- 或显示已登录状态

无论哪种情况,`se_login` 都能正确处理

---

## 4. 插拔化观察点

第一阶段写代码时必须记录以下差异,但不要先抽象:

| 观察点 | OE | SE | 第二阶段可能抽象 |
|---|---|---|---|
| portal base URL | `www.orbitexch.com` | `portal.sharpxch.com` | `BrowserExchangeEndpoints` |
| login URL | OE 首页/客户页 | `sharpxch.com/player/` 外层页 | `login_entry_url` |
| discovery 主路径 | DOM scraper | API `sport/details` 优先 | `ExchangeDiscoveryBackend` |
| price WS endpoint | `multiple-market-prices` | 同名 | 可共用 handler skeleton |
| general WS endpoint | `general` | 同名 | 可共用 SockJS envelope parser |
| price frame schema | OE parser 已知 | zero-order probe 已验证 BIAB/OE 型 frame 可解析 | `PriceFrameParser` protocol |
| current bets schema | OE 已有实盘字段 | zero-order、真单 place+cancel 与真成交 fill probe 已验证 empty/working/matched 快照 | `CurrentBetsParser` protocol |
| stake currency | OE 已按 USD 边界转换 | SE `CURRENT_BETS` / place payload 当前均按 USD 原生处理;账户余额走 HTTP response | `money_normalizer` |
| min stake | OE 7 GBP | 12 venue stake | per-venue `min_stake` config |
| page visibility | OE 需要 visibility spoof | SE 待观察,先复用 | shared browser init script |

第二阶段抽象候选:

```python
class BrowserExchangeSpec(Protocol):
    venue: str
    base_url: str
    login_url: str
    prices_ws_path: str
    general_ws_path: str
    account_currency: str
    min_stake: Decimal
```

但只有当 OE/SE 两边都跑通 discovery、prices WS、general WS、submit/cancel 后才抽。

---

## 5. 验收计划

第一阶段按离线到 live 递进:

1. 配置加载:JSON + env 注入 SharpExch 凭证,不把凭证写入 JSON。
2. Provider 单测:API fixture → `BettingInstrument` legs,InstrumentId 可逆,Q9 info 完整。
3. Data 映射单测:SE price frame fixture → `OrderBookDeltas`。
4. Factory 接线单测:TradingNode config 包含 SE data/exec client,factory 使用
   `ArbContext` keyed map(`session_timeout_secs_by_venue` / `discovery_config_by_venue` /
   `browser_manager_by_venue` / `browser_login_state_by_venue`)。
5. Matching 单测:PM + SE 同赛事可产 `MatchedPair`,PairRegistry 注册三方 instrument。
6. Strategy 单测:SE 作为 OE 类 decimal odds venue 参与机会筛选、candidate、recovery、size 与 share limit。
7. Risk 单测:metadata required venues 含 SE 时,venue liveness fail-closed/pass-open。
8. 独立 zero-order probe:SE 真登录、真 `sport/details`、真 prices/general WS、脱敏样本,但不启动套利 node、不下真单。
9. Skip live smoke:SE 真登录、真 discovery、真 prices WS、真 balance/current bets,但不下真单。
10. 真单 live probe:仅用户明确授权后,先做 12 stake 小额不成交 place+cancel,再做
    `scripts/se_fill_probe.py --confirm --size 12` 成交路径。
11. 完整套利端到端 E2E:放到 venue 插拔化第二阶段完成后再测;第一阶段不再用硬编码链路推进真钱套利 E2E。

---

## 6. 当前剩余工作

| 问题 | 处理 |
|---|---|
| SE 账户真实币种 | 已按实测 `CURRENT_BETS` / place payload USD 原生处理;runtime 余额以 HTTP profile/balance response 为准,WS `BALANCE` 不作为账户事实 |
| SE 最小 stake | 已由 2026-07-01 下单 preflight 确认为 12 USD |
| SE price WS 真实业务帧 | 已由 zero-order probe 与 skip node smoke 验证可解析并发布 `OrderBookDeltas` |
| SE current bets schema | 已验证 empty/working/matched 快照;matched 字段 live 样本:`sizeMatched=12.0`,`averagePrice=4.5`,`sizeRemaining=0.0` |
| 是否能完全 API discovery | 当前 sport details API 已足够 tennis/soccer match odds;其它运动后续再验证 |
| 完整套利真钱 E2E | 等 PM+OE / PM+SE / OE+SE / PM+OE+SE skip smoke 组合验收后,再由用户单独授权执行 |
