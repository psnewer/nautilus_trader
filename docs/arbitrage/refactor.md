# 套利系统重构 - 全面迁移到 NautilusTrader 架构

> **状态**: 设计中(逐组件推进,**不超前设计**)
> **目标**: 将 `src/arbitrage/services/*` 全部迁移到 NT 原生抽象(`Actor` / `Strategy` / `LiveMarketDataClient` / `LiveExecutionClient` / `InstrumentProvider`),最终运行在单一 `TradingNode` 容器内。
> **节奏**: 一次只敲定一个组件的详细设计,后续组件只保留高层映射,等轮到它时再展开。

---

## 1. 背景与动机

当前 `src/arbitrage/services/` 是一套**自研的微服务外壳**(DiscoveryService / MarketMatchingService / OddsSubscriptionService / ExecutionService / RiskService / WebGatewayService),通过 in-process Python 类直接互相引用。adapter 层是手写的客户端,不是 NT 的 `LiveMarketDataClient` / `LiveExecutionClient` 子类。

**问题**:

1. NT 已经为相同问题提供了原语(`InstrumentProvider`、`Cache`、`MessageBus`、`DataEngine` 引用计数订阅、`Strategy` 订单追踪、`RiskEngine` 下单前检查、`Clock` 定时器),自研外壳是重复造轮子且语义不对齐。
2. backtest / paper trading / multi-strategy 等扩展能力被锁死。
3. 适配器没有进 NT 的 Factory 体系,无法被其它 NT-原生组件复用。

**目标**: 端态下整个套利进程是**一个 `TradingNode`**,所有领域代码以 `Actor` / `Strategy` / `LiveMarketDataClient` / `LiveExecutionClient` 形态存在。

---

## 2. 设计原则

| # | 原则 | 含义 |
|---|---|---|
| P1 | 优先使用 NT 原语 | 凡是 NT 已经有的抽象必须直接用,不重新发明 |
| P2 | 领域 IP 才保留自研 | 仅"NT 没有也不该有的"逻辑保留自研(如跨场馆事件匹配 `EventNormalizer` / `MatchEngine`) |
| P3 | 接口契约抄 Betfair | NT 体育博彩抽象由 Betfair 适配器定义,照抄契约不抄 IO |
| P4 | IO 层完全自由 | PM 用 websockets、OE 用 Playwright CDP —— 这些都是 `client.py` 内部细节 |
| P5 | 渐进迁移 | 每一步独立验证,不破坏现有实盘运行路径直到对应模块完全切换 |
| P6 | 不为 backtest 做超前抽象 | 当前只跑 live。Arrow schema / 回放序列化等留第二阶段 |
| **P7** | **一次设计一个组件** | **不超前设计**。轮到 Step N 时才展开 Step N 的细节,避免在没充分讨论时锁死决定 |
| **P8** | **按功能模块组织代码,不按 NT 原语类型分组** | 应用代码放 `src/arbitrage/<capability>/`,内部既有 Actor 也有领域算法,不要 `actors/` `strategies/` `scheduling/` 这种把单个 Actor 单独成目录的横切组织。NT 自己的 `Controller`(`trading/`)、`OrderEmulator`(`execution/`)就是这个原则 |
| **P9** | **`src/` 与 `nautilus_trader/` 的边界** | `nautilus_trader/` 是 NT 上游 fork,会定期 merge upstream;`src/arbitrage/` 是你的应用,upstream 永远不动。**唯一例外**: venue 适配器(`adapters/polymarket/`、`adapters/orbitexch/`)放在 NT 树里,因为 NT 对 adapter 路径有约定,且它们是独立子树不会冲突 |
| **P10** | **Debug 注入通过子类化,生产代码零 `if self._debug`** | 所有可被 debug 改写的行为(数据流 / 客户端选择 / Strategy 决策参数 / Risk 校验)统一用"生产类干净 + Debug 子类覆盖 hook + 工厂层选择"。架构对称,关注点分离,测试隔离。详见 §6.6 |

---

## 3. 端态架构(高层视图)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        TradingNode (单进程,单 event loop)                │
│                                                                          │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │                          MessageBus                              │   │
│   └────────────────────────────────────────────────────────────────┘   │
│      ↑                ↑                ↑                ↑                │
│   ┌─────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────────────┐  │
│   │  Cache  │   │ DataEngine  │   │ ExecEngine   │   │  RiskEngine  │  │
│   │         │   │  ┌────────┐ │   │  ┌────────┐  │   │              │  │
│   │ • Inst  │   │  │PM Data │ │   │  │PM Exec │  │   │  (NT 自带)   │  │
│   │ • Book  │   │  └────────┘ │   │  └────────┘  │   │              │  │
│   │         │   │  ┌────────┐ │   │  ┌────────┐  │   │              │  │
│   │         │   │  │OE Data │ │   │  │OE Exec │  │   │              │  │
│   │         │   │  └────────┘ │   │  └────────┘  │   │              │  │
│   └─────────┘   └─────────────┘   └──────────────┘   └──────────────┘  │
│                                                                          │
│   ┌────────────────────────── Trader ──────────────────────────────┐   │
│   │  Actors                              Strategies                 │   │
│   │  • InstrumentRefresher (×venue)      • ArbitrageStrategy        │   │
│   │  • MarketMatchingActor                                          │   │
│   │  • WebGatewayActor (订阅 AccountState 推前端)                    │   │
│   └────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 组件映射总表(高层映射 —— 详细设计在轮到它时展开)

> **🔑 重大发现(2026-05-09)**: 上游 NT 已经有完整 Polymarket 适配器(`nautilus_trader/adapters/polymarket/{data,execution,providers,factories}.py` + `common/`、`http/`、`websocket/` 子目录)。这意味着 **PM 部分迁移工作量大幅下降**:Step 1 / 2 / 5 的 PM 部分**直接用上游,零代码**。
>
> - 上游 PM 用 **`BinaryOption`** 类型(NT 二元期权 instrument),不是我之前推测的 `BettingInstrument`
> - 上游 PM `InstrumentId` 命名已锁定为 `{condition_id}-{token_id}.POLYMARKET`,有现成的 `get_polymarket_instrument_id()` 等工具函数
> - 用户当前的 `adapters/polymarket/odds_client.py` 和 `executor.py` 是**与上游同职责的两份代码**,迁移后整体删除
> - OE 没有上游适配器,**仍需自写全套**
>
> 因此:**两家 venue 用异构 instrument 类型**(PM `BinaryOption` + OE `BettingInstrument`),`MarketMatchingActor` 必须做语义归一(见 §6.4 / Q9)。

| 当前组件 | 目标位置 | 处理方式 | 详细设计章节 |
|---|---|---|---|
| `services/market_discovery/`(PM scraper) | 上游 `adapters/polymarket/providers.py` | **直接用上游,删除自研** | **§5.1**(已展开) |
| `services/market_discovery/`(OE scraper) | 自写 `adapters/orbitexch/providers.py` | 自写,scraper 类搬入 provider | **§5.1**(已展开) |
| `services/market_discovery/_poll_loop`(调度) | `src/arbitrage/discovery/refresher.py`(`InstrumentRefresher(Actor)`) | 替换为独立 Actor;`refresh_interval` 走 NT 自带持久化(§6.3) | §5.2(待 Step 2) |
| `adapters/polymarket/odds_client.py`(自研) | 上游 `adapters/polymarket/data.py` | **直接用上游,删除自研**;输出 NT 标准 `OrderBookDelta`(取代 dict) | §5.2(待 Step 2) |
| `adapters/polymarket/executor.py`(自研) | 上游 `adapters/polymarket/execution.py` | **直接用上游,删除自研**;天然修掉 `order_version_mismatch` bug;含 expiration / neg_risk / WS USER channel 事件回写 | §5.5(待 Step 5) |
| `adapters/orbitexch/odds_client.py`(自研) | 自写 `adapters/orbitexch/data.py`(`OrbitExchDataClient(LiveMarketDataClient)`) | 包装现有 IO 层为 NT 契约 | §5.2(待 Step 2) |
| `adapters/orbitexch/executor.py`(自研) | 自写 `adapters/orbitexch/execution.py`(`OrbitExchExecutionClient(LiveExecutionClient)`) | 包装现有 IO 层为 NT 契约 | §5.5(待 Step 5) |
| `services/odds_subscription/` | (无对应物) | **整目录删除**,订阅由 DataEngine 引用计数自动完成 | §5.2(待 Step 2) |
| `services/market_matching/` | `src/arbitrage/matching/`(`engine.py`+`normalizer.py`+`actor.py`+`data_types.py`) | 算法保留,外壳替换为 NT `Actor`;不依赖 instrument 具体类型(§6.4) | §5.3(待 Step 3) |
| `services/execution/`(套利决策算法) | `src/arbitrage/strategy/`(`ArbitrageStrategy(Strategy)`) | 仅保留套利决策逻辑(planner 类) | §5.4(待 Step 4) |
| `services/execution/`(其余: tracker / orchestrator / service / session 等) | (无对应物) | **整体删除**;NT `Strategy` + `ExecutionEngine` + `MessageBus` 替代订单追踪、事件分发、生命周期 | §5.4(待 Step 4) |
| `services/risk/` | `src/arbitrage/risk/engine.py`(`ArbitrageRiskEngine` NT 子类) | NT `RiskEngine` 标准管道透明拦截做余额检查;**账户状态维护归 ExecutionClient**(PM 主动 / OE 被动 WS,写 NT Cache);**告警让前端自己看**(WebGatewayActor 订阅 AccountState 事件转 JSON 推浏览器,无独立告警 Actor);Strategy 不引用 Risk | §5.6(待 Step 6) |
| `services/strategy/service.py` 中的 `_check_and_adjust_size` Step 1(深度缩放) | `ArbitrageStrategy._adjust_share_by_liquidity` hook | Strategy 内部职责;读 NT `cache.order_book(...)` | §5.4(Step 4) |
| `services/strategy/service.py` 中 `_check_and_adjust_size` Step 2(最小限额门控)+ `MIN_SIZE_POLYMARKET/_ORBITEXCH` 常量 + `check_min_size` 函数 | (无对应物) | **整体删除**,完全用 NT 自动检查 `instrument.min_quantity`(应用层不更严) | §5.6 |
| `services/web_gateway/` | `src/arbitrage/web/`(`actor.py`+ FastAPI 路由) | 外壳替换为 Actor + FastAPI 协程同 loop;同时承担"行情格式适配"(订阅 NT `OrderBookDelta` 转 JSON 推前端) | §5.7(待 Step 7) |

> **目录组织原则(P8)**: 应用代码按**功能模块(capability)**组织(`discovery/`、`matching/`、`strategy/`、`risk/`、`web/`),内部既有 NT Actor/Strategy 接线代码,也有领域算法和 Data 类型。不使用 `actors/` `strategies/` `scheduling/` 这种按 NT 原语类型横切的目录,也不为单个 Actor 单独建目录。NT 自己也是这样组织的: `Controller(Actor)` 在 `trading/`,`OrderEmulator(Actor)` 在 `execution/`。
>
> 端态 `src/arbitrage/` 形态:
> ```
> src/arbitrage/
> ├── discovery/        # InstrumentRefresher Actor + 配套
> ├── matching/         # MatchEngine + EventNormalizer + Actor + Data 类型
> ├── strategy/         # ArbitrageStrategy
> ├── risk/             # ArbitrageRiskEngine (NT RiskEngine 子类) — 余额检查归这里;无 BalanceMonitorActor
> ├── web/              # WebGatewayActor + FastAPI 路由(订阅 AccountState 推前端)
> ├── common/           # 共享 utils / 配置 / 类型
> ├── testing/          # 已存在
> └── debug/            # 已存在
> ```

---

## 5. 各组件详细设计

> 当前只展开 **Step 1**(已敲定方向)。Step 2/3 已讨论过触发逻辑等关键问题,留摘要;Step 4-7 仅占位,**等到该步开始前再补完**。

### 5.1 Step 1: InstrumentProvider(替代 market_discovery) **【当前讨论中】**

**两家 venue 走不同路径**: PM 直接用上游,OE 自写。

#### 5.1.1 PM: 用上游 `PolymarketInstrumentProvider`(零代码)

上游 `nautilus_trader/adapters/polymarket/providers.py:line 462+` 已经实现完整 Provider:
- 数据源: Gamma API(`list_markets()` + `normalize_gamma_market_to_clob_format()`)+ CLOB API(`get_markets()` 分页游标)
- 输出: `BinaryOption` instrument(NT 二元期权类型),不是 `BettingInstrument`
- 字段富化: 从 Gamma API 补 `feeSchedule`,计算 `taker_fee`
- InstrumentId: 由 `nautilus_trader/adapters/polymarket/common/symbol.py` 的 `get_polymarket_instrument_id(condition_id, token_id)` 生成

**Step 1 PM 工作量**: 零代码。配置 `PolymarketInstrumentProviderConfig` 即可。删除 `services/market_discovery/` 中 PM scraper 部分。

#### 5.1.2 OE: 自写 `OrbitExchInstrumentProvider`

```python
# nautilus_trader/adapters/orbitexch/providers.py
class OrbitExchInstrumentProvider(InstrumentProvider):
    """通过 Playwright 抓取 OE 赛事列表页,生成 BettingInstrument 注入 Cache。"""
    def __init__(self, browser, config):
        super().__init__(config=config)
        self._browser = browser  # 与 OrbitExchDataClient 共享(见 Q2)

    async def load_all_async(self, filters: dict | None = None) -> None:
        page = await self._browser.new_page()
        events = await scrape_event_listing(page, sports=self._config.sports)
        for event in events:
            for market in event.markets:
                for selection in market.selections:
                    instrument = self._build_betting_instrument(event, market, selection)
                    # 把 venue 原始字段 + 事件元数据塞 info dict (供 MatchingActor 归一)
                    self.add(instrument)
```

InstrumentId 由配套 helper 生成(模仿 PM 的目录结构):
```python
# nautilus_trader/adapters/orbitexch/common/symbol.py
def get_orbitexch_instrument_id(market_id: str, selection_id: str) -> InstrumentId:
    return InstrumentId.from_str(f"{market_id}-{selection_id}.{ORBITEXCH_VENUE}")
```

#### 5.1.3 InstrumentId 命名规则(Q1 已锁定)

| Venue | 格式 | 来源 |
|---|---|---|
| PM | `{condition_id}-{token_id}.POLYMARKET` | 上游已定,直接复用 `get_polymarket_instrument_id()` |
| OE | `{market_id}-{selection_id}.ORBITEXCH` | 仿 PM 风格,自定义 helper |

**周期刷新**: 由独立的 **`InstrumentRefresher` Actor** 拥有(见 §5.2),**不是 Provider 自己,不是 DataClient,不是 MatchingActor**。Provider 是被动的。

**Step 1 验收标准**:
- 启动 minimal TradingNode + 两个 provider
- `node.cache.instruments(venue=POLYMARKET)` 返回非空 `BinaryOption` 列表
- `node.cache.instruments(venue=ORBITEXCH)` 返回非空 `BettingInstrument` 列表
- OE instrument 的 `info` dict 含跨场馆匹配所需的通用字段(运动类型 / 队伍名 / 开赛时间)—— 验收 §6.4 归一可行
- **现有 `services/market_discovery/` 暂不删除**,新老并行,验证一周后再删

**Step 1 阻塞项**:
- ~~Q1: InstrumentId 命名规则~~ ✅ 已锁定
- ~~Q2: OE Playwright Browser 共享方式~~ ✅ 已锁定
- Q9 (新): MatchingActor 跨类型语义归一(影响 OE Provider 的 `info` dict 字段设计) ✅ 已锁定方案 A,但 Step 1 OE Provider 实现时需把 §6.4 表格中的 6 个统一 key 都填好

---

### 5.2 Step 2: DataClient + InstrumentRefresher(替代 odds_subscription) **【概要,待 Step 2 启动时展开】**

Step 2 实际是**两个独立组件**: **DataClient**(纯 IO)+ **InstrumentRefresher**(调度 + config 持久化)。两者放同一 Step,因为 Refresher 触发 `provider.load_all_async()` 后,instrument 入 Cache,下游链路要联通到 DataClient 的订阅去重。

#### 5.2.1 DataClient(纯 IO,两家 venue 走不同路径)

**PM**: 用上游 `nautilus_trader/adapters/polymarket/data.py:PolymarketDataClient`,**零代码**。
- WebSocket URL `wss://ws-subscriptions-clob.polymarket.com/ws/market` 与用户 `odds_client.py` 一致
- 上游用 NT 框架集成的 `WebSocketClient`,提供配置化重连 + 指数退避(优于用户硬编码 5s)
- 数据出口: NT 标准 `OrderBookDelta` / `QuoteTick` / `TradeTick`(取代用户自定义 dict)
- 订阅去重: 上游用引用计数(`websocket/client.py:207-214`)
- 删除用户 `adapters/polymarket/odds_client.py`

**OE**: 自写 `OrbitExchDataClient(LiveMarketDataClient)`,实现 `_subscribe_order_book_deltas` 等钩子。
- 内部 IO(Playwright CDP 抓 WS 帧)从用户 `odds_client.py` 平移
- 出口改为 `self._handle_data(OrderBookDelta(...))` 推 NT MessageBus
- WS 重连机制原样保留在 `client.py`

**共同约束**:
- 行情统一用 `OrderBookDelta`,不用 `QuoteTick`
- 订阅去重由 `DataEngine` 引用计数自动完成
- DataClient 不拥有 instrument 刷新调度(职责归 `InstrumentRefresher`),也不拥有 `refresh_interval` 配置

**"改下游"的工作分配**:
- `services/odds_subscription/` 转发用户 dict 的逻辑: 整目录删除(订阅由 DataEngine 自动去重)
- `services/market_matching/` 不消费 odds dict,只读 instrument 元数据 → **不受影响**
- `services/execution/orchestrator.py` 读 dict 算价差: 在 Step 4 重写为 `ArbitrageStrategy`,顺便切到读 NT `cache.order_book(...)`
- `web_gateway/` 转 dict 推前端: 在 Step 7 重写为 `WebGatewayActor`,订阅 NT MessageBus 上的 `OrderBookDelta` 转 JSON
- 因此 Step 2 阶段下游用 feature flag 切换新老路径并行跑,真正"改下游"的代码工作发生在 Step 4 / 7

#### 5.2.2 InstrumentRefresher Actor(调度 + config 持久化)

替代 `services/market_discovery/_poll_loop`。每个 venue 一个实例。

```python
# src/arbitrage/discovery/refresher.py
class InstrumentRefresher(Actor):
    """周期触发 InstrumentProvider.load_all_async,完成后发批次完成事件。"""

    def __init__(self, config: InstrumentRefresherConfig, provider: InstrumentProvider):
        super().__init__(config)
        self._provider = provider
        self._refresh_interval: int = config.refresh_interval_default

    cpdef dict[str, bytes] on_save(self):
        return {b"refresh_interval": str(self._refresh_interval).encode()}

    cpdef void on_load(self, dict[str, bytes] state):
        if b"refresh_interval" in state:
            self._refresh_interval = int(state[b"refresh_interval"])

    def on_start(self):
        self._task = self.create_task(self._run())
        self.msgbus.subscribe(
            f"config.{self._config.venue}.refresh_interval",
            self._on_set_interval,
        )

    async def _run(self):
        while self._is_running:
            await asyncio.sleep(self._refresh_interval)  # 每次循环读最新值
            try:
                await self._provider.load_all_async()
                self.publish_data(
                    DataType(InstrumentsRefreshed),
                    InstrumentsRefreshed(
                        venue=self._config.venue,
                        count=len(self._provider.list_all()),
                        ts_init=self.clock.timestamp_ns(),
                    ),
                )
            except Exception as e:
                self._log.error(f"Refresh failed: {e}")
                # 不发成功事件 → matching 自然 gate 住

    def _on_set_interval(self, new_interval: int):
        self._refresh_interval = max(MIN_INTERVAL, int(new_interval))
        # 持久化由 NT 在 stop 时通过 on_save 自动写入
```

**关键属性**:
- 一个 venue 一个 Refresher 实例(`venue=POLYMARKET` / `venue=ORBITEXCH`)
- `refresh_interval` 通过 NT `on_save / on_load` 自动持久化(走 `CacheDatabaseAdapter` → Redis,详见 §6.3)
- 面板修改通过 MessageBus 命令 `config.{venue}.refresh_interval` 送达,运行时即时生效
- 完成事件 `InstrumentsRefreshed` 推 MessageBus,MatchingActor 订阅(§5.3)
- 单次 refresh 失败**不发**事件,matching 因此自然 gate 住

**Step 2 启动时还需展开**:
- `client.py` 拆分粒度(WS / HTTP / 解析层划分)
- `factories.py` 注册 `DataClientFactory`
- `InstrumentsRefreshed` Data 类型字段细节(`@customdataclass` 注册)
- `MIN_INTERVAL` 数值(防止误设置 0 把场馆刷挂)
- 启动顺序: Refresher 第一次 refresh 是同步等还是异步等;MatchingActor 启动期间无任何 InstrumentsRefreshed 事件时如何展示
- ProbeActor 验证脚本
- 与现有 `odds_subscription` / `market_discovery` 的并行/切换策略

---

### 5.3 Step 3: MarketMatchingActor(替代 market_matching) **【概要,待 Step 3 启动时展开】**

**已敲定的方向**:

- 算法层(`MatchEngine` / `EventNormalizer` / `MatchedPair`)从 `services/market_matching/` **平移**到 `src/arbitrage/matching/`,代码不改
- 外壳替换为 `MarketMatchingActor(Actor)`,放在 `src/arbitrage/matching/actor.py`
- **触发逻辑**: 订阅 `InstrumentRefresher` 发布的批次完成事件,**两家 venue 都有近期成功 refresh 才触发匹配**:
  ```python
  def on_start(self):
      self.subscribe_data(DataType(InstrumentsRefreshed))
      self._last_success: dict[Venue, int] = {}  # venue → ts_init (ns)

  def on_data(self, data):
      if isinstance(data, InstrumentsRefreshed):
          self._last_success[data.venue] = data.ts_init
          if self._all_venues_recent():
              self._do_match()

  def _all_venues_recent(self) -> bool:
      now = self.clock.timestamp_ns()
      window_ns = self._config.fresh_window_ns  # 倾向 2 * refresh_interval
      return all(
          v in self._last_success and (now - self._last_success[v]) < window_ns
          for v in (POLYMARKET, ORBITEXCH)
      )
  ```
- 性质:
  - ✅ 两家 venue 都完成才匹配(避免单家先完成时用对方旧数据)
  - ✅ 单 venue 失败不发 `InstrumentsRefreshed`,自然 gate 住 matching
  - ✅ "近期"窗口排除过期数据(一家长时间挂掉,另一家也不会触发)
- `MatchedPair` 注册为 NT `Data` 子类(`@customdataclass`),通过 `publish_data` 推 MessageBus

**Step 3 启动时还需展开**:
- `MatchedPair` / `MatchedPairRemoved` 字段细节
- `fresh_window_ns` 取值(Q5,倾向 `2 × refresh_interval`)
- 启动时全量匹配 vs 增量匹配
- web_gateway 查询入口设计(MessageBus request/response vs 直接持有 Actor 引用)

**Q9 (新) 处理: 异构 instrument 类型的语义归一**

PM 用 `BinaryOption`,OE 用 `BettingInstrument`,字段不直接对齐。MatchingActor 不能依赖具体类型。**方案 A**(已锁定): Actor 内部做语义归一,通过 `instrument.info` dict 中的通用字段(`event_id` / 队伍名 / 开赛时间 / 运动类型)做匹配。

实现要点:
- 两家 venue 的 `InstrumentProvider` 加载时,将原始字段塞进 `instrument.info`,使用统一的 key 命名(如 `info["sport"]`、`info["home_team"]`、`info["away_team"]`、`info["start_ts"]`)
- `MatchEngine` 读 `info` dict 做匹配,不 isinstance 检查类型
- 这样保证 Step 3 的算法对 PM/OE 完全对称,不为新增 venue 改 MatchingActor

详情见 §6.4。

---

### 5.4 Step 4: ArbitrageStrategy(替代 execution) **【概要,待 Step 4 启动时展开】**

`ArbitrageStrategy(Strategy)` 订阅 `MatchedPair`,对每个匹配对调 `subscribe_order_book_deltas(instrument_id)` 两次,在 `on_order_book_delta` 中跑套利决策,通过 `self.submit_order(...)` 下单(由 NT `ExecutionEngine` 路由到对应 venue 的 ExecutionClient)。订单追踪走 NT `Strategy` 内置回调(`on_order_submitted` / `on_order_filled` / `on_order_rejected` 等),**不再自研 tracker / orchestrator / service**。

**关键边界(SoC,2026-05-09 修正锁定)**:

| 行为 | 归属 | 触发 |
|---|---|---|
| **深度缩放**(根据流动性算应该下多少 share) | **Strategy 内部职责** | `_adjust_share_by_liquidity` hook,Strategy 算订单参数时自己处理 |
| **最小限额检查**(`size ≥ instrument.min_quantity`) | **NT RiskEngine 自动** | NT 自带,无需扩展(应用层不更严,删除 `MIN_SIZE_*` 常量) |
| **余额检查**(够不够下单) | **`ArbitrageRiskEngine`(NT RiskEngine 子类)** | NT 自动拦截 `submit_order` 管道 |

**Strategy 不引用 Risk**。直接 `submit_order()` → NT `ExecutionEngine` 自动经过 `RiskEngine` 拦截 → 通过则路由到 `ExecutionClient`,被拒发 `OrderDenied` 事件。

```python
class ArbitrageStrategy(Strategy):
    def _evaluate_and_submit(self, ctx):
        best = self._evaluate(ctx)
        if not best: return

        # 深度缩放(Strategy 自己做,不是风控)
        adjusted = self._adjust_share_by_liquidity(ctx, best)
        if adjusted is None: return  # 流动性太差,不值得套利(非风控拒)

        pm_order = self._planner.build_pm_order(adjusted, best, ...)
        oe_order = self._planner.build_oe_order(adjusted, best, ...)

        # 直接 submit。NT RiskEngine 透明拦截,做最小限额 + 余额检查。
        self.submit_order(pm_order)
        self.submit_order(oe_order)

    def on_order_denied(self, event):
        """RiskEngine 拒绝(典型: 余额不够、最小限额不满足)。可能要补偿撤另一腿。"""
        ...

    def on_order_rejected(self, event):
        """venue 拒绝(典型: cache stale 时 venue 真实余额不够)。NT 标准异常路径。"""
        ...

    # ─── Hook 点(Debug 子类可覆盖)───
    def _adjust_share_by_liquidity(self, ctx, best) -> Decimal | None: ...
```

**关键性: 这一步是接入上游 PM ExecutionClient(Step 5)的前提** —— 上游 ExecutionClient 只接受 NT `ExecutionEngine` 投递的 `SubmitOrder` 命令,只有继承 NT `Strategy` 的 `ArbitrageStrategy.submit_order(...)` 能触发。所以即使 Step 5 PM 部分零代码,**Step 4 不做的话 Step 5 上游 ExecutionClient 没人调用**。

**Hook 点设计(P10 关键约束)**: 生产 Strategy 必须把可变行为拆成命名清晰的 **protected hook 方法**,以支持 §6.6 Debug 注入框架的子类化覆盖:

```python
def _get_min_rebate_rate(self) -> Decimal: ...           # config 默认值,Debug 覆盖
def _get_pm_price(self, ctx, direction) -> Price: ...    # 真实最优价,Debug 覆盖
def _get_oe_price(self, ctx, direction) -> Price: ...
def _get_pm_size(self, share, direction) -> Quantity: ...
def _get_oe_size(self, share, direction) -> Quantity: ...
def _adjust_share_by_liquidity(self, ctx, best) -> Decimal | None: ...  # 深度缩放,Debug 不覆盖
```

**生产 Strategy 不导入也不感知 `DebugConfig`**。

**当前 `services/strategy/service.py` 中的 `_check_and_adjust_size` 拆解**:
| 原来做的事 | 拆到哪 |
|---|---|
| 深度缩放(Step 1: 按可交易量等比例 scale) | Strategy 内部 hook `_adjust_share_by_liquidity` |
| 最小限额门控(Step 2: `check_min_size`) | **删除应用层**,完全用 NT `instrument.min_quantity`(NT RiskEngine 自动检查) |
| `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 常量 + `check_min_size` 函数 | **删除**(应用层不更严,完全用 NT instrument 元数据) |

**当前 `services/execution/` 拆解**:
| 子模块 | 处理方式 |
|---|---|
| `orchestrator.py` 套利决策算法 | **保留**,迁入 `ArbitrageStrategy` |
| `planner.py` 计划器 | **保留**,作为 strategy 内部 helper |
| `tracker.py` 订单追踪 | **删除**,NT `Strategy` 自带回调 |
| `service.py` 下单调度 + 事件分发 | **删除**,NT ExecutionEngine + MessageBus 替代 |
| `session.py` / `cleanup.py` / `recovery.py` | **待审计**,可能部分保留作为补偿撤单逻辑 |
| `mock_exchange.py` 测试用 | **保留**(改造为 NT `LiveExecutionClient` 子类,见 §6.6) |

**Step 4 启动时还需展开**:
- 补偿撤单方案(单腿成交另一腿失败时 —— 见 memory 中 `bug_compensating_cancel_missing`)
- `ArbitrageStrategy` 状态机设计(双腿事务,处理 `on_order_denied` / `on_order_rejected`)
- StrategyId 命名规则
- 多 MatchedPair 并发的资源/限额处理
- `_adjust_share_by_liquidity` 内部数据源(从 NT `cache.order_book(instrument_id)` 读)

---

### 5.5 Step 5: ExecutionClient(替代 executor) **【概要,待 Step 5 启动时展开】**

**两家 venue 走不同路径**:

**PM**: 用上游 `nautilus_trader/adapters/polymarket/execution.py:PolymarketExecutionClient`,**零代码**。
- 用 `py_clob_client.create_order(args)` 走库内部签名,**天然修掉 `bug_polymarket_order_version_mismatch`**(见 memory)
- 完整支持 `expiration` / `neg_risk` / `Post-Only` / 批量下单
- 通过 WebSocket USER channel 接收订单状态,调 `generate_order_*` 推回 NT MessageBus
- 含余额查询(`generate_account_state`)/ 持仓查询(`/positions` Data API)/ 撤单 / 查挂单
- 删除用户 `adapters/polymarket/executor.py`

**OE**: 自写 `OrbitExchExecutionClient(LiveExecutionClient)`。
- 内部 IO(Playwright 提交订单)从用户 `executor.py` 平移
- 必须实现 NT 契约: `_submit_order` / `_cancel_order` / `_modify_order`
- 必须事件回写: 订单状态变化时调 `generate_order_*` 推 NT MessageBus
- **账户状态维护(被动)**: 订阅 WS 帧,余额变化消息 → 调 `self.generate_account_state(...)` 写 Cache(OE 没有 REST 不能主动拉,只能 WS push)

**账户状态维护(2026-05-09 显式列出)**: 两家 venue 都在 ExecutionClient 内维护 NT `AccountState`,写 Cache。下游(`ArbitrageRiskEngine` 余额检查 / `WebGatewayActor` 转 JSON 推前端)从 Cache 读,对来源透明。

| Venue | 维护方式 |
|---|---|
| PM | **主动** —— 周期 timer 调 `client.get_balance_allowance`(上游 `execution.py:287 _update_account_state` 已实现) |
| OE | **被动** —— WS 帧解析(自写) |

**Step 5 启动时还需展开**:
- OE WS 订单事件订阅(用户当前是否已实现"订单成交回调"? 待审计)
- OE WS 余额消息的解析路径(审计现有 `odds_client.py` 是否抓余额帧)
- PM `bug_polymarket_order_version_mismatch` 用上游版本验证是否消失
- PM `_update_account_state` 触发频率(timer 周期 / 每次下单/撤单后 / 两者)
- StrategyId / OrderId / 命名一致性

---

### 5.6 Step 6: Risk 层重构(替代 risk) **【概要,待 Step 6 启动时展开】**

**关键边界(2026-05-09 三次修正后锁定)**: Risk 在 NT `submit_order` 管道上**透明拦截**,Strategy **不引用** Risk。**没有独立的 BalanceMonitorActor**(告警让前端自己看)。

```
Strategy.submit_order(order)
    ↓ NT ExecutionEngine 自动路由
ArbitrageRiskEngine._check_order(order)         ← 透明拦截
    ├── 通过: 路由到 ExecutionClient
    └── 拒绝: generate_order_denied → Strategy.on_order_denied
```

#### 组件协作(账户状态由 ExecutionClient 维护)

```
ExecutionClient (per venue,数据维护)
├── PM: 主动 — clock.set_timer 周期调 client.get_balance_allowance
│         (上游 execution.py:287 已有 _update_account_state,可直接用)
└── OE: 被动 — 订阅 WS 帧,余额变化消息 → 解析后调 generate_account_state
                              ↓
            两条路径都把 AccountState 写入 NT Cache.account_state
                              ↓ 共享 Cache(NT 自带)
                              ├──→ ArbitrageRiskEngine._check_balance(order)  ← 下单前同步读 Cache
                              └──→ WebGatewayActor 订阅 AccountState 事件 → 推前端 JSON
                                                                              (用户在前端看告警,
                                                                               无独立告警 Actor)
```

**关键决定(2026-05-09 三次修正)**:
- **数据获取归 ExecutionClient**(PM 主动 / OE 被动,因 OE 没 REST 不能主动拉)
- **没有 `BalanceMonitorActor`**: NT 不自带这种告警 Actor,我们也不写;告警职责让前端自己看(WebGateway 订阅 AccountState 推 JSON 即可)
- 余额低于业务阈值的实时告警:**仅在前端层做**(浏览器看到余额数字判断),系统层不做主动 publish 告警

#### `ArbitrageRiskEngine`(NT `RiskEngine` 子类)

```python
# src/arbitrage/risk/engine.py
class ArbitrageRiskEngine(RiskEngine):
    """扩展 NT RiskEngine,加应用层余额检查。最小限额检查由 NT 父类自动处理。"""

    def _check_order(self, order: Order) -> bool:
        # NT 父类自动检查:
        #   - order.quantity >= instrument.min_quantity (最小限额)
        #   - order.quantity <= instrument.max_quantity
        #   - max_order_submit_rate / kill switch 等
        if not super()._check_order(order):
            return False
        # 应用层补充: 余额检查
        if not self._check_balance(order):
            return False
        return True

    # ─── Hook 点(Debug 子类可覆盖)───
    def _check_balance(self, order: Order) -> bool:
        """从 cache.account_state(...) 读余额,判断够不够下单。"""
        ...
```

**关键决定(2026-05-09 修正)**:
- 应用层 `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 常量 + `check_min_size` 函数 + `adjust_share_by_liquidity` 中的 Step 2 **全部删除**(应用层不比 NT instrument.min_quantity 更严,直接用 NT 自带)
- venue 偶发拒绝(cache stale 时余额预检查通过但 venue 真实余额不足)由 NT 标准 `on_order_rejected` 处理,**不是设计层的"双兜底"**(RiskEngine 是唯一真理源,venue 拒绝是 cache 同步异常)

#### ExecutionClient 的"账户状态维护"职责(本来就该有,这里显式列出)

| Venue | 维护方式 | 实现来源 |
|---|---|---|
| **PM** | **主动** —— 周期 HTTP 拉取 `client.get_balance_allowance` | 上游 `nautilus_trader/adapters/polymarket/execution.py:287 _update_account_state` 已实现,直接用 |
| **OE** | **被动** —— 订阅 WS 帧,余额变化消息 → 解析 → 调 `self.generate_account_state(...)` | 自写,Step 5 实施 |

两条路径都通过 `generate_account_state` 标准 NT 接口写入 Cache,下游(`ArbitrageRiskEngine` 同步读做检查 / `WebGatewayActor` 订阅事件推前端)对来源透明。

#### 拆解当前 `services/risk/service.py`

| 当前职责 | 目标位置 |
|---|---|
| 余额查询 / 维护 | **ExecutionClient 内部**(PM 主动 / OE 被动)+ 写 NT Cache |
| 余额阈值告警 | **删除** —— 让前端订阅 AccountState 自己看 |
| 余额门控(`check_balance`) | `ArbitrageRiskEngine._check_balance`(NT 标准管道拦截) |
| 循环熔断 | 待审计,可能 NT `RiskEngine` 配置 / 独立机制 |
| 持仓限额 | NT `RiskEngine` 自带或扩展 |

**Step 6 启动时还需展开**:
- NT `RiskEngine` 的具体可扩展性(子类化是否完整支持 / `_check_order` 是否是合适的 hook 点)
- 上游 PM `_update_account_state` 触发频率(timer 周期 / 下单后顺便触发 / 两者结合)
- OE WS 帧中余额信息的解析路径(Step 5 启动时审计 OE WS 协议)
- 循环熔断的归属(审计现有 `services/risk/service.py` 中相关逻辑)

---

### 5.7 Step 7: WebGatewayActor(替代 web_gateway)

**占位** —— 等 Step 6 完成后讨论。

预期方向:
- FastAPI 与 TradingNode 同进程同 loop
- HTTP 路由桥接 MessageBus
- **同时承担"行情格式适配"**: 订阅 NT `OrderBookDelta` / `MatchedPair` / **`AccountState`** 等 NT 标准类型,转 JSON 推前端
- 包括 **`AccountState` 推送**(替代独立的 BalanceMonitorActor)—— 浏览器拿到余额数字自己显示,余额低/熔断由用户看着判断
- 配置类 HTTP POST(如修改 `refresh_interval`)→ publish 到 MessageBus → `InstrumentRefresher` 等 Actor 收到更新

---

## 6. 当前阶段(Step 1)的横切问题

> 横切问题只列与当前讨论范围相关的;后续步骤特有的横切问题等轮到时再加。

### 6.1 InstrumentId 命名规则(Q1)

`BettingInstrument.id` 形如 `Symbol.Venue`。需要约束:
- 必须能稳定标识一个 outcome
- 必须能从交易所原始 ID 反向解析
- 长度受 NT `Symbol` 限制(待查证具体上限)

候选规则:
- PM: `{condition_id}-YES.POLYMARKET` / `{condition_id}-NO.POLYMARKET`
- OE: `{match_id}-{market_type}-{selection}.ORBITEXCH`

需在 Step 1 启动前敲定。

### 6.2 OE Playwright Browser 共享方案(Q2,✅ 已锁定)

**已敲定方案**: **方案 A —— 沿用现有 `PlaywrightBrowserManager`,所有权抽到 NT factory 层**。

#### 现状

`nautilus_trader/adapters/orbitexch/browser_manager.py:PlaywrightBrowserManager` 已存在(200 行),实现质量良好:
- ✅ 多 page 支持(`create_page(name)` / `get_page(name)` / `close_page(name)`)
- ✅ Anti-detection / stealth(`_setup_stealth`)
- ✅ Persistent context(`user_data_dir`,关键 —— OE 登录态持久化)
- ✅ 共享 BrowserContext(三方共享登录 cookie/session)

#### 唯一需要修正: 所有权位置

当前 `nautilus_trader/adapters/orbitexch/data.py:74,121`(用户半成品)的所有权设计**有问题**:
```python
async def _connect(self):
    await self._browser_manager.start()       # ← DataClient 启动 manager
async def _disconnect(self):
    await self._browser_manager.close()       # ← DataClient 关闭会杀掉所有 page
```

Step 1 修正: 把 manager 的所有权抽到 NT factory 层:
```python
# nautilus_trader/adapters/orbitexch/factories.py
_shared_manager = None

def get_orbitexch_browser_manager(config) -> PlaywrightBrowserManager:
    """全进程共享一个 manager 实例,生命周期归 NT TradingNode。"""
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = PlaywrightBrowserManager(
            user_data_dir=config.user_data_dir,  # 持久化登录态
            ...
        )
    return _shared_manager

def get_orbitexch_instrument_provider(...): 
    return OrbitExchInstrumentProvider(browser_manager=get_orbitexch_browser_manager(...), ...)

def get_orbitexch_data_client(...): 
    return OrbitExchDataClient(browser_manager=get_orbitexch_browser_manager(...), ...)

def get_orbitexch_exec_client(...):  # Step 5
    return OrbitExchExecutionClient(browser_manager=get_orbitexch_browser_manager(...), ...)
```

Manager 的 `start()` / `close()` 由 NT TradingNode 生命周期触发(通过 NT 自身的 connect/disconnect 钩子),**三方组件都不调**,只调 `create_page(name)` / `get_page(name)`。

#### Page 命名约定

| 消费者 | Page name | 用途 |
|---|---|---|
| `OrbitExchInstrumentProvider` | `"discovery"` | 列举赛事/市场 |
| `OrbitExchDataClient` | `"data"` | WS 帧拦截(单页多市场) |
| `OrbitExchExecutionClient` | `"execution"` | 提交订单 |

三方共享 BrowserContext = 共享登录态(用 `user_data_dir` 持久化)。各自专属 page = WS 帧拦截 / 页面状态互不干扰。

#### `nautilus_trader/adapters/orbitexch/data.py` 半成品的处理

这是之前部分迁移的产物,有几处需要 Step 2 整体重写:
1. **基类错**: 用了 `LiveDataClient`,应改为 `LiveMarketDataClient`(才有 `_subscribe_order_book_deltas` 等钩子)
2. **数据类型错**: 输出 `QuoteTick`,应改为 `OrderBookDelta`(与 PM 对齐,§5.2 已锁定)
3. **所有权错**: 如上,DataClient 启动/关闭 BrowserManager,Step 1 修正后只消费 page,不持有生命周期

**Step 1 阶段**: `data.py` 暂不动,保留作 Step 2 重写时的参考。Step 2 启动时整体替换。

### 6.3 NT 自带的持久化机制(影响 Q6 / Q7)

NT 通过 `CacheDatabaseAdapter`(`cache/database.pyx`)提供持久化,backing 当前**只支持 Redis**(`DatabaseConfig.type` 默认且仅 `"redis"`)。

**自动持久化的内容**:
| 命名空间 | 持久化内容 |
|---|---|
| `instruments` / `orders` / `positions` / `accounts` | NT 实体,框架自动写入 |
| `actors` / `strategies` | Actor / Strategy 自身状态,通过 `on_save() -> dict[str, bytes]` / `on_load(state)` 钩子 |
| (可选) `MessageBusConfig.database` | 消息流持久化,用于重放 |

**对本项目的影响**:

1. **不需要为 mutable config 单独引入 Redis 路径**。把可调参数挂到对应 Actor 的 `on_save / on_load`,搭顺风车。
2. **重启后订单/持仓自动恢复**(目前自研服务没这个能力,这是迁移红利之一)。
3. 你已经在用 Redis,只需配一次 `TradingNodeConfig(cache=CacheConfig(database=DatabaseConfig(type="redis", ...)))`。
4. 路径选择(Q6): **路径 A** —— 启用 NT `CacheDatabaseAdapter`,所有持久化(trading state + 各 Actor config)统一走同一个 Redis 实例;不再考虑路径 B/C。

**对应到 Step 2**:
- `InstrumentRefresher.on_save` 持久化 `refresh_interval`
- 重启时 `on_load` 读回 → 沿用上次面板调过的值
- 面板首次设值前: 用 `default_config.json` 中的 `refresh_interval_default`
- Redis RDB / AOF 配置需要确认(否则重启 Redis 会丢配置)

### 6.4 异构 Instrument 类型的语义归一(Q9)

PM 用 NT 现成的 `BinaryOption`(二元期权),OE 用 NT 现成的 `BettingInstrument`(体育博彩)。两者都是 `Instrument` 子类但字段不直接对齐。

**已锁定方案**: **A) MatchingActor 内部做语义归一,不依赖具体类型**。

**实现约定**: 两家 venue 的 `InstrumentProvider` 在加载时,把跨场馆匹配所需的通用字段以**统一 key 命名**塞进 `instrument.info` dict:

| 通用 key | PM 数据源 | OE 数据源 |
|---|---|---|
| `info["sport"]` | Gamma API `event.tags` | OE 赛事页运动分类 |
| `info["competition"]` | Gamma API `event.title` 解析 | OE 赛事页联赛名 |
| `info["home_team"]` | Gamma API `event.outcomes` 解析 | OE selection 名称 |
| `info["away_team"]` | Gamma API `event.outcomes` 解析 | OE selection 名称 |
| `info["start_ts"]` | Gamma API `event.startDate` | OE 赛事页开赛时间 |
| `info["selection_role"]` | "YES" / "NO" | "home" / "draw" / "away" |

`MatchEngine` 只读 `instrument.info`,不 `isinstance` 检查类型,也不 import `BinaryOption` / `BettingInstrument` 任一具体类。

**好处**:
- 新增 venue 时(假设 Pinnacle / FanDuel 等)只需让 Provider 输出统一 `info` dict,Matching 不动
- 未来 PM / OE 切换 NT 类型(比如 NT 给 PM 出新的体育博彩专用类型)时 Matching 也不动

**Step 1 OE Provider 的字段映射**: 必须在 §5.1 OE 自写时把上面 6 个 key 全部填好,否则 Step 3 匹配跑不动。

### 6.5 上游 PM 适配器的存在与影响(2026-05-09 发现)

迁移过程中**完全采用上游 PM 适配器**,删除用户自研的 `odds_client.py` / `executor.py`。理由(经子代理详细 diff 验证):

| 维度 | 用户自研 | 上游 NT |
|---|---|---|
| 行情订阅 | `websockets` 库 + 硬编码 5s 重连 + dict 输出 | NT `WebSocketClient` + 配置化指数退避 + `OrderBookDelta` 输出 |
| 下单 | 缺 `expiration` / `neg_risk` / `Post-Only` / 批量 | 全部完整 |
| 订单事件回写 | 无 USER channel 订阅 → 订单状态停在 LIVE 不更新 | 完整 USER channel + `generate_order_*` |
| Instrument 加载 | 仅 token list,无 fee schedule | 完整 `BinaryOption`,含 fee 富化 |
| ExecutionClient 契约 | 不继承 `LiveExecutionClient`,NT 不识别 | 标准实现 |
| `bug_polymarket_order_version_mismatch` | 复现中 | 用 `py_clob_client.create_order()` 内部处理,**天然规避** |
| 查仓位 | ✅(`odds_client.py:fetch_positions`,自定义 `PolymarketPosition`) | ✅ NT 标准 `PositionStatusReport` |
| 查挂单 | ✅(`odds_client.py:fetch_open_orders`,自定义 `PolymarketOrder`) | ✅ NT 标准 `OrderStatusReport` |
| 查单订单 / 查成交历史 | ❌ | ✅ |
| 撤单粒度 | 单笔 | 5 种(单 / 批量 / 按 strategy 全撤 / 全局全撤 / 按市场撤) |
| Reconciliation(启动/重连与 venue 对账) | 无 | NT 框架自带,上游 PM 实现 `generate_*_status_reports` 钩子 |
| API 调用同步机制 | `_api_lock` 串行化所有调用(粗粒度) | 无锁,所有 `ClobClient` 调用走 `asyncio.to_thread` 真并发 |

**没有保留用户自研版本的合理理由**: 上游版本在所有维度上都覆盖 + 更完整。**Reconciliation** 是迁移最大的隐性红利之一 —— 重启后挂单/仓位与 venue 自动对账。

### 6.6 Debug 注入框架(Q11)

#### 总览

`debug_config.json` 是**全栈行为注入框架**,远不止 mock_exchange。两层结构:

| 层 | 字段类别 | 当前位置 | 迁移后注入位置 |
|---|---|---|---|
| **A. 数据流替换** | `mock_data: {category: "odds"}` | odds_subscription 内 | DataClient Debug 子类(出口前替换) |
| | `mock_data: {category: "positions"}` | fetch_positions 内 | ExecutionClient Debug 子类(`generate_position_status_reports` 前替换) |
| **B. 客户端选择** | `overrides.use_mock_exchange` | service.py 分支 | 启动 factory 选 `Mock*ExecutionClient` |
| | `overrides.skip_execution` | service.py 分支 | 启动 factory 选 `SkipExecutionClient` 子类(`_submit_order` 直接 generate_order_filled mock 成交) |
| **C. Strategy 内部参数覆盖** | `min_rebate_rate` | strategy/service.py | `DebugArbitrageStrategy._get_min_rebate_rate` 子类覆盖 |
| | `polymarket_price` / `orbitexch_price` | 同上 | `_get_pm_price` / `_get_oe_price` 子类覆盖 |
| | `polymarket_size` / `orbitexch_size` / `order_size` | 同上 | `_get_pm_size` / `_get_oe_size` 子类覆盖 |
| **D. Risk 层覆盖** | `skip_check_size` | strategy/service.py(语义错位,见 §5.4 修正) | `DebugArbitrageRiskEngine` 子类覆盖,跳过 NT 父类的最小限额检查(让小单通过用于测试) |

#### 设计原则(P10,新增)

**所有 debug 行为变化必须通过子类化 + 工厂层选择实现,生产代码零 `if self._debug` 分支**。

理由:
1. **关注点分离**: 生产路径完全干净,无 debug 渗透
2. **架构对称**: DataClient / ExecutionClient / Strategy / Risk Actor 全部走子类化,统一机制
3. **测试隔离**: 测试生产类不需要 mock 掉 debug
4. **NT-pure**: 子类化 + 工厂选择是 NT 的原生扩展机制(参考 §5.5 PM 上游对应路径)

#### 配置加载: `DebugConfig` 单例

```python
# src/arbitrage/debug/config.py
class DebugConfig:
    """普通 Python 对象,启动时加载,通过 NT 工厂层 DI 分发。不进 NT Cache(Q11.5)。"""

    @classmethod
    def load(cls, path: str = "debug_config.json") -> "DebugConfig | None":
        if not os.path.exists(path): return None
        with open(path) as f: return cls(json.load(f))

    @property
    def enabled(self) -> bool: ...
    def get_override(self, name: str) -> Any | None: ...        # overrides 取值
    def get_mock(self, category: str, conditions: dict) -> dict | None:  # mock_data 按 conditions 匹配
        ...
```

#### 子类化模板

**DataClient**:
```python
# src/arbitrage/debug/data_clients.py
class DebugPolymarketDataClient(PolymarketDataClient):
    def __init__(self, *args, debug: DebugConfig, **kwargs):
        super().__init__(*args, **kwargs)
        self._debug = debug
    def _handle_data(self, data):
        if isinstance(data, OrderBookDelta):
            mock = self._debug.get_mock("odds", conditions={...})
            if mock: data = self._apply_mock_to_delta(data, mock)
        super()._handle_data(data)
```

**ExecutionClient**:
```python
class SkipExecutionPolymarketClient(PolymarketExecutionClient):
    """skip_execution = true 时使用,不发真单,直接 mock 成交事件。"""
    async def _submit_order(self, command):
        self.generate_order_submitted(...)
        self.generate_order_filled(...)
```

**Strategy**:
```python
# src/arbitrage/debug/strategy.py
class DebugArbitrageStrategy(ArbitrageStrategy):
    def __init__(self, *args, debug: DebugConfig, **kwargs):
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _get_min_rebate_rate(self):
        ovr = self._debug.get_override("min_rebate_rate")
        return Decimal(str(ovr)) if ovr is not None else super()._get_min_rebate_rate()

    def _get_pm_price(self, ctx, direction):
        ovr = self._debug.get_override("polymarket_price")
        return Price(ovr, ...) if ovr is not None else super()._get_pm_price(ctx, direction)

    # ... 其它 hook 同理
```

**RiskEngine**:
```python
# src/arbitrage/debug/risk.py
class DebugArbitrageRiskEngine(ArbitrageRiskEngine):
    """skip_check_size 启用时跳过 NT 自带的最小限额检查(让小单通过用于测试链路)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs):
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _check_order(self, order):
        if self._debug.get_override("skip_check_size"):
            # 跳过 NT 父类的 min/max quantity 检查,只跑应用层余额检查
            return self._check_balance(order)
        return super()._check_order(order)
```

注: NT `RiskEngine` 的细粒度 hook(只跳 min_quantity 不跳 max_quantity 等)需 Step 6 启动时根据 NT API 确认。当前设计假定可整体 bypass 父类检查,如不行则需更细粒度 hook 覆盖。

#### 工厂选择

```python
# src/arbitrage/debug/factories.py
def make_pm_data_client(debug: DebugConfig | None, **kwargs):
    if debug and debug.enabled:
        return DebugPolymarketDataClient(debug=debug, **kwargs)
    return PolymarketDataClient(**kwargs)

def make_pm_exec_client(debug, **kwargs):
    if debug and debug.get_override("use_mock_exchange"):
        return MockPolymarketExecutionClient(debug=debug, **kwargs)
    if debug and debug.get_override("skip_execution"):
        return SkipExecutionPolymarketClient(debug=debug, **kwargs)
    return PolymarketExecutionClient(**kwargs)

def make_arbitrage_strategy(debug, **kwargs):
    if debug and debug.enabled:
        return DebugArbitrageStrategy(debug=debug, **kwargs)
    return ArbitrageStrategy(**kwargs)

# Risk / OE 同构
```

#### 目录结构

```
src/arbitrage/debug/                     # 新建,集中所有 debug 注入代码
├── __init__.py
├── config.py               # DebugConfig 单例
├── data_clients.py         # DebugPolymarketDataClient / DebugOrbitExchDataClient
├── exec_clients.py         # MockPolymarketExecutionClient / SkipExecution* 等
├── strategy.py             # DebugArbitrageStrategy
├── risk.py                 # DebugArbitrageRiskEngine
├── timeline.py             # 订单状态流转(NT Clock.set_timer 重写,Q11.4 = b)
└── factories.py            # make_*_client / make_arbitrage_strategy / ...
```

#### 文件迁移

| 当前 | 新位置 |
|---|---|
| `src/arbitrage/services/debug/` 整目录 | `src/arbitrage/debug/`(`config.py` / `manager.py` 重组) |
| `src/arbitrage/services/execution/mock_exchange.py` | `src/arbitrage/debug/exec_clients.py` + `timeline.py`(拆分 + NT-pure 重写) |
| `debug_config.json`(项目根) | 保持原位 |
| `debug_config.example.json` | `src/arbitrage/debug/debug_config.example.json` |
| `tests/arbitrage/services/integration/debug_config_size_test.json` | `tests/arbitrage/_helpers/debug_configs/` |

#### Q11 子问题锁定

| # | 问题 | 锁定 |
|---|---|---|
| Q11.1 | 是否实现热重载 | ✅ 不实现,改完重启进程 |
| Q11.2 | `skip_check_size` 落点 | ✅ `DebugArbitrageRiskEngine._check_order` 子类覆盖,跳过 NT 自带的最小限额检查(让小单通过用于测试链路)。**修正**: 应用层 `MIN_SIZE_*` 常量与 `check_min_size` 函数全部删除(应用层不比 NT instrument.min_quantity 更严);深度缩放归 Strategy 内部,不归 Risk |
| Q11.3 | `skip_execution` 实现 | ✅ `SkipExecutionPolymarketClient(PolymarketExecutionClient)` 子类,`_submit_order` 直接 generate_order_filled mock |
| Q11.4 | timeline 引擎(订单状态流转)平移还是重写 | ✅ 重写为 NT-pure 风格,用 `Clock.set_timer` 触发状态变化 |
| Q11.5 | DebugConfig 是否进 NT Cache | ✅ 不进 Cache,只通过 NT 工厂层 DI 传递(YAGNI) |
| **全局** | **架构对称** | ✅ **所有 debug 行为变化通过子类化 + 工厂层选择(P10),生产代码零 `if self._debug` 分支** |

### 6.7 上游 ClobClient 调用是否需要外层加锁(Q10)

**已锁定方案**: **不加锁,直接用上游裸版**(`async with self._api_lock` 不再保留)。

理由 / 风险评估:
- 上游所有 `ClobClient` 同步方法走 `asyncio.to_thread` 进默认 ThreadPoolExecutor,本质是真正多线程并发
- 用户原 `_api_lock` 一刀切串行化,粒度太粗(读类查询也被串行,损失吞吐)
- `py_clob_client` 是否完全线程安全未实测,但上游被 NT 用户广泛使用未爆问题,作为基线可接受
- 如果实盘观察到并发问题(典型嫌疑: nonce 碰撞、签名错乱),再做**子类包装** —— 只对写操作(`_submit_order` / `_cancel_order` / `_batch_cancel_orders`)加锁,读操作保持并发:

```python
# 仅作为后备方案保留,初版不实施
class LockedPolymarketExecutionClient(PolymarketExecutionClient):
    def __init__(self, ..., write_lock: asyncio.Lock):
        super().__init__(...)
        self._write_lock = write_lock

    async def _submit_order(self, command):
        async with self._write_lock:
            await super()._submit_order(command)

    async def _cancel_order(self, command):
        async with self._write_lock:
            await super()._cancel_order(command)
```

WS 订阅锁(用户原 `_ws_lock`)直接删除 —— 上游用引用计数处理(`websocket/client.py:207-214`),更优。

---

## 7. 当前开放问题清单

> 列出 Step 1 阻塞的问题以及讨论中已浮现、影响 Step 2/3 设计的问题。后续步骤特有的问题等轮到时再加。

| # | 问题 | 当前倾向 / 锁定方案 | 阻塞 |
|---|---|---|---|
| Q1 | InstrumentId 命名规则 | ✅ **已锁定**: PM `{condition_id}-{token_id}.POLYMARKET`(用上游 helper),OE `{market_id}-{selection_id}.ORBITEXCH`(自写 helper) | ~~Step 1~~ |
| Q2 | OE Playwright Browser 共享方式 | ✅ **已锁定**: 方案 A,沿用现有 `PlaywrightBrowserManager`,所有权从 DataClient 抽到 NT factory 层(`get_orbitexch_browser_manager` 全进程共享单例);三方按 page name `"discovery"`/`"data"`/`"execution"` 拿专属 page,共享 BrowserContext(共享登录态) | ~~Step 1~~ |
| Q3 | `refresh_interval` 是面板参数还是只启动 config | 面板参数,通过 `InstrumentRefresher` Actor 持久化 | Step 2 |
| Q4 | 单 venue refresh 失败如何处理 | 不发 `InstrumentsRefreshed` 事件,matching 自然 gate 住 | Step 2/3 |
| Q5 | matching 的"近期"窗口大小 | `2 × refresh_interval`,允许一次 retry 缓冲 | Step 3 |
| Q6 | 持久化存储用什么 | 路径 A: 启用 NT `CacheDatabaseAdapter`(Redis) | Step 2 |
| Q7 | 哪些参数 runtime-mutable + 持久化 | 每 Step 自管;Step 2 范围内只 `refresh_interval` | (per-step) |
| Q8 | 调度逻辑放 DataClient 还是单独 Actor | 单独 `InstrumentRefresher` Actor | Step 2 |
| **Q9** | **PM/OE 异构 instrument 类型如何在 MatchingActor 中归一** | ✅ **已锁定**: 方案 A,Actor 内通过 `instrument.info` dict 中的统一 key(sport / competition / home_team / away_team / start_ts / selection_role)做匹配,不依赖具体类型 | Step 1 (OE Provider 字段映射) + Step 3 |
| **Q10** | **上游 PM ExecutionClient 是否需要外层 lock 包装(用户原 `_api_lock` 是否保留)** | ✅ **已锁定**: 不加锁,直接用上游裸版;遇到问题再子类化只对写操作加锁。WS `_ws_lock` 直接删除(上游用引用计数) | Step 5 (后备方案) |
| **Q11** | **Debug 注入框架的迁移设计** | ✅ **已锁定(P10 / §6.6)**: 所有 debug 行为变化通过子类化 + 工厂层选择;生产代码零 `if self._debug` 分支。子问题 Q11.1-Q11.5 已逐项锁定。**关联边界修正(Q12)**: `_check_and_adjust_size` 拆解 —— Step 1 深度缩放归 Strategy 内部 hook,Step 2 最小限额由 NT `instrument.min_quantity` 自动处理(应用层删除);Strategy 不引用 Risk;skip_check_size 落点是 `DebugArbitrageRiskEngine` | Step 4-7 |
| **Q12** | **Risk 层架构: LiquidityRiskActor 是否需要** | ✅ **已锁定: 不需要**。深度缩放归 Strategy 内部职责(算应该下多少 share);最小限额检查由 NT RiskEngine 自动处理(`instrument.min_quantity`);余额检查由 `ArbitrageRiskEngine`(NT RiskEngine 子类)在 `submit_order` 管道上透明拦截。Strategy 不引用 Risk。venue 偶发拒绝(cache stale)由 NT 标准 `on_order_rejected` 处理,不是设计层"双兜底" | Step 4-6 |

---

## 8. 迁移顺序与验收标准(高层)

> 详细验收标准在每个 Step 启动时再细化。

| Step | 内容 | 状态 |
|---|---|---|
| Step 0 | 解锁 Q1 / Q2 | **当前位置** |
| Step 1 | InstrumentProvider × 2 | 待 Step 0 完成 |
| Step 2 | DataClient × 2 + InstrumentRefresher × 2 | 待 |
| Step 3 | MarketMatchingActor | 待 |
| Step 4 | ArbitrageStrategy | 待 |
| Step 5 | ExecutionClient × 2 | 待 |
| Step 6 | ArbitrageRiskEngine(NT RiskEngine 子类)+ ExecutionClient 账户状态维护 | 待 |
| Step 7 | WebGatewayActor | 待 |
| Step 8 | 清理 + 文档更新 | 待 |

每一步遵循:
1. 启动前补充该步骤详细设计到 §5.N
2. 实现 + 独立冒烟验证
3. 与现有路径并行运行至少一周
4. 验证无回归后,删除旧实现
5. 更新本文档"修订记录"

---

## 9. 不在本次迁移范围

- backtest 支持(需 Arrow schema、历史回放)
- 多策略 / 多账户隔离
- 跨进程 NT 部署模式
- WebSocket 推送细化

---

## 修订记录

| 日期 | 变更 |
|---|---|
| 2026-05-09 | 初稿 |
| 2026-05-09 | 重构: 按"逐步推进"组织(P7);目录组织改为按功能模块(P8);DataClient 拥有 InstrumentProvider 周期刷新;MarketMatchingActor 触发逻辑改为订阅 instrument 事件 + 防抖;Step 4-7 收回为占位,只敲 Step 1 |
| 2026-05-09 | 调度归属修正: 周期刷新从 DataClient 抽出,改为独立 `InstrumentRefresher` Actor(每 venue 一个),理由是状态归属清晰 + 走 NT actor 持久化 + DataClient 只管 IO。MarketMatchingActor 触发改为订阅 `InstrumentsRefreshed` 批次完成事件 + 跨 venue gating(替代 per-instrument 防抖)。新增 §6.3 NT 自带持久化机制(`CacheDatabaseAdapter` + Redis backing + Actor `on_save/on_load`)。开放问题扩充 Q3-Q8。 |
| 2026-05-09 | 目录组织修正: 取消 `src/arbitrage/scheduling/` 单 Actor 子目录(违反 P8),`InstrumentRefresher` 落在 `src/arbitrage/discovery/refresher.py`(承接原 `services/market_discovery/` 的 capability 名)。新增 P9 说明 `src/` 与 `nautilus_trader/` 的边界(理由是 git upstream merge 工作流,不是架构原因)。强化 P8: 不为单个 Actor 单独建目录,参照 NT 自己的做法(`Controller` 在 `trading/`,`OrderEmulator` 在 `execution/`)。 |
| 2026-05-09 | **重大发现**: 上游 NT 已有完整 PM 适配器(子代理详细 diff 验证)。Q1 锁定: PM 用上游 `{condition_id}-{token_id}.POLYMARKET`,OE 仿 PM 风格 `{market_id}-{selection_id}.ORBITEXCH`。Q9 新增 + 锁定: PM `BinaryOption` + OE `BettingInstrument` 异构,MatchingActor 通过 `instrument.info` 统一 key 做归一(方案 A)。§5.1 / §5.2 / §5.5 PM 部分改为零代码(直接用上游),用户 `odds_client.py` / `executor.py` 整体删除,顺带规避 `order_version_mismatch` bug。§5.4 ArbitrageStrategy 重要性升级(没它 Step 5 上游 ExecutionClient 没人调用)。新增 §6.4 异构 instrument 归一,§6.5 上游 PM 适配器影响。映射表 §4 大幅扩展。 |
| 2026-05-09 | Q10 新增 + 锁定: 不加锁直接用上游裸 ExecutionClient,遇到问题再子类化包写锁(后备方案模板已留 §6.6)。WS 订阅锁直接删除(上游用引用计数更优)。§6.5 表格补 5 行(查挂单 / 单订单 / 成交历史 / 撤单粒度 / Reconciliation 红利 / 同步机制对比),澄清用户其实有 `fetch_open_orders` 接口但属上游标准化报告体系外。 |
| 2026-05-09 | Q2 锁定: 沿用现有 `nautilus_trader/adapters/orbitexch/browser_manager.py:PlaywrightBrowserManager`(代码质量良好);所有权从 DataClient 抽到 NT factory 层(`get_orbitexch_browser_manager` 共享单例),三方共享 BrowserContext(共享登录态),按 page name `"discovery"`/`"data"`/`"execution"` 拿专属 page。当前半成品 `nautilus_trader/adapters/orbitexch/data.py` 标记为"Step 2 整体重写"(基类错: 用 `LiveDataClient` 应为 `LiveMarketDataClient`;数据类型错: `QuoteTick` 应为 `OrderBookDelta`;所有权错: 启动/关闭 manager 应改为只消费)。§6.2 重写为完整方案。 |
| 2026-05-09 | Q11 锁定: Debug 注入框架完整设计写入新增 §6.6。新增 P10 设计原则: 所有 debug 行为通过子类化 + 工厂层选择,生产代码零 `if self._debug`。子问题 Q11.1-Q11.5 全部锁定。**关联边界修正**: `_check_and_adjust_size` 整段从 `services/strategy/service.py` 抽到 Risk 层(§5.4 / §5.6 边界改写),Strategy 只管决策不做物理校验,`skip_check_size` 落点是 Risk 层 Debug 子类。校准 NT RiskEngine 真实角色: 只负责框架层订单合法性(min/max quantity 等),应用层尺寸/余额校验归自写 `LiquidityRiskActor` / `BalanceMonitorActor`(之前 §5.6 表述为"下单前检查归 RiskEngine"是误解)。§5.4 Strategy 设计加 hook 点契约(`_get_min_rebate_rate` / `_get_pm_price` 等),为 Debug 子类预留覆盖位。 |
| 2026-05-09 | 测试结构落地: 新建 `tests/arbitrage/<capability>/` 按 P8 capability 组织(discovery / matching / adapters/{polymarket,orbitexch} / strategy / risk / web / debug / e2e / _helpers)。形态混合: 每个 capability 一个 README.md(用例编号 / 前置 / 输入 / 步骤 / 期望 / 验收)+ skeleton `.py`(pytest 函数体目前 `pytest.skip` + docstring 引用 README 用例)。Step 1-2 详细写,Step 3 摘要,Step 4-7 占位。删除旧 `tests/arbitrage/services/`(1.0M)/ `tests/mock_exchange/`(32K,框架本体在 src 保留)/ 空的 `tests/odds_signal_trigger/` / 错位的 `tests/unit_tests/web_panel/`(132K)。`debug_config_size_test.json` 移到 `tests/arbitrage/_helpers/debug_configs/`。NT 框架测试(`unit_tests/{accounting,cache,...}` 等)未动。`tests/arbitrage/conftest.py`(NT 兼容 fixture)保留。 |
| 2026-05-09 | Q12 锁定 + 边界二次修正(基于用户反馈): **重新拆分 risk vs strategy 的职责**。深度缩放(`_check_and_adjust_size` Step 1)**归 Strategy 内部 hook**(算下多少 share,是策略性决定不是风控);最小限额检查(Step 2 + `MIN_SIZE_POLYMARKET/_ORBITEXCH` 常量)**整体删除**,完全用 NT `instrument.min_quantity` 自动检查(应用层不更严);余额检查归 `ArbitrageRiskEngine`(NT `RiskEngine` 子类),在 `submit_order` 管道上透明拦截。**Strategy 不引用 Risk** —— `LiquidityRiskActor` / `LiquidityRiskService` 这层删除(Q12 close)。skip_check_size 落点修正: `DebugArbitrageRiskEngine._check_order` 跳过 NT 父类的最小限额检查。venue 偶发拒绝由 NT 标准 `on_order_rejected` 处理,不算设计层"双兜底"。映射表 §4 / §5.4 / §5.6 / §6.6 全部更新。 |
| 2026-05-09 | BalanceMonitorActor 三次修正(用户指出 OE 没法主动拉余额): **账户状态维护归 ExecutionClient**(PM 主动 timer / OE 被动 WS push),不归 BalanceMonitorActor。BalanceMonitorActor 重定义为**反应型 Actor**: 只订阅 `cache.account_state` 变更做阈值告警,不主动拉数据(避免通用 Actor 含 venue 特异性)。§5.5 ExecutionClient 显式列出"账户状态维护"职责;§5.6 重写组件协作图;§4 映射表更新。`tests/arbitrage/risk/README.md` 加 risk-6.5 / 6.6(PM 主动 / OE 被动)与 risk-6.7(反应型告警)用例,删除"周期轮询"用例。澄清 Q1: NT 不自带"业务级余额够不够"检查,只自带框架层订单合法性 + Margin 账户的 margin 计算。 |
| 2026-05-09 | BalanceMonitorActor 四次修正(用户选 D: 不做告警让前端自己看): **彻底删除 BalanceMonitorActor**。NT 没自带这种 Actor,我们也不写。`AccountState` 由 WebGatewayActor 订阅 + 转 JSON 推前端,浏览器看着判断。无系统层主动告警 / publish BalanceAlert。如未来需要熔断 / Slack 推送等,按时再加(P7 不超前实现)。Step 6 名称从 "BalanceMonitorActor + RiskEngine 配置" 改为 "ArbitrageRiskEngine + ExecutionClient 账户状态维护"。§3 架构图 / §4 映射表 / §5.6 / §5.7 / §6.6 Debug 表 / Step 8 全部更新。`tests/arbitrage/risk/README.md` 删 risk-6.7;`tests/arbitrage/web/README.md` 加 web-7.3(AccountState 推送替代 BalanceAlert)。 |
