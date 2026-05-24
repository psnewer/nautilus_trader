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
| **P11** | **无自然归属的跨组件协议单独成章,不嵌在某组件子节下** | 当一个机制描述的是组件**之间**的协调/共享契约(同步、互斥、消息协议、全局状态、熔断),且**找不到单一自然归属**,就提升为独立横切章节(§6.x),章首列明"哪几个组件共同实现本契约、各自实现什么",让任一实现者以该章为准。**主判据(归属)**:有没有一方是契约**定义者**、其余只**消费**(清晰的生产者/消费者 或 宿主/调用方不对称)?有 → 放主方小节 + 从另一方交叉引用,**不**单独成章;没有(一群对等组件共同维护同一不变量)→ 单独成章。**辅助提示(数量)**:无归属的情况**通常涉及 ≥3 个组件**——因为 2-方交互几乎总有生产者/消费者不对称可作归属;但**恰好 2 个纯对等、无主的协议同样适用**,数量只是启发不是硬线。反例:同步协议(strategy+两健康检查+execution,4 方无主)原误置于"§6.8 健康检查"下,后提升为 §6.10。 |

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

### 4.0 全局推进总览(Step 状态 + Q 决策索引,2026-05-21)

> 当前所有 Step 都停在"方向/概要锁定",**无一进入逐行详细设计**(P6 不超前)。横切决策 Q1–Q19 散落各章,此处汇总作单一索引。

**7 个 Step 状态**:

| Step | 组件 | 状态 | 关联横切 Q | 章节 |
|---|---|---|---|---|
| 1 | InstrumentProvider(市场发现) | 方向锁定 | Q1 Q2 Q9 | §5.1 |
| 2 | DataClient + InstrumentRefresher(赔率订阅) | 概要 | Q3 Q4 Q6 Q7 Q8 | §5.2 |
| 3 | MarketMatchingActor(匹配) | 概要 | Q4 Q5 Q9 | §5.3 |
| 4 | ArbitrageStrategy(决策) | 概要 | Q12 Q13 Q14 Q16 Q19 | §5.4 |
| 5 | ExecutionClient(PM 上游薄子类 / OE 自写)+ merge/claim | 概要 | Q10 Q13 Q15 Q17 Q18 Q19 | §5.5 §5.8 §6.8 |
| 6 | ArbitrageRiskEngine + ArbitragePortfolio(风控/指标) | 概要 | Q12 Q14 Q16 Q17 | §5.6 §6.9 |
| 7 | WebGatewayActor(前端网关) | 占位 | — | §5.7 |
| 横切 | Debug 注入框架 | 锁定 | Q11 | §6.6 |

**Q1–Q19 一句话索引**(✅=已锁定):

| Q | 结论 | 章节 |
|---|---|---|
| Q1 ✅ | InstrumentId 命名:PM `{condition_id}-{token_id}.POLYMARKET` / OE `{market_id}-{selection_id}.ORBITEXCH` | §6.1 |
| Q2 ✅ | OE Playwright Browser 共享单例,所有权抽到 NT factory;三方按 page name 拿专属 page | §6.2 |
| Q3 | `refresh_interval` 面板参数,`InstrumentRefresher` 持久化 | §5.2 |
| Q4 ✅ | 单 venue refresh 失败不发 `InstrumentsRefreshed`,matching 自然 gate | §5.3 |
| Q5 | matching"近期"窗口 = 2×`refresh_interval` | §5.3 |
| Q6 | 持久化用 NT `CacheDatabaseAdapter`(Redis) | §6.3 |
| Q7 | runtime-mutable 参数每 Step 自管 | §6.3 |
| Q8 | 调度归独立 `InstrumentRefresher` Actor | §5.2 |
| Q9 ✅ | 异构 instrument 经 `instrument.info` 6 个统一 key 归一 | §6.4 |
| Q10 ✅ | 上游 ClobClient 不加外层锁,直接用裸版 | §6.7 |
| Q11 ✅ | Debug 全走子类化 + 工厂层选择,生产代码零 `if debug` | §6.6 |
| Q12 ✅ | 无 LiquidityRiskActor;深度缩放归 Strategy、最小限额用 NT 自带、余额归 RiskEngine | §5.6 |
| Q13 ✅ | 健康检查下沉各 adapter;execution 退化为单一职责 session,移除 recovery | §6.8 |
| Q14 ✅ | way_rebate 等 → `ArbitragePortfolio` 子类(kernel swap)+ settled gate | §6.9 |
| Q15 ✅ | execution tracking 绝对超时(NT clock 一次性 alert),超时即停不补救 | §6.8.5 |
| Q16 ✅ | tp/sl/global_sl 三门限 → `ArbitrageRiskEngine._check_rebate_gates` 逐 submit deny(别开新仓) | §5.6 |
| Q17 ✅ | PM 余额事件驱动(连接+链上成交确认)、健康检查不拉余额;可用余额按 venue 非对称(PM 自扣挂单 / OE 信 WS) | §5.5 §5.6 |
| Q18 ✅ | merge/claim 保留自研;**Q18b** 并入 PM 健康检查 tick(非独立 Actor)、结果不作健康判据;**Q18c** 宿主 = PM ExecutionClient 薄子类(三层:宿主/`PolymarketSettlement` 编排/`contract.py` IO) | §5.8 §6.8.4 |
| Q19 ✅ | 健康检查 ⊥ 执行 **全局**互斥;msgbus 消息(OE/PM 各发各的,ref-count 并成全局态)+ strategy 前置 pre-check 放弃机会;单 loop 无锁 | §6.10 |
| Q20 ✅ | strategy 机会快照隔离:开跑时冻该 pair 的订单簿+持仓+way_rebate(预算冻结),全程用拷贝免受新成交扰动;安全闸走 live;NT 无原生快照故自建;strategy 内部不单独成章 | §5.4 |

---

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
| `services/execution/cleanup.py`(merge/claim 编排)+ `adapters/polymarket/contract.py`(链上 IO) | **并入 PM 健康检查**(§6.8.4)+ **保留** `contract.py` | merge/claim 是链上 CTF 操作,**上游 ExecutionClient 无对应物,不可删**;编排平移进 PM 健康检查 tick(复用其 `/positions` 拉取,Q18b),结果不作健康判据;`contract.py` 作 IO 层保留;**不设独立 Actor** | §5.8 / §6.8.4 |

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
- **健康检查 / 页面刷新归本 adapter**(2026-05-19 Q13): 原 `services/*/` 下的 OE 网页监控并入,两个刷新触发(时间维度 + `leg_settled=false`)详见 §6.8

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
| **机会评估**(这个套利机会够不够赚才开,`min_rebate_rate`) | **Strategy 内部职责** | Strategy 读 `portfolio.min_way_rebate` 与 `_get_min_rebate_rate()` 比较;不够就不 submit |
| **深度缩放**(根据流动性算应该下多少 share) | **Strategy 内部职责** | `_adjust_share_by_liquidity` hook,Strategy 算订单参数时自己处理 |
| **最小限额检查**(`size ≥ instrument.min_quantity`) | **NT RiskEngine 自动** | NT 自带,无需扩展(应用层不更严,删除 `MIN_SIZE_*` 常量) |
| **余额检查**(够不够下单) | **`ArbitrageRiskEngine`(NT RiskEngine 子类)** | NT 自动拦截 `submit_order` 管道 |
| **组合级硬停**(止盈 `match_tp` / 止损 `match_sl` / 全局熔断 `global_sl`) | **`ArbitrageRiskEngine._check_rebate_gates`** | NT 自动拦截;触线一律 deny = 别开新仓(2026-05-19 锁定,Q16,详见 §5.6) |

> **机会评估 vs 组合级硬停的分工(Q16)**: Strategy 判"这单值不值得做"(`min_rebate_rate`,正向门槛);Risk 判"无论机会多好,触 tp/sl/global 线一律不许开"(反向硬停)。两者正交。TP/SL 放 Risk 而非 Strategy,是因为它在本系统里不是挂 venue 的价格止损单,**唯一动作就是"挡新单"**,而挡单正是 NT `RiskEngine` 的本职。撤单走另一命令通路,不受这些 deny 影响。

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
| `session.py` / `recovery.py` | **删除**(2026-05-21 用户拍板,补救逻辑全归 strategy,不保留旧外壳) |
| `cleanup.py`(merge/claim 编排) | 编排平移进 `PolymarketSettlement`(Q18c),旧外壳删除;`contract.py` 保留作 IO |
| `mock_exchange.py` 测试用 | **保留**(改造为 NT `LiveExecutionClient` 子类,见 §6.6) |

**机会快照隔离(Q20,2026-05-21 锁定)**:

> **动机**:订单规划 + 执行期间,避免被新成交(改持仓/订单簿 → 改机会计算)扰动。strategy 在机会评估开跑时**冻一份该 pair 数据**,该次套利全程用这份拷贝;期间 live cache 照常更新,其它逻辑照走,互不影响。

- **NT 无原生快照可用**(已核实):`Cache.snapshot_position` 是 netting 平/反开仓的归档记账(pickle 存历史),**非读隔离视图**;`cache.order_book(id)` 返回 live 对象**直接引用**(增量帧原地改),无 copy-on-read / MVCC。→ **strategy 自建**(持仓用 `pickle.loads(pickle.dumps(...))` 深拷贝,订单簿冻取所需值)。
- **快照范围 = per-pair**(该 competition 所有腿,PM+OE),不是单 instrument——机会计算需全腿。
- **冻什么(只冻机会计算输入)**:
  - 订单簿所需值(最优价 / 深度,供定价 + `_adjust_share_by_liquidity`)
  - 该 pair 持仓
  - **way_rebate**:**不在 cache**(纯 pull,§6.9)→ strategy 在取快照那刻**调一次 `portfolio.way_rebate(pair)` 把结果冻进快照**,下游规划/深度缩放复用冻结值,不重算(冻输入 ≡ 冻输出,way_rebate 是持仓纯函数)
- **不冻什么(走 live)—— 安全闸要最新信号**(用户拍板,选项 1):
  - settled pre-check(strategy-4.14)、Q19 健康检查互斥 pre-check(§6.10)、RiskEngine 余额检查(§5.6)**全读 live**。
  - RiskEngine 本就是独立组件读 live cache,天然如此;strategy 快照是规划私有,不传给 RiskEngine。
- **生命周期**:评估开跑 → 取快照 → 规划 + submit + tracking → 该次套利结束(双腿 terminal/timeout/放弃)→ 丢弃;**下一轮重新取新鲜快照**(与 strategy-4.9"每轮全量重算"一致,快照不跨轮持久)。
- **回收机制(防内存泄漏,2026-05-21)** —— 三条做到确定性释放:
  1. **取快照在 cheap live pre-check 之后**:settled / Q19 健康检查互斥 / 阈值等便宜的 live 判断**先跑**,通过了才取快照 → "放弃在 pre-check"根本不分配快照(省内存 + 免清理)。
  2. **单一释放点放 `finally`**:快照绑在 per-opportunity 上下文对象上,该上下文唯一 teardown 覆盖**所有出口**(正常 terminal / §6.8.5 timeout / 规划阶段放弃 / 异常)→ `finally` 释放。复用 Q19"清位放 finally"同款纪律。
  3. **绑上下文而非长存 dict**:快照存 per-opportunity 局部/上下文对象,作用域结束 GC 自动回收;**不**塞进 strategy 长存 `self._snapshots` 字典(避免漏 `del`)。
  - **上界保障**:Q19 全局互斥 → 同时只有 1 次执行在飞 → 持过执行全程的长命快照**最多 1 份**;评估阶段放弃的快照在评估内即释放。纯内存 strategy 本地,进程重启即清,无持久泄漏。
- **归属(P11)**:strategy 内部单一归属(自己拷贝、自己读),**不单独成章**,归本节。

**Step 4 启动时还需展开**:
- 补偿撤单方案(单腿成交另一腿失败时 —— 见 memory 中 `bug_compensating_cancel_missing`)
- `ArbitrageStrategy` 状态机设计(双腿事务,处理 `on_order_denied` / `on_order_rejected`)
- StrategyId 命名规则
- 多 MatchedPair 并发的资源/限额处理
- `_adjust_share_by_liquidity` 内部数据源(从 NT `cache.order_book(instrument_id)` 读)

**📍 落地索引(Step 4 实施清单 —— 横切结论单一真理源在 §6.x,本步只需逐项落地)**:

| 横切章节 | 本步要落地什么 |
|---|---|
| §6.8.2 | strategy 调 way_rebate / submit 前读 `leg_settled[pair_id]` 做 settled pre-check(三分支:entry 不存在/全 true → 通过;任一 false → abort) |
| §6.10 | **健康检查互斥 pre-check(Q19)**:submit 前 `if _health_check_active: 放弃机会 early return`(订阅 `health_check.*` 维护镜像);执行 session submit 时 publish `execution.started`、terminal/timeout publish `execution.finished`。与 settled pre-check 并列,均在首个 await 前同步判 |
| §6.8.5 | 接受 execution 的 cancel-only/submit+track session 语义:strategy **每轮全量重算**,不假设上轮 submit 还在排队;残留挂单时当次 submit 被丢弃,下轮重发(可能是另一意图) |
| §6.8.6 | 补偿撤单 / 撤后再下 / 单腿失败 / 裸单窗口(Q-F)**全归 strategy**,execution 不再 recovery —— 此为 Step 4 核心待设计项 |
| §6.9.3 / §6.9.6 | 调 `portfolio.min_way_rebate(pair_id)` 判机会;调用前 settled pre-check(与 Portfolio 内部 gate 双重防御) |
| §6.6 | 生产 Strategy 的 hook 必须可被 `DebugArbitrageStrategy` 子类覆盖(price/size/min_rebate override) |

| 测试用例文件 | 覆盖范围 |
|---|---|
| `tests/arbitrage/strategy/README.md` | strategy-4.1~4.14(决策、深度缩放、双腿、回调、每轮重算、settled pre-check、调 portfolio) |
| `tests/arbitrage/debug/README.md` | `DebugArbitrageStrategy` override hook |

---

### 5.5 Step 5: ExecutionClient(替代 executor) **【概要,待 Step 5 启动时展开】**

**两家 venue 走不同路径**:

**PM**: 用上游 `nautilus_trader/adapters/polymarket/execution.py:PolymarketExecutionClient`,**零代码**(或薄子类)。
- 用 `py_clob_client.create_order(args)` 走库内部签名,**天然修掉 `bug_polymarket_order_version_mismatch`**(见 memory)
- 完整支持 `expiration` / `neg_risk` / `Post-Only` / 批量下单
- 通过 WebSocket USER channel 接收订单状态,调 `generate_order_*` 推回 NT MessageBus
- 含余额查询(`generate_account_state`)/ 持仓查询(`/positions` Data API)/ 撤单 / 查挂单
- 删除用户 `adapters/polymarket/executor.py`
- **健康检查(2026-05-19 Q13;Q-L 翻盘为 (a))**: 默认周期 + 可被外部事件中断立即触发;动作 = 拉持仓/挂单(**不拉余额**,Q17)**走 NT 标准 report 通路**(`generate_position_status_report` / `generate_order_status_report` → ExecutionEngine reconcile),**不直接覆盖 cache**(保证 Portfolio 一致 + Strategy 收到 events.position/order);成功后 PM 侧涉及的所有 `leg_settled=true`。详见 §6.8.4

**OE**: 自写 `OrbitExchExecutionClient(LiveExecutionClient)`。
- 内部 IO(Playwright 提交订单)从用户 `executor.py` 平移
- 必须实现 NT 契约: `_submit_order` / `_cancel_order` / `_modify_order`
- 必须事件回写: 订单状态变化时调 `generate_order_*` 推 NT MessageBus
- **账户状态维护(被动)**: 订阅 WS 帧,余额变化消息 → 调 `self.generate_account_state(...)` 写 Cache(OE 没有 REST 不能主动拉,只能 WS push)
- **健康检查协同**: OE 的健康检查 / 页面刷新主体归 `OrbitExchDataClient`(§5.2 / §6.8.3);execution 端只在 tracking terminal 命中时把对应方向 `leg_settled=true`

**Execution session 契约(2026-05-19 Q13)** —— 适用于两家 venue:

| Session 类型 | 触发 | 动作 | track 终点 |
|---|---|---|---|
| **cancel-only** | submit 调用时 instrument 上有残留挂单 | 撤掉残留挂单(**丢弃**当次传入的 submit) | CANCELED |
| **submit+track** | submit 调用时无残留挂单 | 下单 → 等成交 | FILLED / CANCELED / REJECTED |

两种 session **都 track 到 terminal**;**移除**内部 recovery loop(re-plan / retry);补救逻辑下放 Strategy(后议)。详见 §6.8.5

**账户状态维护(2026-05-09 显式列出)**: 两家 venue 都在 ExecutionClient 内维护 NT `AccountState`,写 Cache。下游(`ArbitrageRiskEngine` 余额检查 / `WebGatewayActor` 转 JSON 推前端)从 Cache 读,对来源透明。

| Venue | 维护方式 |
|---|---|
| PM | **事件驱动**(上游 `execution.py:287 _update_account_state` 已实现)—— **连接时** + **链上成交确认时**(`POLYMARKET_FINALIZED_TRADE_STATUSES`)自动刷;**上游无周期 timer、NT 也无默认 QueryAccount 轮询、健康检查也不拉余额**(Q17,完全靠事件);可用余额由 `_check_balance` 自扣在途挂单(§5.6) |
| OE | **被动** —— WS 帧解析(自写) |

**Step 5 启动时还需展开**:
- ~~OE WS 订单事件订阅是否已实现~~ ✅ 已审计(2026-05-21):**功能性 stub** —— `websocket_handler` 有 'orders' 帧分发 + callback 注册管道,但 `message_parser.parse_order_message` 是 `# TODO return None`、`data.py:_on_order_update` 只 `log.debug`。**Step 5 必须实写**:解析订单帧 → `generate_order_*` 回写 NT
- ~~OE WS 余额帧解析路径~~ ✅ 已审计(2026-05-21):现代码**没抓** WS 余额帧(WS 只订 prices/orders 两类),余额走 `scraper.get_balance()` 页面 DOM 抓取。**用户确认 OE 站点 WS 确有余额帧(已含挂单占用)**,只是现代码没订。**Step 5 必须**:加第三类 WS 帧捕获 account/balance → `generate_account_state`(对齐 §5.5/§5.6 "被动 WS" 目标 + Q17 "OE 信 WS 不再减")
- PM `bug_polymarket_order_version_mismatch` 用上游版本验证是否消失
- ~~PM `_update_account_state` 触发频率~~ ✅ 已锁定(Q17):事件驱动(连接 + 链上成交确认),无周期 timer,健康检查不拉
- StrategyId / OrderId / 命名一致性

**📍 落地索引(Step 5 实施清单 —— 横切结论单一真理源在 §6.x,本步只需逐项落地)**:

| 横切章节 | 本步要落地什么 |
|---|---|
| §6.8.2 | execution tracking 收到任一 venue 确认事件(OrderAccepted / 部分 OrderFilled / 全成 / Canceled / Rejected)即把对应方向 `leg_settled=true`;每次新 execution 重置为 false |
| §6.8.3 | **OE**:健康检查吸收原网页监控;staleness 仅在 health tick 评估(收帧只写 `last_update_ns`,不立即刷新);超时则走 NT report 通路刷新页面/对账;删除旧 `_staleness_monitor_loop` |
| §6.8.4 | **PM**:周期 + 可被外部事件中断;走 NT report 通路(`generate_position_status_report` / `generate_order_status_report`,**不含余额**,Q17);成功后 PM 侧涉及方向 `leg_settled=true` |
| §6.8.4.5 | health check 循环用 NT Clock 自重排 one-shot alert(`set_time_alert_ns` + 回调 finally 里 `_schedule_next`);触发用 `override=True`;无 block/unblock |
| §6.8.5 | cancel-only / submit+track 两类 session,都 track 到 terminal;**移除 recovery loop**;超时用 NT one-shot alert `exec_timeout_{coid}`,terminal 时 `cancel_timer`;绝对超时不因部分成交重置 |
| §6.6 | `skip_execution` = `SkipExecutionPolymarketClient` 子类 `_submit_order` 直接 mock fill;OE 同构子类 |
| §6.7 | 上游 ClobClient 是否需外层锁(后备方案,初版不实施) |
| §5.6 账户状态 | PM 事件驱动(连接+链上成交确认)/ OE 被动 WS 帧 → `generate_account_state` 写 Cache;健康检查不拉余额;可用余额按 venue 非对称自算(PM 自扣挂单 / OE 信 WS,Q17,详见 §5.6) |

| 测试用例文件 | 覆盖范围 |
|---|---|
| `tests/arbitrage/adapters/orbitexch/README.md` | oe-adapter-2.health.{1,1b,2,3,4} / oe-adapter-2.schedule.{1-5} / oe-adapter-5.health.{1-5} / oe-adapter-5.session.{1-3} / oe-adapter-5.timeout.{1-5} |
| `tests/arbitrage/adapters/polymarket/README.md` | pm-adapter-5.health.{1-5} / pm-adapter-5.schedule.{1-5} / pm-adapter-5.session.{1-3} / pm-adapter-5.timeout.{1-5} |
| `tests/arbitrage/debug/README.md` | `SkipExecutionPolymarketClient` 链路 |

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
├── PM: 事件驱动 — 连接时 + 链上成交确认时自动调 _update_account_state
│         (上游 execution.py:287 已有,无周期 timer;周期兜底靠 §6.8.4 健康检查)
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
    """扩展 NT RiskEngine,加应用层余额检查 + 组合级硬停门限(tp/sl/global)。
    最小限额 / 名义额 / 频率检查由 NT 父类自动处理。"""

    # ⚠️ 签名必须与 NT 父类一致:cpdef bint _check_order(self, Instrument instrument, Order order)
    #    (engine.pyx:571,两参;cpdef 可被 Python 子类 override,内部调用会派发到这里)
    def _check_order(self, instrument: Instrument, order: Order) -> bool:
        # NT 父类自动检查:
        #   - order.quantity >= instrument.min_quantity / <= max_quantity (最小/最大限额)
        #   - max_notional_per_order / max_order_submit_rate / TradingState 等
        if not super()._check_order(instrument, order):
            return False
        # 应用层补充 1: 余额检查
        if not self._check_balance(order):
            return False
        # 应用层补充 2: 组合级硬停门限(只挡开新仓;撤单走另一命令通路不受影响)
        if not self._check_rebate_gates(order):
            return False
        return True

    # ─── Hook 点(Debug 子类可覆盖)───
    def _check_balance(self, order: Order) -> bool:
        """读 cache.account_state 判断够不够下单。可用余额按 venue 非对称(Q17):
        - PM: available = total − Σ(PM cache.orders_open 在途名义额)(reported free=total 不含占用,自扣)
        - OE: available = cache 余额(WS 已含挂单占用,不再减,否则双重扣减)
        """
        ...

    def _check_rebate_gates(self, order: Order) -> bool:
        """组合级硬停:平移自旧 services/risk/service.py:check_risk 的三个门限。
        触线一律 deny(= 别开新仓),与 strategy 的机会评估(min_rebate_rate)正交。

        数据源:pair_id = instrument.info["competition"];rebate **持 ArbitragePortfolio
        引用调其方法**(cache 不存 rebate,纯 pull 现算,§6.9),不重复算法。

        门限(任一触发 → return False):
            1. match_tp:某 pair 所有方向 rebate ≥ tp        → deny(已赚够,别再加仓)
            2. match_sl:某 pair min_way_rebate < match_sl   → deny(该场恶化,别再加仓)
            3. global_sl:global_min_rebate_sum < global_sl  → deny(账户级累计止损,熔断)

        settled gate 语义(2026-05-19 锁定):
            - leg_settled entry **不存在**(从没下过单)         → 放行(无结算风险,Q-G1)
            - way_rebate 返回 {} / min_way_rebate 返回 None     → 该 pair 无法评估 tp/sl,不在此门限拦
            - global_min_rebate_sum 返回 None(任一 pair 任一腿 false,fail-closed)
              → **deny(fail-closed 拦截)**:全局图景不全时一律挡新开仓,等健康检查 reconcile
                结算齐后 global 恢复实数自动放开;撤单走另一通路不受影响,无死锁
        """
        ...
```

**关键决定(2026-05-09 修正)**:
- 应用层 `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` 常量 + `check_min_size` 函数 + `adjust_share_by_liquidity` 中的 Step 2 **全部删除**(应用层不比 NT instrument.min_quantity 更严,直接用 NT 自带)
- venue 偶发拒绝(cache stale 时余额预检查通过但 venue 真实余额不足)由 NT 标准 `on_order_rejected` 处理,**不是设计层的"双兜底"**(RiskEngine 是唯一真理源,venue 拒绝是 cache 同步异常)

#### ExecutionClient 的"账户状态维护"职责(本来就该有,这里显式列出)

| Venue | 维护方式 | 实现来源 |
|---|---|---|
| **PM** | **事件驱动** —— 连接时 + 链上成交确认时 HTTP 拉 `get_balance_allowance`;**无周期 timer、健康检查也不拉**(Q17,完全靠事件) | 上游 `nautilus_trader/adapters/polymarket/execution.py:287 _update_account_state` 已实现,直接用 |
| **OE** | **被动** —— 订阅 WS 帧,余额变化消息 → 解析 → 调 `self.generate_account_state(...)` | 自写,Step 5 实施 |

两条路径都通过 `generate_account_state` 标准 NT 接口写入 Cache,下游(`ArbitrageRiskEngine` 同步读做检查 / `WebGatewayActor` 订阅事件推前端)对来源透明。

**⚠️ PM 余额的"挂单占用"陷阱(2026-05-19,Q17)**:
- 上游 `_update_account_state` 发 `generate_account_state(reported=True, balances=[AccountBalance(total, locked=0, free=total)])`。Polymarket 链上结算,未撮合限价单**不托管抵押品**,故 venue 视角 `free=total` 是真的。
- NT `CashAccount` 本可从 open orders 自算 locked(`AccountsManager.update_orders` → `calculate_balance_locked`,portfolio.pyx:572),但 `CashAccount.apply()` 见 `is_reported` 会**清空 `_balances_locked`**(cash.pyx:178-179),把 venue 上报当权威 → 每次刷新后 cache 的 `free` 又回到 total。
- **因此 PM 侧 `_check_balance` 不能直接信 `cache.account.balance_free()`**。

**可用余额算法 = 按 venue 非对称(2026-05-19 Q17 锁定)**:

| Venue | 余额来源 | 是否含挂单占用 | `_check_balance` 取可用余额 |
|---|---|---|---|
| **PM** | REST `get_balance_allowance`(reported,free=total) | ❌ 不含(链上不托管未撮合单) | `total − Σ(PM cache.orders_open 在途名义额)` **自扣** |
| **OE** | WS 被动推 | ✅ **已含**(用户确认,2026-05-19) | **直接信 cache 余额,不再减** |

> 不要对两 venue 用统一"自扣"——OE 再减会**双重扣减**低估可用。`_check_balance` 内按 `order.instrument_id.venue` 分支。

#### 拆解当前 `services/risk/service.py`

| 当前职责 | 目标位置 |
|---|---|
| 余额查询 / 维护 | **ExecutionClient 内部**(PM 主动 / OE 被动)+ 写 NT Cache |
| 余额阈值告警 | **删除** —— 让前端订阅 AccountState 自己看 |
| 余额门控(`check_balance`) | `ArbitrageRiskEngine._check_balance`(NT 标准管道拦截) |
| 单场止盈 `match_tp` | `ArbitrageRiskEngine._check_rebate_gates`(逐 submit deny = 别开新仓) |
| 单场止损 `match_sl` | 同上 |
| 全局累计止损 / 循环熔断 `global_sl` | 同上(2026-05-19 锁定:**不**翻 `TradingState`、**不**起监测 Actor;就是 `_check_order` 逐 submit 判) |
| 持仓限额 | NT `RiskEngine` 自带或扩展 |

**Step 6 启动时还需展开**:
- NT `RiskEngine` 的具体可扩展性(子类化是否完整支持 / `_check_order` 是否是合适的 hook 点)
- ~~上游 PM `_update_account_state` 触发频率~~ ✅ 已锁定(Q17):事件驱动,无周期 timer,健康检查不拉;可用余额 `_check_balance` 按 venue 非对称自算(PM 自扣 / OE 信 WS)
- OE WS 帧中余额信息的解析路径(Step 5 启动时审计 OE WS 协议)
- ~~循环熔断的归属~~ ✅ 已锁定(2026-05-19,Q16):tp/sl/global_sl 三门限进 `ArbitrageRiskEngine._check_rebate_gates`,逐 submit deny;不翻 `TradingState`、不起 Actor
- ~~`_check_rebate_gates` rebate 数据源~~ ✅ 已锁定:持 ArbitragePortfolio 引用调其方法(cache 不存 rebate,纯 pull 现算)
- ~~settled gate 失败时保守放行 vs 拦截~~ ✅ 已锁定:absent→放行(Q-G1);global `None`→**fail-closed 拦截**(2026-05-19)
- `match_tp` 是否真要留在 risk(它是"赚够别加"的盈利目标,SoC 上偏 strategy;锁定为 risk 是因动作纯为"挡新单")—— 如 Step 4/6 实施时发现别扭可回迁,不影响机制

**📍 落地索引(Step 6 实施清单 —— 横切结论单一真理源在 §6.x,本步只需逐项落地)**:

| 横切章节 | 本步要落地什么 |
|---|---|
| §6.9.1~6.9.5 | `ArbitragePortfolio(Portfolio)` 子类:`way_rebate` / `min_way_rebate` / `way_rebates_by_venue` / `global_min_rebate_sum` 四个 Python 方法;算法平移自 `services/risk/position.py`;`launcher.py` 里 `kernel._portfolio` swap;pull-based 无 cache;pair_id 由 `instrument.info["competition"]` 解析 |
| §6.9.3 settled gate | way_rebate 内部 gate(Q-G):entry 不存在 → 通过;全 true → 通过;任一 false → 返回空(`{}` / `None`);`global_min_rebate_sum` fail-closed 返回 `None` |
| §6.6 | `DebugArbitrageRiskEngine._check_order` 子类覆盖 `skip_check_size`(跳过 NT 父类最小限额检查) |
| §5.6 正文 | `ArbitrageRiskEngine._check_balance` 读 cache.account_state 做余额检查;`_check_rebate_gates` 做 tp/sl/global_sl 三门限(逐 submit deny,Q16);`_check_order` 签名两参 `(instrument, order)`;NT 父类自动管 min/max_quantity;**无 BalanceMonitorActor**、**无熔断 Actor / TradingState 翻闸** |

| 测试用例文件 | 覆盖范围 |
|---|---|
| `tests/arbitrage/risk/README.md` | risk-6.1~6.6(透明拦截/min_quantity/余额检查/venue 兜底/账户维护)+ risk-6.9.1~6.9.12(ArbitragePortfolio swap / way_rebate 算法 / settled gate) |
| `tests/arbitrage/debug/README.md` | `DebugArbitrageRiskEngine` skip_check_size |
| `tests/arbitrage/web/README.md` | web-7.{6,7}(HTTP GET positions → portfolio.way_rebate) |

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

### 5.8 merge / claim 链上结算(并入 PM 健康检查)**【概要,Q18/Q18b 锁定方向,待启动时展开】**

**背景**: PM 套利会在同一 condition 下持有多个 outcome token,且赛事结算后有 winning token 待赎回。这两件事(merge 回 USDC.e / redeem winning token)是**链上 CTF 合约操作**,**上游 NT PM ExecutionClient 完全没有**(它只包 CLOB 订单簿,grep 零命中)。本工程已自研实现,迁移时**保留**(不能被"删除自研、用上游替代"一刀切误伤)。

**复用的现成代码(已实现,保留)**:
| 文件 | 角色 | 迁移处理 |
|---|---|---|
| `nautilus_trader/adapters/polymarket/contract.py` `PolymarketContractService` | 链上 IO —— Builder Relayer 调 `CTF/NegRiskAdapter.mergePositions/redeemPositions`(区分 negRisk) | **保留**作为 IO 层(类比其它 adapter IO) |
| `src/arbitrage/services/execution/cleanup.py` `PostSessionCleanup._do_cleanup` | 编排 —— 按 condition_id 分组,决定 merge(两 outcome 都持,amount=min)/ redeem(redeemable=true) | 编排逻辑**平移进独立普通类 `PolymarketSettlement`**(`src/arbitrage/settlement/settlement.py`);旧 `PostSessionCleanup` / `SessionCompleteMessage` 触发外壳删除 |

**接口宿主(Q18c 钉死,2026-05-21)—— 三层结构**:

| 层 | 落点 | 职责 |
|---|---|---|
| **宿主 + 触发** | **PM `ExecutionClient` 薄子类**(`PolymarketExecutionClient` 子类) | 唯一同时具备 `generate_*_status_report`(execution.py:660/367 是 ExecutionClient 方法)+ 钱包 creds + 健康检查 tick 的组件;tick 内 reconcile 后调下层 |
| **编排** | `PolymarketSettlement` 普通类(组合持有,**非** ExecutionClient 方法) | 平移 `cleanup.py:_do_cleanup`:按 condition 分组、min 取量、redeemable 门控;入参 = `/positions` 原始响应 |
| **链上 IO** | `contract.py:PolymarketContractService`(保留) | relayer 调 `mergePositions`/`redeemPositions` |

- ExecutionClient 子类**只做"宿主 + 触发"**,`self._settlement.run(positions_raw)` 委托;链上分组/编码逻辑**不内联进订单客户端**。
- 原 Q18 的"独立 `PolymarketSettlementActor` + 自调度周期" **作废**——merge/redeem 与健康检查**都要拉 `/positions`**,合并一次拉取,避免双拉。

**纯度取舍(已知,2026-05-21)**: Q18 第一轮曾因"污染订单客户端"反对把 merge/claim 放进 ExecutionClient;Q18b 折叠进健康检查后,因健康检查宿主就是 ExecutionClient,merge/redeem 实际回到 ExecutionClient 内。这是**用纯度换"免双拉 + 复用钱包 creds"**的务实取舍,**用组合(`PolymarketSettlement` 独立类)而非内联**缓解——ExecutionClient 不持有链上编码逻辑,只持引用并触发。
- **数据源 = 健康检查那次 PM Data API `/positions` 原始响应**(含 `size`/`condition_id`/`neg_risk`/`redeemable`/`mergeable`);**不能用 NT cache 持仓**——上游翻成通用 `PositionStatusReport` 时丢了 `redeemable`/`mergeable`(execution.py:684)。
- 分组逻辑:merge = 同 condition ≥2 outcome 持仓 → `merge_positions(condition_id, min(sizes), neg_risk)`;redeem = `redeemable=true` → `redeem_positions(...)`。
- **redeem 的"结算滞后"**:体育赛事下单后数小时/天才链上结算(`redeemable` 翻 true),健康检查的周期性天然兜住(每 tick 检查),无需事件触发。

**merge/redeem 结果不作为健康判定依据(Q18b,用户拍板)**: `TxResult` 失败**只 log + 下次 tick 重试**(幂等:merge amount 按当时持仓重算、redeem 由 redeemable 门控),**不影响** `venue_connected` / `leg_settled`。健康检查的"健康"只看通讯/对账,不看链上结算成败。

**结果回流(无需显式 publish 事件)**: merge/redeem 成功后链上持仓变化 → 同一/下次 PM 健康检查的 position report 通路自然反映 → `way_rebate` pull-based 调用即重算。不发事件、不直接改 cache。

**与执行互斥**: merge/redeem 在健康检查 tick 内跑,自动受 §6.10 全局互斥保护(执行在飞时整个健康检查 tick 跳过,merge/redeem 也不会在执行期改链上持仓)。

**实施时还需展开**:
- merge/redeem 在 tick 内的次序(reconcile 先 / merge-redeem 后);失败不阻断 reconcile
- `PolymarketContractService` creds 如何与 execution config 共享 / 注入(NT factory 层;builder/relayer creds 为额外配置)
- `_execute_with_proxy` 的 monkey-patch derive 线程安全(patch 全局模块引用;健康检查在 NT 单 loop 内串行跑,确认与其它 PM 操作不并发——§6.10 互斥已大幅降低风险)
- merge/redeem 频率是否要低于健康检查(如每 N 个 tick 跑一次,避免频繁上链);Step 实施时定
- Debug 子类化(P10:`skip_settlement` / mock TxResult)

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

### 6.8 Health check 下沉到 adapter + Execution 简化(Q13,2026-05-19 锁定)

#### 6.8.1 总览

**两个紧耦合变更**:

1. **Health check 下沉**: 平台/比赛级健康状态由对应 adapter 自己维护,删除独立 health 服务 + 原 OE 网页监控
2. **Execution 简化**: 移除 execution 内部的 recovery loop,execution 退化为"执行 + 追踪",补救逻辑下放 strategy(后议)

---

#### 6.8.2 两层 health 状态

| 层级 | 字段 | 类型 | 初值 | 含义 |
|---|---|---|---|---|
| 平台级 | `venue_connected` | `bool`(per venue) | `false` | venue 连通性(WS/页面/REST 可达) |
| 比赛级 | `leg_settled` | `list[bool]`(per competition,len=本轮执行方向数) | 首次 execution 触发时创建,全 `false` | 上一次 execution 的尾巴是否已被 venue 端确认到本地 cache |

**`leg_settled` 语义**(2026-05-19 修正):

- **不是** "终态(完全成交/撤/拒)已对账";**是** "本轮 execution 启动后,venue 端的**任何确认事件**已落地本地 cache"
- 是 **"通讯通道存活信号"**,不是 "terminal 锁存"
- `false` = execution 已启动但本地**从未收到** venue 关于该腿的任何事件(包括 `OrderAccepted` 都没有)→ 说明可能 submit 没到 venue / WS 通道死了 / 页面挂了,**这才是健康检查兜底刷新的真实价值**
- `true` = 至少有一次 venue 确认事件已应用进 cache,**无论 order 处于何种状态**(`PARTIALLY_FILLED` / `FILLED` / `CANCELED` / `REJECTED` 都可以)
- 长度 = 本次 execution 涉及的方向数(2-way 比赛 2,3-way 比赛 3)
- 存储: 与该 competition 其它数据**同一 cache entry**(Q-A)
- 生命周期: **首次 execution 触发时创建,不删除**;此后每次新的 execution 启动时整组重置为 `false`,而不是删了重建

**为什么 partial fill 也算"确认"**:
- partial fill 证明 "submit 已到 venue,通讯通道工作,venue 端正在处理这个 order" —— `leg_settled=true` 表达的就是这一层
- 后续若再有 partial 漏消息,strategy 通过 cache 的 order quantity / position 大小可以察觉(策略层关心的事);`leg_settled` 不是"实时同步确认器",而是 **execution 启动后是否完全没回信** 的判断器

**状态流转**:

| 触发 | 动作 |
|---|---|
| 首次 execution 启动 | 创建 entry,所有方向 `settled=false` |
| 后续 execution 启动 | 该 competition 所有方向 `settled=false`(重置) |
| **该腿任何 venue 事件落地 cache**(`OrderAccepted` / `OrderFilled` 含 partial / `OrderCanceled` / `OrderRejected` ...) | 该腿 `settled=true` |
| OE health check 看到 `settled=false` | 刷新该 competition 页面 → 同步持仓/挂单/余额 → 该腿 `settled=true` |
| PM health check 成功拉取 持仓/挂单/余额 | PM 侧该 competition 所有方向 `settled=true` |
| **非 execution 触发的 venue 事件**(推迟到达 / 手动在 venue 操作 / 历史挂单成交,含 partial) | **进 NT 标准管道**(`generate_order_*` → ExecutionEngine → cache + topic + Strategy 回调);**不创建** `leg_settled` entry;若该方向已有 entry(以前 execution 过)则置 `settled=true`,否则跳过 |

**边界澄清**: `leg_settled` 不是 fill 事件的通用 sink,而是 **execution 启动后通讯通道是否存活的指示**。execution 没启动过的方向上发生 venue 端事件(WS push),走 NT 标准路径通知 Strategy 即可,不会凭空创建 `leg_settled` entry。这保证 entry 集合 = "曾经发起过 execution 的 (competition, direction)" 集合,边界清晰。

---

#### 6.8.3 OE adapter 健康检查(吸收原 OE 网页监控)

**两个刷新触发条件,正交并存**:

| 触发 | 条件 | 适用阶段 |
|---|---|---|
| **时间维度** | competition 页面在阈值内无任何赔率/订单更新 | 全程(执行前后都要,保证 strategy 看的赔率不冻) |
| **状态维度** | competition 某方向 `leg_settled=false` | 执行后对账兜底(tracking 漏消息时强制重新同步) |

**实施要点**:
- 阈值: 沿用现有 OE 网页监控的 **per-competition** 阈值(`orbitexch_staleness_timeout_sec`,Step 5 实施时 grep 原 `services/*/` 拿到具体值)
- **时间维度判定用 NT clock**(P1 复用 NT 时间控制): 每个 competition 记 `last_update_ns`(收到该场赔率/订单帧时写 `clock.timestamp_ns()`,**收帧只写变量,廉价**);**只在健康检查 tick 内**判 `clock.timestamp_ns() - last_update_ns > 阈值_ns` → stale。不用 wall-clock / `time.time()`
- **不做立即刷新 / 不起独立 staleness 轮询循环**(2026-05-19 用户拍板): staleness 仅在健康检查 tick 评估,**不**为提早发现而起 watchdog 或独立快轮询。
  - **检测延迟 = 阈值 + 最多一个 `health_check_interval_sec`**(因为只在 tick 扫),用户已确认可接受(不需要立即刷新)
  - 含义: 应让 `health_check_interval_sec` ≤ 旧 `_staleness_check_interval`(30s)量级,否则 stale 发现太慢;Step 5 实施时一并确认两个值的关系
- 刷新动作: 页面 reload → 等待重新订阅完成 → 拉一次持仓/挂单 → 标记所有方向 `settled=true`
- **健康检查不碰余额**(2026-05-19 Q17):OE 余额走 WS 被动推(reactive,已含挂单占用),WS 活着即新;健康检查的职责是**保证 WS/页面活着**,WS 死了 reload/重连后 WS 自然重推余额。**不在健康检查里拉余额**(OE 也无 REST 可拉)。
- **数据回写走 NT 标准 report 通路**(对齐 §6.8.4 PM,Q-L 翻盘后保持一致):
  - 持仓差异 → `generate_position_status_report(report)`
  - 挂单差异 → `generate_order_status_report(report)`
  - 两条均由 ExecutionEngine reconcile / Portfolio 自动接管,不绕过任何 NT 标准路径(避免 §6.8.4 列出的 4 项 (b) 隐藏代价)
  - 与 PM 的唯一区别只是**触发源**:OE 是页面 reload 后从 DOM/WS 帧采集,PM 是 REST 主动拉取
- 删除: 原 `services/*` 下的 OE 网页监控并入 `OrbitExchDataClient`(或独立 adapter 内部 Actor)

---

#### 6.8.4 PM adapter 健康检查

**触发**:
- 默认周期(沿用现有逻辑)
- 等待周期期间,**外部消息可立即触发**(跳过当次周期等待)

**动作**:
- 拉 持仓 / 挂单(**不拉余额**,见下)
- 数据处理: **走 NT 标准 report 通路**(2026-05-19 修正,Q-L 翻盘为 (a))
  - 持仓差异 → `generate_position_status_report(report)` → ExecutionEngine.reconcile_execution_report
  - 挂单差异 → `generate_order_status_report(report)` → ExecutionEngine.reconcile_execution_report
- **健康检查不碰余额**(2026-05-19 Q17):PM 余额完全靠上游事件(连接时 + 链上成交确认 `POLYMARKET_FINALIZED_TRADE_STATUSES`)刷新,健康检查不顺手拉。可用余额由 `_check_balance` 用 `total − Σ(cache.orders_open 在途名义额)` 自算(§5.6 Q17);挂单本身由本健康检查的 order report 通路保持新,故 available 始终准。
- ExecutionEngine 在 reconcile 内部:
  - 推进 Order 状态机 / 写 `cache.orders` / 发 `events.order.{strategy_id}` topic
  - 派生 Position / 写 `cache.positions` / 发 `events.position.{strategy_id}` topic
  - 通过 `Portfolio.update_order` / `Portfolio.update_position` endpoint 触发 Portfolio 重算 PnL / margin
- 拉取成功后,PM 侧涉及的所有 competition 所有方向 `settled=true`

**merge / claim 并入本健康检查 tick(Q18 修正,2026-05-21)**:
- 拉 `/positions` 时同时取**原始响应**的 `redeemable` / `mergeable` / `neg_risk`(NT `PositionStatusReport` 丢了这些,所以 merge/redeem 直接用 raw 响应,不读 cache)。
- 复用同一次拉取,**不再独立调度**(原 Q18"独立 `PolymarketSettlementActor` + 自带周期"作废):按 condition_id 分组 → merge(≥2 outcome 持仓,amount=min)/ redeem(`redeemable=true`),调 `contract.py:PolymarketContractService`(保留作 IO)。
- **merge/redeem 的 `TxResult` 不作为健康判定依据**(用户拍板 2026-05-21):失败仅 log + 下次 tick 重试(幂等),**不影响** `venue_connected` / `leg_settled`。
- redeem 的"结算滞后"由健康检查的周期性天然兜住(每 tick 检查 `redeemable`)。详见 §5.8。

**为什么从 (b) 私有覆盖翻回 (a) NT 标准 report**(2026-05-19 修正):

(b) 私有覆盖的隐藏代价(上一轮讨论中暴露,见 NT 订单/仓位追踪路径图):
- ❌ Order 对象状态机不被 `_apply_event_to_order` 推进 → cache 里的 Order 字段可能与 venue 真实状态不一致
- ❌ Position 不经 `_handle_position_update` 派生 → `PositionOpened/Changed/Closed` 事件不发
- ❌ Portfolio 收不到 `update_order / update_position` endpoint → unrealized PnL / margin / balance 计算与 venue 偏离
- ❌ Strategy 收不到 `events.order.* / events.position.*` topic → 退化到主动轮询 cache,违背 NT 事件驱动语义

(a) NT 标准 report 通路的代价:
- ⚠️ 多一跳 (report → reconcile → cache + 事件 publish),性能可忽略(健康检查频率远低于 venue 推送)
- ⚠️ 需要把 raw API 响应翻译成 `PositionStatusReport` / `OrderStatusReport`(上游 PM `nautilus_trader/adapters/polymarket/execution.py:289+` 已经有现成实现:`_generate_order_status_reports` / `_generate_position_status_reports`,Step 5 实施时直接复用)

权衡: NT 一致性 + Portfolio 准确性 > 一跳的性能成本。P1 (NT 原语优先) 要求选 (a)。

---

#### 6.8.4.5 健康检查循环节奏控制(OE / PM 共用,2026-05-19 锁定;P1 复用 NT 时间控制)

**不自造 `asyncio.Event + monotonic()` 循环,改用 NT `Clock` 的一次性 time-alert 自重排模式**(P1 NT 原语优先)。NT `LiveClock`(`common/component.pyx:807`)持有 asyncio loop,timer 回调在 event loop 上 fire,组件 stop 时自动 `cancel_timers()`。

**NT 时间控制 API 映射**(替代原自造方案):

| 原自造 | NT 原生 |
|---|---|
| `_next_check_at: float`(monotonic) | `clock.next_time_ns(timer_name)`(查询下次 fire 时间) |
| `time.monotonic()` | `clock.timestamp_ns()` |
| `asyncio.wait_for(event, timeout)` | `clock.set_time_alert_ns(name, now_ns + interval_ns, callback=_on_health_tick)` |
| `_trigger_event.set()` 立即唤醒 | `clock.set_time_alert(name, <now 或过去>, override=True)` → 过去/当下时间立即 fire(`component.pyx:333`) |
| 每轮结束重置时间 | callback 末尾再 `set_time_alert_ns(name, now_ns + 当前interval, override=True)` 重排 |
| 关停手动 cancel | NT 组件 `stop()` 自动 `cancel_timers()` |

**循环节奏(自重排 one-shot alert)**:

```python
TIMER = "health_check"   # per-venue 唯一名,如 "health_check_orbitexch"

def on_start(self):
    self._schedule_next()    # 首次排程(initial check 已在启动序列单独跑)

def _schedule_next(self):
    interval_ns = secs_to_nanos(self._config.health_check_interval_sec)  # 每次读当前 config
    self._clock.set_time_alert_ns(
        name=TIMER,
        alert_time_ns=self._clock.timestamp_ns() + interval_ns,
        callback=self._on_health_tick,
        override=True,
    )

def _on_health_tick(self, event: TimeEvent):
    try:
        self._run_health_check()     # OE: 刷页+对账;PM: 拉持仓/挂单/余额+对账
    finally:
        self._schedule_next()        # 不论成功/异常都重排下次(finally 保证)

def trigger_health_check(self):
    # 立即触发:重设 alert 到当下 → NT 立刻 fire(past/now → immediate)
    self._clock.set_time_alert_ns(
        name=TIMER,
        alert_time_ns=self._clock.timestamp_ns(),
        callback=self._on_health_tick,
        override=True,
    )
```

**关键约束**(语义不变,落到 NT 原语上):

| 约束 | NT 实现 |
|---|---|
| **每轮结束**按当前 config 重排 | `_schedule_next()` 在 callback 的 `finally` 里,每次读 `health_check_interval_sec` → 运行时改 interval 下一轮即时生效 |
| 异常也重排 | `try/finally` 保证;NT timer 回调内异常不影响下次排程 |
| 时钟单调 | `clock.timestamp_ns()`(NT 统一时钟,backtest 亦兼容) |
| trigger 立即唤醒 | `set_time_alert_ns(now, override=True)` —— 满足 §6.8.4 PM "外部消息可立即触发" |
| 不重叠 | one-shot 自重排:下次只在本次 callback 完成后才排,天然不并发 |
| **不实现** block / unblock | 见下;P6 不超前实现 |

**status 查询接口**(给 WebGatewayActor / Debug 用):

```python
def get_next_check_at_ns(self) -> int:
    """下次健康检查的 NT clock 时间(ns)。0 = 无排程(NT next_time_ns 约定)。"""
    return self._clock.next_time_ns("health_check")
```

**不实现 block / unblock 机制**(对应原 `service.py:204-217` 死代码):grep 全工程零调用、前端零消费,P6 不超前实现下新架构不继承。若未来真有"执行期暂停健康检查"场景,NT 原语下实现也简单(`cancel_timer(name)` 暂停 + `set_time_alert` 恢复),不需要预留标志位。

**旧代码清理(Step 5/6 实施时)**:

`services/risk/service.py` 中以下符号在新架构里**不存在**,迁移收尾时一并删除:

| 旧符号 | 处置 |
|---|---|
| `block_health_check()` / `unblock_health_check()` / `is_health_check_blocked()` / `_health_check_blocked` / status `"blocked"` 字段 | **删除**(死预留,零调用) |
| `_health_check_loop()`(asyncio while 循环)/ `_next_health_check_at`(monotonic)/ `_health_check_event`(asyncio.Event) | **删除**,由 NT `clock.set_time_alert_ns` 自重排取代 |
| `trigger_health_check()` / `get_next_health_check_at()` | **保留语义**,重写为 NT clock 版(见上) |

---

#### 6.8.5 Execution session 单一职责 + tracking 超时(Q15,2026-05-19 锁定)

**契约**: execution 内部不做决策,一次 session 单一职责。两种 session **共用同一全局超时配置**;超时即结束 session,**不做任何补救动作**。

| Session 类型 | 触发条件 | 动作 | session 结束条件 |
|---|---|---|---|
| **cancel-only** | strategy 调 submit 时,该 instrument 上有**残留挂单**(原 "stale orders" 措辞修正) | 撤掉残留挂单(**丢弃**当次传入的 submit) | venue 推 CANCELED **或** 超时 |
| **submit+track** | strategy 调 submit 时,无残留挂单 | 下单 → 等成交 | venue 推 terminal(FILLED / CANCELED / REJECTED / EXPIRED) **或** 超时 |

**两种 session 都 track 到 terminal 或 timeout 二选一**(不分种类的契约统一)。

**超时机制**(Q-T 四项已拍板,2026-05-19):

| 维度 | 决定 |
|---|---|
| **超时触发动作** | session **直接结束**,**不**自动发起撤单 / 重试 / 任何补救;order 在 venue 端保持当时状态(`ACCEPTED` / `PARTIALLY_FILLED` 等),由 strategy 后续轮次决定怎么处理 |
| **配置位置** | **全局唯一**超时值(per-venue 不分),沿用现工程的配置方式;Step 5 实施时 grep 原 `services/execution/` 拿默认值 |
| **timer 模式** | **绝对超时**,从 session 启动时刻起算;partial fill / OrderAccepted **不重置** timer |
| **cancel session 是否也用同一值** | **是**,两类 session 共用同一超时配置,不单独设置 |

**用 NT `Clock` 一次性 time-alert 实现**(P1 复用 NT 时间控制,不自造 `asyncio.wait_for` 超时):

```python
def _start_session(self, client_order_id):
    timeout_ns = secs_to_nanos(self._config.session_timeout_sec)
    timer = f"exec_timeout_{client_order_id}"           # per-session 唯一名
    self._clock.set_time_alert_ns(
        name=timer,
        alert_time_ns=self._clock.timestamp_ns() + timeout_ns,  # 绝对超时,从启动起算
        callback=self._on_session_timeout,
    )

def _on_order_terminal(self, client_order_id):
    # 收到 terminal(FILLED 全成 / CANCELED / REJECTED / EXPIRED)→ 取消超时 alert
    self._clock.cancel_timer(f"exec_timeout_{client_order_id}")
    # session 正常结束

def _on_session_timeout(self, event: TimeEvent):
    # 超时 fire:session 结束,不做任何补救;cancel session 超时仅 log warning
    self._log.warning(f"Session timeout: {event.name}")
    # 不调 cancel / submit;order 在 venue 保持当时状态,留给 strategy 下一轮
```

- **绝对超时**靠 alert 一次性设在 `submit_ts + timeout`,partial / accepted 事件**不重设** alert(天然不重置)
- **terminal 抢先**靠收到终态时 `cancel_timer` 取消 alert(对应"完全成交不等超时")
- **关停**靠 NT 组件 `stop()` 自动 `cancel_timers()`,无悬挂 timer
- per-session timer 名用 `client_order_id` 保证唯一,避免并发 session 互相干扰

**超时后的状态自洽**:

1. **`leg_settled` 不受超时影响** —— 超时前若收到任何 venue 确认事件(99% 场景:至少有 `OrderAccepted`),settled 已为 true;若极端"submit 后零响应",超时时 settled 仍为 false,**这才是健康检查兜底刷新的工作**(§6.8.3 / §6.8.4),不是 execution 的工作
2. **order 在 cache / venue 的状态可能仍是 non-terminal**(如 `PARTIALLY_FILLED`);**strategy 下一轮**通过读 cache.order 自然感知;若 strategy 决定撤,**触发新一轮 submit 时 §6.8.5 残留检测会自然走 cancel-only session**,流程闭合
3. **cancel session 超时** —— cancel 命令已发但 venue 未推 CANCELED 即超时;session 结束,`leg_settled` 维持当前值,**仅 log warning**,不再升级动作(避免无限循环);下一轮 strategy 提交时若残留挂单还在,会再次进入 cancel-only session

**关键约束**:
- cancel-only session 中,当次传入的 submit **不进队列、不延后下发、直接丢弃**
- strategy 必须**每次循环都全量重算意图**,不能假设"我上次发的 A 还在排队"
- strategy 下一次循环重新评估行情后自行重发(可能发的不是 A 而是 B,因为行情变了)
- **execution 唯一的"决策"只有 watchdog(超时即停),策略性的补救/撤后重发/跨腿对冲全归 strategy**

**移除**:
- execution 内部 recovery loop(re-plan / retry)
- 接受窗口风险: 短期可能出现"裸单飘着没人管"的间隙;strategy 后议补救(关联 `bug_compensating_cancel_missing`)

**与健康检查互斥的钩子(Q19,§6.10)**:
- session **submit 时** publish `execution.started`、置 `_execution_active`(首个 await 前同步)
- session **terminal 或 timeout 时** publish `execution.finished`、清 `_execution_active`(放 finally,terminal 与 timeout 两条路径都要清,避免泄漏导致健康检查永久饿死)

---

#### 6.8.6 显式 defer(进 §7 / TODO 区)

以下事项已浮现但留待后续轮次:

| 事项 | 关联 | 状态 |
|---|---|---|
| 非执行期间收到的 WS 消息怎么入 cache | Step 2 / Step 5 | ✅ **NT 原生路径解决**: WS 监听完全在 adapter 内部,无论执行期 / 非执行期收到的帧都走 `_handle_data` / `generate_*` 接口入 NT 标准管道,不分阶段 |
| `way_rebate` 计算的触发时机 | Strategy 设计 / Portfolio | ✅ **§6.9 解决**: 由 `ArbitragePortfolio.way_rebate(pair_id)` 提供 pull-based 查询(对应 NT `unrealized_pnl(instrument_id)` 风格),strategy 决策点 / WebGateway HTTP 请求按需调用;无独立触发器,无需订阅事件 |
| Strategy 端补救 / 撤后再下 / 跨腿对冲 | Step 4 | ⏳ defer 到 Step 4 |
| 裸单飘着的窗口(Q-F) | Step 4 strategy 设计 | ⏳ defer 到 Step 4 |
| `bug_compensating_cancel_missing` 闭环 | Step 4 strategy 设计 | ⏳ defer 到 Step 4 |

---

### 6.9 ArbitragePortfolio: way_rebate 等领域指标(Q14,2026-05-19 锁定)

#### 6.9.1 背景与定位

NT `Portfolio`(`portfolio/portfolio.pyx:97`)自带 `unrealized_pnl` / `realized_pnl` / `net_exposure` / `net_exposures` / `equity` / `margins_*` 等指标,但**没有 way_rebate**(套利领域专属:按 outcome 枚举结算分支的 payoff 函数,**不依赖 mark price**)。

讨论确认(2026-05-19):
- way_rebate 是 **per-pair (per-competition) 的衍生指标**,语义上**与 NT `unrealized_pnl` 并列扩展**,不是替代
- NT 已经在 `unrealized_pnl(instrument_id)` 单数 / `unrealized_pnls(venue=, ...)` 复数双层 API 上做出了"单一标的 vs 聚合"的范式;way_rebate 用**完全镜像的范式**(`way_rebate(pair_id)` 单 pair / `way_rebates_by_venue(pair_id)` per-pair-per-venue),只是聚合 key 从 `venue/instrument_id` 改成 `pair_id`(从 `instrument.info["competition"]` 抽,Q9 已锁的统一字段)
- 实现方式选 **(i) 子类化 `ArbitragePortfolio(Portfolio)` + kernel swap**,理由见 §6.9.4

---

#### 6.9.2 way_rebate 算法定义(平移自 `services/risk/position.py`)

对一场比赛(`pair_id`)的所有腿(`legs`,可能跨 venue 跨方向):

```
way_rebate[outcome] = (
    sum(leg.profit_if_wins() for leg in legs if leg.market_type == outcome)
    - sum(leg.loss_if_loses() for leg in legs if leg.market_type != outcome)
) / share
```

其中:
- `profit_if_wins`: PM 端 = `size * (1 - price)`;OE 端 = `size * (price - 1) * fx`
- `loss_if_loses`: PM 端 = `size * price`;OE 端 = `size * fx`
- `share`: 基准金额(默认 100;面板参数,可调)
- `outcome` ∈ {`home`, `draw`, `away`},`draw` 仅当任一腿 `market_type == "draw"` 时计入

算法不依赖任何 mark price / 当前赔率,只依赖**成交时落库的 size / price / fx**。

---

#### 6.9.3 API 表面(对照 NT 现有方法)

```python
# src/arbitrage/portfolio_ext/arbitrage_portfolio.py
from nautilus_trader.portfolio.portfolio import Portfolio


class ArbitragePortfolio(Portfolio):
    """
    扩展 NT Portfolio,加 betting/arbitrage 领域专属指标。
    
    与 NT 内置指标并列;不覆盖父类任何 cpdef/cdef 方法(Cython 子类化约束)。
    """

    # ─── per-pair 标量(对应 unrealized_pnl) ─────────────────────────

    def way_rebate(
        self,
        pair_id: str,
        account_id: AccountId | None = None,
    ) -> dict[str, float]:
        """
        Return conditional payoff rates per outcome for one competition.

        Settled gate (2026-05-19):
        - 若 `leg_settled[pair_id]` entry 存在且**任一方向 false** → 返回 `{}`(不可信,strategy 自然放弃)
        - 若 entry 不存在(从未 execution 过此 pair)→ **正常计算**(无 execution-staleness 风险)
        - 若 entry 存在且所有方向 true → 正常计算

        Returns
        -------
        dict[str, float]
            {outcome: net_payoff / share}, e.g. {"home": 0.10, "away": 0.35}
            空 dict 表示:(1) 该 pair 无持仓,或 (2) settled gate 失败
        """

    def min_way_rebate(
        self,
        pair_id: str,
        account_id: AccountId | None = None,
    ) -> float | None:
        """
        Return the lowest outcome's rebate (used as conservative floor).
        
        Settled gate: 同 `way_rebate`,gate 失败返回 `None`。
        """

    # ─── per-pair-per-venue 嵌套(对应 net_exposures 风格) ─────────

    def way_rebates_by_venue(
        self,
        pair_id: str,
        account_id: AccountId | None = None,
    ) -> dict[Venue, dict[str, float]]:
        """
        Return per-venue per-outcome payoff rates for one competition.
        
        Settled gate: 同 `way_rebate`,gate 失败返回 `{}`。
        """

    # ─── 全账户聚合(对应 equity) ───────────────────────────────────

    def global_min_rebate_sum(
        self,
        account_id: AccountId | None = None,
    ) -> float | None:
        """
        Sum of min_way_rebate across all active pairs. 用于循环熔断阈值判断。

        **扫描范围 = 只有持仓的 pair(active pairs)**:遍历 cache 中有 open position
        的 pair(对应旧 `position.py:373` 遍历 `self._positions.values()`)。
        **没下过单 / 无持仓的比赛不进遍历**,不会触发 None(否则日程表里只要有未交易
        比赛 global 就恒为 None,把系统焊死)。

        Settled gate (fail-closed, Q-G3) —— 仅对 active pair 判:
        - 某 active pair 有 leg_settled entry 且**任一方向 false** → 返回 `None`(全局判断作废,不返回部分和)
        - active pair **无 entry**(历史导入等非 execution 持仓)→ 按 Q-G1 放行,正常计入其 min_way_rebate
        - 所有 active pair 都 settled(或无 entry)→ 正常累加返回 float
        """

    # ─── 当前比赛的所有 legs(辅助方法,不对外暴露指标) ─────────

    def _legs_for_pair(self, pair_id: str, account_id) -> list[PositionLeg]:
        """
        从 cache 拉所有 instrument 满足 `instrument.info["competition"] == pair_id` 的开仓位置,
        转成内部 PositionLeg(包含 size / price / venue / market_type / fx)。
        """
```

**纯 Python 方法**(不是 `cpdef`)—— 这是 Cython 子类化约束:子类化 `cdef class` 时**只能加 Python 方法,不能加 `cpdef`/`cdef`**。性能上无问题(way_rebate 计算量小,纯 Python 足够)。

---

#### 6.9.4 接线: kernel swap

NT `system/kernel.py:359` 硬编码了 `Portfolio(...)`,没有 factory 注入点。我们的做法:

```python
# src/arbitrage/launcher.py 或 main.py 中
from nautilus_trader.live.node import TradingNode
from nautilus_trader.portfolio.portfolio import Portfolio
from src.arbitrage.portfolio_ext.arbitrage_portfolio import ArbitragePortfolio


def build_node(config) -> TradingNode:
    node = TradingNode(config=config)
    _swap_portfolio(node)
    return node


def _swap_portfolio(node: TradingNode) -> None:
    """把 kernel 内置 Portfolio 实例换成 ArbitragePortfolio。"""
    kernel = node.kernel
    old = kernel._portfolio
    new = ArbitragePortfolio(
        msgbus=kernel.msgbus,
        cache=kernel.cache,
        clock=kernel.clock,
        config=kernel.config.portfolio,
    )
    # 解除旧实例的 msgbus endpoint(以防 NT 升级后内部还引用)
    kernel.msgbus.deregister(endpoint="Portfolio.update_account", handler=old.update_account)
    kernel.msgbus.deregister(endpoint="Portfolio.update_order", handler=old.update_order)
    kernel.msgbus.deregister(endpoint="Portfolio.update_position", handler=old.update_position)
    # 新实例在 __init__ 内自动 register 三个 endpoint
    kernel._portfolio = new
```

**风险**: 触到 `kernel._portfolio` 私有属性。NT 升级时若改名需要同步改 launcher。每次 NT upstream merge 后跑一次 `pm-adapter-5.x` 系列即可发现。

**为什么不选其它方案**:
- (ii) Monkey-patch `Portfolio.way_rebate = ...` —— 全局污染、不适合多策略并存、Cython class 加 Python 方法可行但破坏 IDE 类型推断
- (iii) 纯 helper `compute_way_rebate(cache, pair_id)` —— 最低侵入但失去"Portfolio 是衍生指标家"的语义,Strategy / WebGateway 各自调用工具函数,接口分散

---

#### 6.9.5 pair_id 解析约定

聚合 key 通过 instrument 的统一 info 字段拿(Q9 已锁):

```python
def _resolve_pair_id(self, position: Position) -> str | None:
    instrument = self._cache.instrument(position.instrument_id)
    if instrument is None or instrument.info is None:
        return None
    return instrument.info.get("competition")
```

PM 和 OE 的 Provider 在 Step 1 必须保证 `instrument.info["competition"]` 同一比赛跨 venue 取同一字符串值,这一点已在 §6.4 / Q9 锁定。

---

#### 6.9.6 何时调用

**纯 pull-based,无独立触发器**:

| 调用方 | 时机 | 用途 |
|---|---|---|
| `ArbitrageStrategy` | 评估机会时(每个 MatchedPair 事件后) | 读 `min_way_rebate(pair_id)` 与 `_get_min_rebate_rate()` 比较,决定是否值得做 |
| `WebGatewayActor` | 收到前端 HTTP `GET /positions/{pair_id}` | 即时算 `way_rebate / way_rebates_by_venue` 序列化推 JSON |
| `ArbitrageRiskEngine._check_rebate_gates` | 每次 `submit_order` 经 RiskEngine 拦截时(逐 submit) | 读 `global_min_rebate_sum()` / `min_way_rebate()` / `way_rebate()` 与 tp/sl/global_sl 阈值比较,触线 deny(Q16,§5.6) |

**不需要订阅 `events.position.*` 主动重算**:way_rebate 是位置数据的纯函数,position 一改 cache 就最新,调用即重算,无需缓存中间结果。这与 NT `unrealized_pnl` 设计一致(`portfolio.pyx:1307-1316` 表明:有 price 参数 = fresh calc 不入缓存,无 price = 用 cached PnL 或现算)。

---

#### 6.9.7 与 §6.6 Debug 框架的关系

Debug 子类化机制可叠加:`DebugArbitragePortfolio(ArbitragePortfolio)` 可覆盖 `way_rebate` 等方法以注入 overrides(例如 mock_position 强制某个 pair 的 way_rebate),供回归测试 / 模拟实盘场景用。具体 Debug hook 表 §6.6 加。

---

### 6.10 组件间同步:健康检查 ⊥ 执行(Q19,2026-05-21 锁定)

> **横切协议(2026-05-21 由 §6.8.7 提升为独立章节)**:这不是健康检查的内部细节,而是 **strategy ⊥ 健康检查 ⊥ 执行 三方协调协议**。**四个组件共同实现同一份契约**:strategy(§5.4)、OE 健康检查(`OrbitExchDataClient`,§6.8.3)、PM 健康检查(`PolymarketExecutionClient` 子类,§6.8.4)、execution session(§6.8.5)。任一组件的实现者都应以本章为准。

**问题**: 健康检查(OE 页面 reload / PM report 拉取 + merge/redeem)与订单执行(submit + tracking)若同时跑会互相破坏 —— OE reload 冲掉执行页面、merge/redeem 改链上持仓时执行在飞、report reconcile 与 tracking 抢 `leg_settled`。

**锁定方案: 全局互斥(最粗粒度,用户拍板)** —— 不分 venue、不分 competition:**任一健康检查 tick 在跑 → strategy 放弃所有机会;任一执行在飞 → 所有健康检查推迟**。OE / PM 都参与。

**两个全局状态**(单一 asyncio event loop,无需锁,见下):
- `_health_check_active`(任一 venue 健康检查 tick 进行中)
- `_execution_active`(一次执行 session 从 submit 到 terminal/timeout 进行中)

**消息(NT msgbus,前后都发,用户要求"让对方知道")**:
- 健康检查:tick 开始(首个 await 前)publish `health_check.started`,finally publish `health_check.finished`
- 执行:submit 时 publish `execution.started`,session 到 terminal/timeout 时 publish `execution.finished`
- 各组件订阅对方的 started/finished 维护本地镜像(strategy 订 `health_check.*`;两个健康检查订 `execution.*`)
- **OE / PM 各维护各的健康检查、各发各的 `health_check.*`**(两套独立组件 / 独立 NT clock / 独立节奏,互不知对方);但因 Q19 是**全局**互斥,消费方把两路消息**并成一个全局状态**:`_health_check_active` 用 **ref-count**(`started`→++ / `finished`→--,`>0` 即视为"有健康检查在跑"),容许两 venue 健康检查并存(count 可达 2),strategy 只要 count>0 就放弃。**不是两个独立互斥域**。`_execution_active` 同理(执行 session 也可能并发,ref-count)。

**协议**:
- **执行同步前置(用户原话)** —— strategy 决策点新增 pre-check:`if _health_check_active: 放弃机会, early return`(不 submit)。**不是执行中途打断,而是健康检查期间根本不开新执行**(与 settled pre-check 并列,§5.4)
- **健康检查 tick 让路**:callback 开头 `if _execution_active: 跳过本 tick`(§6.8.4.5 的 finally 照常重排下次 alert,本 tick 不 reload / 不 reconcile / 不 merge-redeem)

**为什么单 loop 无需锁**(P1,NT 原语):NT `LiveClock` 回调、Actor handler、msgbus 派发**都在同一 asyncio event loop 串行**;只要"检查 + 置位 + publish"在**首个 `await` 之前同步完成**,就不存在交错——strategy 的 submit 决策回调与健康检查回调不会真正并行。`msgbus.publish` 同步派发,publish `health_check.started` 返回时 strategy 的镜像已置位。

**纪律(实施约束)**:置位必须在任何 `await` 之前同步做,清位放 `finally`;否则 await 窗口内可能漏判。

**机会/检测延迟代价(用户已接受)**:执行在飞时所有健康检查暂停(staleness 检测延迟 += 执行时长,上界 = §6.8.5 tracking timeout);健康检查跑时 strategy 放弃机会,下一轮(§6.8.4.5 重排后)重评。全局粒度换取实现最简 + 最安全。

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
| **Q13** | **健康检查归属 + execution 简化** | ✅ **已锁定(§6.8)**: 健康检查下沉到各 adapter —— OE 内含网页刷新(时间维度 + `leg_settled=false` 两触发并存),PM 内含周期拉持仓/挂单/余额(可被外部事件中断,**走 NT 标准 report 通路 `generate_position_status_report` / `generate_order_status_report`,经 ExecutionEngine reconcile 入 cache**)(Q-L 2026-05-19 从 (b) 翻盘为 (a),保证 Portfolio 一致性 + Strategy 收到 events.position/order 事件);引入两层状态 `venue_connected`(平台级)/ `leg_settled`(比赛级,len=方向数,与该比赛数据同 cache entry);execution 退化为单一职责 session(cancel-only 或 submit+track,都 track 到 terminal),**丢弃** cancel-only session 当次传入的 submit,strategy 必须每轮全量重算;移除 execution 内部 recovery loop,补救/撤后再下/跨腿对冲(关联 `bug_compensating_cancel_missing`)下放 Strategy 后议 | Step 2 / Step 4 / Step 5 |
| **Q14** | **way_rebate 等领域指标归属 + settled gate** | ✅ **已锁定(§6.9)**: 子类化 `ArbitragePortfolio(Portfolio)` 加 4 个 Python 方法(`way_rebate(pair_id)` / `way_rebates_by_venue(pair_id)` / `min_way_rebate(pair_id)` / `global_min_rebate_sum()`),与 NT `unrealized_pnl` 并列扩展,聚合 key = `instrument.info["competition"]`(Q9);算法平移自 `services/risk/position.py`,纯 pull-based 无触发器;接线 = 在 `launcher.py` 中 kernel swap `kernel._portfolio`。**Settled gate(2026-05-19)**: way_rebate 计算时检查 `leg_settled` —— entry 不存在 → 通过(Q-G1);任一方向 false → way_rebate/way_rebates_by_venue 返回 `{}`,min_way_rebate 返回 `None`(Q-G2);global_min_rebate_sum **fail-closed**,任一 pair 任一方向 false → 返回 `None`(Q-G3)。Strategy 调用 way_rebate 前**也要做 settled pre-check**,gate 失败放弃机会,与 Portfolio 内部 gate 双重防御 | Step 4 / Step 6 / Step 7 |
| **Q18** | **merge / claim(链上结算)集成进 NT** | ✅ **已锁定(2026-05-19,§5.8)**: merge/claim 是链上 CTF 操作,**上游 PM ExecutionClient 无对应物**(只包 CLOB),本工程自研保留。数据源 = **PM Data API `/positions` 原始**(NT cache 丢了 redeemable/mergeable)。merge=同 condition≥2 outcome 持仓合并(amount=min);redeem=redeemable 门控。`contract.py` 保留作 IO。**归属 Q18b 修正(2026-05-21)**:**不设独立 Actor**,merge/redeem **并入 PM 健康检查 tick**(§6.8.4,复用其 `/positions` 拉取避免双拉);`TxResult` **不作健康判据**(失败 log+下次重试);redeem 结算滞后由健康检查周期性兜住;`cleanup.py` 编排平移进健康检查路径。**Q18c 钉死宿主(2026-05-21)**:三层 = 宿主/触发 **PM `ExecutionClient` 薄子类**(唯一同时有 `generate_*_status_report`+钱包 creds+健康检查 tick)→ 编排 `PolymarketSettlement` 普通类(组合,非内联)→ IO `contract.py`;纯度取舍已知,用组合缓解 | Step 5 / §6.8.4 |
| **Q20** | **strategy 机会快照隔离** | ✅ **已锁定(2026-05-21,§5.4)**: 为避免订单规划+执行被新成交扰动,strategy 在机会评估开跑时取 **per-pair 快照**(冻订单簿所需值 + 持仓 + **way_rebate**——因不在 cache,取快照那刻调一次 `portfolio.way_rebate(pair)` 冻结果),全程用拷贝直到该次套利结束(双腿 terminal/timeout/放弃)再丢弃,下一轮重取新鲜快照。**安全闸(settled / Q19 健康检查互斥 / RiskEngine 余额)走 live 不冻**(要最新安全信号,用户选项 1)。**NT 无原生读隔离快照**(`Cache.snapshot_position` 是 netting 归档;`cache.order_book` 返回 live 引用)→ strategy 自建深拷贝。strategy 内部单一归属,**不单独成章**(P11) | Step 4 |
| **Q19** | **健康检查 ⊥ 执行 互斥粒度 + 机制** | ✅ **已锁定(2026-05-21,§6.10)**: **全局互斥**(用户拍板,最粗最简)——任一健康检查 tick 跑 → strategy 放弃所有机会;任一执行在飞 → 所有健康检查推迟(OE/PM 都参与)。机制 = NT msgbus 消息(`health_check.started/finished` / `execution.started/finished`,前后都发)+ 各组件本地镜像;**执行同步前置** = strategy 决策点 pre-check `if _health_check_active: 放弃 early return`(与 settled pre-check 并列);健康检查 tick 开头 `if _execution_active: 跳过`。**单 asyncio loop 串行 → 无需锁**(置位/publish 须在首个 await 前同步,清位放 finally)。代价:执行期健康检查暂停(staleness 延迟 += 执行时长,上界=tracking timeout)、健康检查期放弃机会,用户已接受 | Step 4 / Step 5 |
| **Q17** | **PM 余额刷新机制 + 挂单占用** | ✅ **已锁定(2026-05-19,§5.5 / §5.6)**: (1) **刷新机制**:上游 `_update_account_state` 是**事件驱动**(连接时 + 链上成交确认 `POLYMARKET_FINALIZED_TRADE_STATUSES`),**无周期 timer**;NT 也无默认 `QueryAccount` 轮询(全库仅反序列化处实例化);周期兜底归 §6.8.4 健康检查 tick。原文档"PM 主动周期 timer"措辞已纠正。(2) **挂单占用(按 venue 非对称)**:**PM** 上报 `reported=True/locked=0/free=total`(链上不托管未成交单),`CashAccount.apply()` 见 reported 清空 NT 自算 locked(cash.pyx:178-179)→ cache `free` 恒 total → `_check_balance` 自算 `total − Σ(PM cache.orders_open 在途名义额)`;**OE** WS 上报**已含挂单占用**(用户确认 2026-05-19)→ `_check_balance` **直接信 cache 余额不再减**(否则双重扣减)。`_check_balance` 内按 `order.instrument_id.venue` 分支。(3) **健康检查不碰余额**:PM 完全靠事件、OE 完全靠 WS;§6.8.3/§6.8.4 健康检查动作里余额已移除 | Step 5 / Step 6 |
| **Q16** | **止盈/止损/全局熔断(tp/sl/global_sl)归属** | ✅ **已锁定(2026-05-19,§5.4 / §5.6 / §6.9.6)**: 三门限全进 `ArbitrageRiskEngine._check_rebate_gates`,**逐 submit deny = 别开新仓**;**不**翻 NT `TradingState`、**不**起监测 Actor、**无频率**(执行靠 NT 逐 command 拦截,本就 per-submit)。撤单走另一命令通路不受影响,补偿撤单照常。理由:本系统 TP/SL 不是挂 venue 的价格止损单,唯一动作是"挡新单",而挡单正是 NT RiskEngine 本职;NT 的 bracket/STOP 等价格触发原语不适配 way_rebate 指标。分工:Strategy 判"值不值得做"(`min_rebate_rate` 正向门槛),Risk 判"触线一律不许开"(反向硬停),两者正交。`match_tp` 偏 strategy 性质但因动作纯挡单留 risk,实施别扭可回迁(不影响机制)。**数据源**:持 `ArbitragePortfolio` 引用调其方法(cache 不存 rebate,纯 pull)。**settled gate**:entry 不存在→放行(Q-G1);global `None`(任一 pair 一腿 false)→ **fail-closed deny**(拦新开仓,等健康检查 reconcile 自动放开,撤单不受影响无死锁) | Step 4 / Step 6 |
| **Q15** | **execution tracking 超时机制** | ✅ **已锁定(§6.8.5)**: 两类 session 共用同一**全局超时配置**(per-venue 不分),Step 5 实施时 grep 现工程默认值;**绝对超时**(从 session 启动起算,partial fill / OrderAccepted **不重置** timer);**超时即结束 session,不做任何补救动作**(不自动撤、不重试);order 在 venue 端保持当时状态由 strategy 下一轮通过读 cache 自然感知 + 触发残留检测进入 cancel-only session 闭环;cancel session 超时仅 log warning 不再升级。execution 唯一的"决策"是 watchdog;策略性补救全归 strategy | Step 4 / Step 5 |

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
| 2026-05-23 (#33) | **位置校准:venue-coupled 代码回 adapter 目录(用户审计触发,#29/#30/#31 偏离纠正)**:用户问"为什么落在 src/arbitrage,不是迁到 NT?"逼审,发现把"迁到 NT"误读成"代码搬到 nautilus_trader/ 目录"——其实是迁到 **NT 原语**(Actor/ExecutionClient/Portfolio/...)。P9 明文 venue 适配器是**唯一例外**可住 `nautilus_trader/adapters/<venue>/`,且 OE 整套已在那里(browser_manager/executor/scraper/data/websocket_handler/...)。#29 的"app→adapter 干净依赖"理由站不住,settlement 移动是 Q18c README 明确锁的位置,不应过度泛化。**校准移动**:`src/arbitrage/execution/polymarket.py` → `nautilus_trader/adapters/polymarket/arb_execution.py`(与上游 `execution.py` 同目录,新文件名避 upstream merge 冲突);`src/arbitrage/execution/orbitexch.py` → `nautilus_trader/adapters/orbitexch/execution.py`(OE 无上游);`src/arbitrage/discovery/oe_provider.py` → `nautilus_trader/adapters/orbitexch/providers.py`(按 §5.1 表 line 130/184);`src/arbitrage/execution/factories.py` 拆 → `adapters/polymarket/arb_factories.py` + `adapters/orbitexch/factories.py`(按 NT 约定 factory 与 client 同目录)。**留 src/arbitrage/**:session.py / health_check.py(跨 venue 共用)+ risk/settlement orchestration/discovery refresher+events/bootstrap/common(都是跨 venue app 代码,设计本就 src/arbitrage)。execution §3.1/§3.2/§7 + discovery §3.2/§7 + 各测试 import 全部回写;全 arb 套件 **101 passed, 27 skipped**(同移动前)。**教训**:遇到与设计文档冲突时,即使我"自认有把握"也要先确认是否误读基础前提(P9 是什么、约定既有 precedent 是什么),`design-docs` skill 的"有把握→以详细设计为准"前提是 *真有把握*。 |
| 2026-05-23 (#32) | **Step 1/2 Discovery 落地(slice 6:OE Provider + Refresher + InstrumentsRefreshed)**: 用户指出 execution 离线骨架虽全但无 strategy/discovery/matching 没法跑端到端,选先做 Discovery+Matching。**discovery 详细设计** 占位 37 行 → 标准 7 节 ~150 行(`architectures/discovery/architecture.md`)。**落地**:`src/arbitrage/discovery/{events.py,oe_provider.py,refresher.py,__init__.py}` —— `InstrumentsRefreshed`(@customdataclass 来自 `model.custom`,字段 venue/count + ts;to_dict/from_dict roundtrip)、`OrbitExchInstrumentProvider`(包 `OrbitExchScraper.discover_events`,每场 MatchEvent → 各方向 BettingInstrument,info 6-key:sport/competition/home_team/away_team/start_ts/selection_role;**start_ts 暂置 0**,scraper DOM 抽时间是 Step 1 TODO)、`InstrumentRefresher(Actor)`(每 venue 一个,NT clock 自重排同 §6.8.4.5 节奏,异常路径也重排不卡死;`on_save/on_load` 持久化 `refresh_interval`(损坏值/<min 安全夹住);msgbus `config.{venue}.refresh_interval` 命令运行时改值;Q4 失败/0 count **不 publish**)。`_RuntimeDeps` 注入 `provider + loop`(与 LiveExecutionClient 同模式,sync clock 回调 `loop.create_task` 异步 tick;不复用 NT `register_executor` 的线程池)。**测试** `tests/arbitrage/discovery/{test_events,test_oe_provider,test_refresher}.py` **20 passed**(3 PM provider skipped:需上游 creds);全 arb 套件 **101 passed, 27 skipped**。架构 §7 + discovery README 落地状态回写。**仍待**:scraper DOM 抽 start_ts、PM info 6-key post-processor(launcher 接线点)、双 venue Refresher 在 TradingNode 内并行(/live-test)。**下一步**:Matching(§5.3 占位 → 详细设计 + MarketMatchingActor + 异构归一算法平移)。 |
| 2026-05-23 (#31) | **Execution slice 5:Launcher 接线(ArbContext 注入通道 + 自定义 factory)**: NT `LiveExecClientFactory.create(loop, name, config, msgbus, cache, clock)` 签名固定,无法直接传我们的额外依赖(leg_settled / settlement / positions_fetcher / 间隔)。**方案**:`src.arbitrage.bootstrap` 加进程级 `ArbContext`(`prepare_arb_context` / `get_arb_context` / `reset_arb_context`),launcher 在 `node.build()` 前填好,`ArbPolymarketLiveExecClientFactory` / `ArbOrbitExchLiveExecClientFactory`(`src/arbitrage/execution/factories.py`)在 create 内读 context 构造 Arb*ExecutionClient。同 `install_arbitrage_engines` 的 import-替换思路,bootstrap 持共享态、NT 机制取用。**启动顺序** 7 步落入 execution §3.x:install → TradingNode → prepare_arb_context → add_exec_client_factory → build → wire_arbitrage_runtime → run。**漏调早失败**:context.leg_settled is None → `RuntimeError("prepare_arb_context")`(test_factories `test_*_factory_raises_when_context_unset`)。**wire 复用 context registry**:`wire_arbitrage_runtime` 不传 leg_settled 时优先用 `_arb_context.leg_settled`(execution / portfolio / risk 同一对象,test_bootstrap `test_wire_reuses_context_registry_when_none_passed`)。**踩坑修(测抓出)**:`PolymarketWebSocketAuth` 在 `common.credentials` 非 `websocket`;`PlaywrightBrowserManager.__init__` 无 `base_url` 参(只 browser_type/headless/user_data_dir)——离线 import 测早炸。**OE factory 全程走通**(stub context + PlaywrightBrowserManager 不真启浏览器),PM factory 因 PM 链上 creds 只测早失败保护(全调用 /live-test)。`test_factories.py` + 扩展 `test_bootstrap.py`,全 arb 套件 **81 passed**。execution doc 加 Launcher 接线段。**execution 离线骨架 + launcher 接线全部完成**;剩 = **全链路 /live-test**(真 ClobClient/Playwright 跑通 live seam:`_connect`/`_place_via_executor`/`positions_fetcher`/`CURRENT_BETS` item 待 populated 抓帧)。 |
| 2026-05-22 (#30) | **Execution slice 4:OE 执行客户端骨架 `src/arbitrage/execution/orbitexch.py:OrbitExchExecutionClient`**: `(ArbExecutionSessionMixin, LiveExecutionClient)` 自写。**比 PM 更可离线测**:super 只需 instrument_provider(+标准 NT 依赖),browser 仅 `_connect` 用 → 客户端可离线构造。**已测(离线)**:`oe_balance_to_account_balances`(WS 已净挂单→total=free,GBP,Q17)、`_on_general_frame` BALANCE→`generate_account_state` + 未知/null 忽略、`_modify_order`→拒绝(OE 不支持改单)、`_submit_order` session 门控(cancel-only 丢弃 / executor 失败→`generate_order_rejected` / 成功→`generate_order_accepted`,注入 fake `_place_via_executor`)。**live seam(NotImplementedError 显式标 /live-test)**:`_connect`(browser/executor/general WS 接线)、`_place_via_executor`(NT Order→executor 旧 Order:`order_models.Order` 的 market_id/selection_id 取自 instrument.info)、`_cancel_*`。**待 `CURRENT_BETS` populated 抓帧**:`_on_current_bets` item→事件 + `generate_*_status_reports`(现 stub 返 [] 让 client 可连)。`test_orbitexch_client.py`,全 arb 套件 **76 passed**。execution §3.2/§7 + OE README 回写。**execution 离线骨架完成**(settlement/leg_settled/OE 帧/session/健康检查 loop/PM 子类/OE 客户端);**剩**:launcher 接线(positions_fetcher / 注册 client / install_arbitrage_engines)+ 全链路 /live-test。 |
| 2026-05-22 (#29) | **Execution slice 3b:PM 薄子类落地 `src/arbitrage/execution/polymarket.py:ArbPolymarketExecutionClient`**: `(ArbExecutionSessionMixin, PolymarketExecutionClient)`(MRO 验过 mixin 在上游前 → 覆盖 `_send_order_event`/`_submit_order` 生效)。`__init__` 接 `_init_arb_session` + 建 `HealthCheckLoop`(PM `health_interval_secs` + `lambda:self._execution_active`)+ 持 `PolymarketSettlement`;`_submit_order`→`_begin_session`;`_cancel_residual_orders` 建 `CancelOrder`→上游 `_cancel_order`(补偿撤单放行);`_connect/_disconnect` 起停 health;`_run_health_check`(positions_fetcher→`pm_position_to_settlement`→`settlement.run` + `leg_settled.mark`)。**位置决策(改 §3.1)**:执行客户端放 **`src/arbitrage/execution/`(app)非 adapter 目录**——依赖方向 app→adapter(import 上游 PM 类),adapter 目录保持上游可合并(同 settlement 落位)。Data API /positions **注入** `positions_fetcher`(launcher 接,复用旧 `odds_client.fetch_positions`/`PolymarketPosition`),客户端解耦。**离线可测**:纯映射 `pm_position_to_settlement` + MRO(`test_polymarket_client.py`,全 arb 套件 69 passed);**集成(真 ClobClient/ws_auth、_submit/_run_health_check 接线、reconcile 走 NT report 通路、`order_version_mismatch` 消失)经 /live-test 验**。execution §3.1/§7 回写。**下一步**:OE 客户端(submit/cancel 包 executor + BALANCE→account_state;CURRENT_BETS item 待抓帧)+ launcher 接线(positions_fetcher / 注册 client)。 |
| 2026-05-22 (#28) | **Execution slice 3a:健康检查 loop 节奏落地 + OE/PM 间隔分别可设(用户要求)**: `src/arbitrage/execution/health_check.py:HealthCheckLoop`(OE/PM 共用,§4.3/§6.8.4.5)。NT clock 自重排:sync callback → `create_task(_tick)`;`_tick` async,`try` 跑 `run_check`(失败吞掉)+ `finally` 按**当前** interval 重排(异常路径也重排,不卡死);`trigger_now`/`stop`。**用户新要求(2026-05-22):OE、PM 健康检查间隔分别可设** → `interval_secs_provider` 为**每实例独立** callable、每次重排重读(PM/OE 各传各的配置 + 运行时改值即时生效);配置落位 PM ExecClient config / OE DataClient config 各自 `health_interval_secs` 字段(随客户端落地)。**纠**原 §4.2"全局唯一超时配置 per-venue 不分"仅指 session timeout;健康检查间隔本就分 venue,现明确分别可配。执行⊥健康检查互斥:`is_execution_active` callable(PM 同对象直读 `_execution_active`;OE 经 msgbus 订阅 `execution.*` ref-count)。**测试** `test_health_check.py` **10 passed**(含 `test_pm_oe_separate_intervals`、`test_interval_reread_each_reschedule` 直证分别可设 + 运行时重读)。execution §4.3 回写。全 arb 套件 66 passed。**下一步**:PM 薄子类把 session + HealthCheckLoop + settlement 接起来。 |
| 2026-05-22 (#27) | **Execution slice 2:共享 session 核心落地(`src/arbitrage/execution/session.py`)**: `ArbExecutionSessionMixin`(PM 子类 + OE 客户端共用,mixin 在 MRO 前)。**关键简化**:NT 所有 `generate_order_*` 都汇入 `_send_order_event(OrderEvent)`(cpdef,可覆盖)——故**只覆盖这一个漏斗**做 leg_settled 标记 + 终态检测,不用逐个覆盖五个 generate_*。机制:`_begin_session`(`cache.orders_open` 残留检测 → cancel-only:撤残留+`generate_order_rejected`丢弃当次 submit / submit+track:`arm(pair,instrument_id)`+publish `execution.started`+NT clock 绝对超时 alert)、`_send_order_event` 覆盖(venue 确认事件→`mark`;终态=全成[累计 last_qty≥qty]或 Canceled/Rejected/Expired→`_end_session`)、超时(terminal 抢先 `cancel_timer`;超时即结束不补救,§4.2)、`_execution_active`=在飞 session 数(ref-count,§6.10 健康检查让路)。撤残留是 venue IO → 子类覆盖 `_cancel_residual_orders`。leg_settled 用 #25 的 per-leg `arm`(新增 registry 方法)。**测试** `tests/arbitrage/execution/test_session.py` **8 passed**(submit+track/cancel-only/标记/全成终态/部分不终态/撤单终态/超时/ref-count;FakeCache+真实 OrderEvent+TestClock)。**bug(测试抓出)**:`ClientOrderId` 无 `.to_str()`,用 `.value`(运行期才炸)。execution §4.1/§4.2/§7 + 共用清单回写。全 arb 套件 56 passed。**下一步**:PM 薄子类(健康检查 loop + 接 session + settlement)。 |
| 2026-05-22 (#26) | **OE `general` 频道 WS 帧格式锁定(用户提供实测抓帧)**: 推翻 #20 的"格式待确认"。`general` WS(SockJS 下行 `a[...]`)**承载多类帧、按顶层 key 分型**:`{"BALANCE":{"balance":"37.49","avBalance":null}}`(`balance` 字符串、已含挂单占用)、`{"CURRENT_BETS":[...]}`(当前注单);上行订阅请求 `["{...subscribe...}"]` 无 `a`/无数据;未知 key 帧时不时收到 → 忽略。**实现**:`message_parser.parse_general_frame`(分型 + BALANCE 解析 + CURRENT_BETS 透传 + 未知忽略 + 健壮 null/字符串),旧 `parse_order_message` 变兼容委托。**测试**:`tests/arbitrage/adapters/orbitexch/test_ws_general_frames.py` **8 passed**(含 SockJS `a[...]` 全链路解包,样本为真实抓帧)。**仍待**:`CURRENT_BETS` 单 bet item 字段(抓到样本为空 `[]`)——工作假设与 REST `/customer/api/currentBets` `bets[]` 同源,待 populated 抓帧确认后实写 bet→OrderStatusReport 映射。execution §3.2 + OE README oe-adapter-5.ws.{1,2} 回写。 |
| 2026-05-22 (#25) | **leg_settled 腿键 = instrument_id(取代整数下标)+ 计划改 per-leg reset(用户确认)**: 建 session 核心时发现原 §4.4 `list[bool]` + 整数下标需要一份"方向→下标"映射,**无单一归属**(谁定 home/draw/away 的次序?且耦合到搁置的 strategy 腿提交)。改为 **`dict[pair_id, dict[instrument_id, bool]]`** —— 一个 instrument 天然就是一条腿、全局唯一,`generate_order_*` 事件直接带 `order.instrument_id`,零映射。`LegSettledRegistry` API 改 `reset(pair_id, instrument_ids)` / `mark(pair_id, instrument_id)`(消费侧 `any_unsettled` 不变,Portfolio 不受影响)。同步:risk 的 `test_leg_settled.py` + `test_portfolio.py` settled 用例改用 instrument_id(40 passed),risk README 6.9.13、execution §4.4 回写。**下一步 session 核心拟采 per-leg arm(某腿 submit+track 启动时只把该腿置 false,合并不整组),进一步去掉对 strategy 提交轮次边界的耦合**——对套利(每场腿集合稳定)等价于"整组重置",对单腿补发更精确;若要严格"整组"语义请提出。 |
| 2026-05-22 (#24) | **Step 5 Execution 启动 + settlement 编排落地(slice 1)**: 调研现状定基:上游 PM `PolymarketExecutionClient` 成熟(`_submit/_cancel_order`/`_update_account_state`/`generate_*_status_reports`/终态 `POLYMARKET_FINALIZED_TRADE_STATUSES`),PM 薄子类只需加健康检查 loop + settlement + leg_settled;OE `executor.py` 已有 Playwright `place_order`/`cancel_order`/`cancel_all_unmatched`/`get_current_bets`,submit/cancel + 报告(轮询)可做。**真实未知**:OE `general` 频道 WS 帧(订单状态 + 余额)格式——`message_parser.parse_order_message` 是 TODO、仓库无样本、帧仅 `_log.debug`;**用户将提供一份抓到的帧样本**,OE 订单回调 + 账户帧解析待样本到位再实写(其余 OE 部分不依赖)。**slice 1 落地**:`PolymarketSettlement`(平移 `cleanup.py:_do_cleanup`)落 `src/arbitrage/settlement/settlement.py`(**app 编排**,遵 settlement README 的 Q18c 落位;**纠**:初次误放 adapter 目录,移正——IO `contract.py` 留 adapter,编排归 app,PM 子类宿主 import app 编排),`run(positions)→SettlementResult`,失败吞 errors / 不抛 / 不作健康判据。`tests/arbitrage/settlement/test_settlement.py` 11 passed(FakeContract 不上链)。execution §4.6 + settlement README 回写。**构建顺序**:settlement(✅)→ 共享 session/timeout/leg_settled 核心 → PM 薄子类(健康检查 loop)→ OE 客户端(submit/cancel + 报告 + 帧 seam)。 |
| 2026-05-22 (#23) | **Step 6 Risk 落地 + 三处详细设计修正(编码核实触发,以详细设计为准回写)**: 写 `src/arbitrage/risk/{engine,portfolio,config}.py` + `common/leg_settled.py` + `bootstrap.py`,核实并修正初设/详细设计三处假设:(1) **基类 `RiskEngine`→`LiveRiskEngine`**:实盘 kernel 实例化 `LiveRiskEngine`(kernel.py:407),基类 `RiskEngine` 仅 backtest;`ArbitrageRiskEngine` 重命名 `ArbitrageLiveRiskEngine` 子类化 Live 版。(2) **接线 kernel swap→导入名替换**:`Portfolio`/`LiveRiskEngine` 都被 kernel 用模块级名硬编码构造、无注入点;原 §6.9.4 "构造后 deregister/reregister swap" 易碎(风控引擎还挂 4 处 msgbus + Trader ref),改为**构造 TradingNode 前替换 `nautilus_trader.system.kernel.Portfolio`/`.LiveRiskEngine`** → kernel 原生构造子类,零摘除/零重注册;领域参数(share/fx/三门限/leg_settled)经 `configure_arb` setter 注入(NT 固定实参表外)。覆盖 Portfolio + RiskEngine。(3) **`_check_order` 职责澄清**:NT `_check_order` 仅 price/quantity/GTD;notional/submit_rate/native 余额在 `_check_orders_risk_for_account`(本类不覆盖,父类原样跑)；native 余额对 PM 偏宽松(free==total),故 PM 自扣余额前置在 `_check_order`。自定义拒绝**必须自调 `_deny_order`**(父类见 False 仅 return)。**新增 `LegSettledRegistry`**(`common/leg_settled.py`):execution 写、portfolio/risk/strategy 读的横切共享契约对象(P11 无单一归属,语义真理源在 execution §4.4),settled gate 经它读;launcher 构造一份注入各方。**已 end-to-end 验证**:cpdef `_check_order` 子类覆盖被 `_handle_submit_order` 派发(覆盖触发 + deny 事件发出 + 订单不泄漏);way_rebate 公式对齐旧 position.py 示例(home=0.10/away=0.35)。改动:risk/architecture.md §1/§3.1/§3.2/§3.3/§3.4/§7;risk README + pytest 落地。**写 pytest(29 passed)又抓出两个 cdef 可见性 production bug(已修)**:① `Portfolio._cache` 是私有 cdef(非 readonly,异于 `RiskEngine._cache`)→ `ArbitragePortfolio` 覆盖 `__init__` 自存 `_arb_cache`;② `order.has_price_c()` 是 cdef 不可从 Python 子类方法调 → 改 `order.has_price`(property)。两者运行期才炸,印证落地测试价值。**仍待**:`skip_check_size` Debug 粒度(Step 6 Debug 子类)；`instrument.info` schema 由 discovery 落实;需全节点/真实 Position 的用例(risk-6.1/6.2/6.5/6.6 等)延后。 |
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
| 2026-05-19 | **Q13 锁定 (§6.8)**: 健康检查从独立服务下沉到各 adapter —— OE adapter 吸收原 OE 网页监控,两个刷新触发并存(**时间维度**: 阈值内无赔率/订单更新;**状态维度**: `leg_settled=false`);PM adapter 健康检查 = 默认周期 + 可被外部事件中断,动作 = 拉持仓/挂单/余额**直接覆盖 cache**(Q-L 选 (b),不走 NT report 通路)。引入两层 health 状态: 平台级 `venue_connected`(per venue, 启动 false)+ 比赛级 `leg_settled`(per competition,len=方向数,与该比赛数据同 cache entry,首次 execution 创建后不删除)。`leg_settled` 语义 = "venue 端这条腿的终态是否已确认进 cache"(不是"曾经 fill 过");tracking 收到 terminal(含 CANCELED)置 true,OE 刷新成功置 true,PM 拉取成功 PM 侧所有方向置 true。**Execution 简化**: session 单一职责 —— 进入时若有残留挂单走 cancel-only(**丢弃**当次 submit,strategy 下轮重算重发),否则走 submit+track,两者都 track 到 terminal;移除内部 recovery loop。补救 / 撤后再下 / 跨腿对冲(关联 `bug_compensating_cancel_missing` / Q-F 裸单窗口)显式 defer 到 Step 4 Strategy 设计。WS 非执行期消息入 cache 路径、way_rebate 触发时机也同步 defer。§5.2 OE DataClient / §5.5 PM+OE ExecutionClient 加注 Q13 边界引用;§7 新增 Q13 行;§6.8 全章新增。 |
| 2026-05-19 (#2) | **Q-L 翻盘 (b)→(a) + Q14 新锁(§6.9 ArbitragePortfolio)**: 讨论 NT 订单/仓位状态机后认识到 (b) "PM 健康检查直接覆盖 cache" 会绕过 `_apply_event_to_order` / `_handle_position_update` / Portfolio 三个 endpoint / `events.order|position.*` topic 四条 NT 标准路径,导致 Order 状态机不推进、Position 不派生、Portfolio PnL/margin 偏离、Strategy 退化轮询;翻盘为 (a) 走 NT 标准 report 通路(`generate_position_status_report` / `generate_order_status_report`),经 ExecutionEngine reconcile 入 cache,代价仅多一跳。§6.8.4 重写。**Q14 新增并锁定**: way_rebate 不做独立 Actor(过度设计),改为子类化 `ArbitragePortfolio(Portfolio)` 加 `way_rebate(pair_id)` / `way_rebates_by_venue(pair_id)` / `min_way_rebate(pair_id)` / `global_min_rebate_sum()` 四个纯 Python 方法,与 NT `unrealized_pnl` 并列扩展,聚合 key = `instrument.info["competition"]`(Q9),算法平移自 `services/risk/position.py`,纯 pull-based 调用即重算;接线 = `launcher.py` 中 kernel swap `kernel._portfolio`(NT 没给 Portfolio factory 注入点)。§6.9 全章新增。§6.8.6 defer 清单更新: `非执行期 WS 消息` 标 NT 原生解决(adapter 内 IO 不分阶段),`way_rebate 触发时机` 标 §6.9 解决。§7 加 Q14 行,Q13 行更新 Q-L 翻盘。 |
| 2026-05-19 (#3) | **§6.8.3 OE 健康检查刷新后数据回写显式走 NT report 通路(对齐 §6.8.4 PM Q-L 翻盘后),避免隐式路径歧义。§6.8.2 `leg_settled` 语义大修正(基于用户反馈)**: 从 "终态(完全成交/撤/拒)已对账" → **"execution 启动后通讯通道存活信号"**;`settled=true` 触发条件放宽到**任何**venue 确认事件(`OrderAccepted` / partial `OrderFilled` / full / `OrderCanceled` / `OrderRejected`)落地 cache;`settled=false` 真实含义 = "execution 启动但从未收到 venue 任何关于该腿的事件"(submit 没到 / WS 死),这才是健康检查兜底刷新的真实价值。新增"非 execution 触发事件无 entry 不创建"边界澄清。**Q15 新锁(§6.8.5 execution tracking 超时)**: 两类 session 共用同一全局超时配置(per-venue 不分,沿用现工程配置方式),绝对超时不重置 timer,超时即结束 session **不做任何补救动作**(不自动撤、不重试),order 在 venue 端保持当时状态由 strategy 下一轮通过读 cache + 残留检测自然处理;cancel session 超时仅 log warning。execution 唯一的"决策"是 watchdog,策略性补救全归 strategy。§7 加 Q15 行。OE/PM README 用例:OE `oe-adapter-5.health.{1,2,3}` 描述放宽到 "任何确认事件",新增 `.{4,5}` 覆盖 partial / OrderAccepted;PM `boundary.{1,2}` 重命名为 `health.{2,3}` 与 OE 对称,新增 `.{4,5}` 同上;Step 5 启动时再补 timeout 用例。 |
| 2026-05-19 (#4) | **§6.9 way_rebate 加 settled gate(Q-G1/G2/G3)**: way_rebate 四个方法增加门控 —— `leg_settled` entry 不存在(Q-G1) → 通过正常计算(无 execution-staleness 风险);entry 存在任一方向 false(Q-G2) → `way_rebate`/`way_rebates_by_venue` 返回 `{}`,`min_way_rebate` 返回 `None`;`global_min_rebate_sum` fail-closed(Q-G3),任一 pair 任一方向 false 即返回 `None`,不返回部分和。**Strategy 在调用 way_rebate 前也必须做 settled pre-check**(strategy-4.14),pre-check 失败时不调 way_rebate / 不发订单 / early return,与 Portfolio 内部 gate 双重防御(兜底 WebGateway 等其它调用方)。§6.9.3 四方法 docstring 加 settled gate 说明;§7 Q14 行更新;risk README 新增 `risk-6.9.{9,10,11,12}`;strategy README 新增 `strategy-4.14`。其它(无 cache 显式说明等)不动 —— 用户明确"其他不管"。 |
| 2026-05-19 (#5) | **§6.8.4.5 健康检查循环节奏控制(OE/PM 共用)新增**: 显式锁定状态变量(`_next_check_at` / `_trigger_event`)、循环节奏(每轮结束重置下次时间,读当前 config 支持运行时 interval 改动即时生效)、异常路径也重置(避免一次失败永久卡死)、`monotonic()` 时钟、`trigger_health_check` API 立即唤醒。**显式不实现 block/unblock 机制**(原 `services/risk/service.py:99,204-217,513` 死预留 —— grep 全工程零调用 / 前端零消费,P6 不超前实现下新架构不继承);旧符号 Step 5/6 实施时一并删除。OE README 新增 `oe-adapter-2.schedule.{1,2,3,4,5}` 共 5 个用例;PM README 对称新增 `pm-adapter-5.schedule.{1,2,3,4,5}` 共 5 个用例。 |
| 2026-05-21 (#22) | **启动详细设计阶段 + 重编排架构文档(用户决定)**: 删除旧微服务 `architecture.md`(顶层 + 6 service + 连字符版 execution-architecture.md);旧 md 全部视为过时不参考,详细设计**只从 refactor.md(本初设)推导**。新结构 = **按 NT 组件**:`docs/arbitrage/architecture.md`(端态总览 + 导航)+ `architectures/<组件>/architecture.md`(标准模板 7 节:职责/数据流mermaid/接口签名+消息接线+同步参与/算法/横切咬合/时序/落地清单)。**已写详细**:risk、execution、_cross-cutting/{synchronization,debug-injection}。**占位**(P7,概要未锁):discovery/data/matching;**搁置**:strategy(信号流水线框架待设计);**暂不迁移**:web。冲突约定:详细设计有把握→以详细为准并回写本表;没把握→讨论。下游:CLAUDE.md 索引加 refactor/总览/组件三行、`.claude/hooks-config.json` doc-sync 改为 `src/arbitrage/{cap}/ → architectures/{cap}/architecture.md`。refactor.md 保留为初设(本表续记)。 |
| 2026-05-21 (#21b) | **Q20 补:快照回收机制(用户问"会不会内存泄漏")**: 原 §5.4 只写"结束→丢弃",未钉死回收,有泄漏风险(pre-check 放弃 / 规划放弃 / timeout / 异常 / 长存 dict 漏 del)。补三条确定性释放:① 取快照在 cheap live pre-check **之后**(放弃在 pre-check 则不分配);② 单一释放点放 `finally`,绑 per-opportunity 上下文,覆盖所有出口(terminal/timeout/放弃/异常),复用 Q19"清位放 finally"纪律;③ 绑上下文而非长存 `self._snapshots` dict,作用域结束 GC 回收。上界:Q19 全局互斥 → 长命快照最多 1 份;纯内存重启即清。改动:§5.4 Q20 加回收小节;strategy README 加 `strategy-4.19`。 |
| 2026-05-21 (#21) | **Q20:strategy 机会快照隔离(用户新需求)**: 为避免订单规划+执行被新成交(改持仓/订单簿)扰动机会计算,strategy 在评估开跑时冻一份 **per-pair 快照**,该次套利全程用拷贝。**先核实 NT 无原生读隔离快照**(`Cache.snapshot_position` 是 netting flip 归档非读视图;`cache.order_book` 返回 live 引用,无 copy-on-read/MVCC)→ 自建深拷贝。冻:订单簿所需值 + 持仓 + **way_rebate**(用户指出不在 cache → 取快照那刻调一次 `portfolio.way_rebate(pair)` 冻结果,下游复用)。**安全闸(settled / Q19 健康检查互斥 / RiskEngine 余额)走 live 不冻**(用户选项 1,要最新安全信号;RiskEngine 本就独立读 live)。生命周期 = 评估开跑→规划+执行→双腿 terminal/timeout/放弃→丢弃,下轮重取(与 4.9 每轮重算一致)。strategy 内部单一归属,**不单独成章**(P11 验证通过)。改动:§5.4 加快照小节、§4.0 + §7 加 Q20;strategy README 加 `strategy-4.{17,18}`。 |
| 2026-05-21 (#20) | **清未闭环项:session/recovery 删除 + OE WS 现状审计**(用户收口): (1) **`session.py`/`recovery.py` 删除**(补救全归 strategy,不留旧外壳);`cleanup.py` 编排已平移 `PolymarketSettlement`(Q18c)。§5.4 拆解表更新。(2) **OE WS 审计(读 `websocket_handler`/`message_parser`/`data.py`)**:订单回调是**功能性 stub**(`parse_order_message` = `# TODO return None`、`_on_order_update` 只 log)→ Step 5 实写解析 + `generate_order_*`;余额帧**现代码没订**(WS 只订 prices/orders),现走 `scraper.get_balance()` 页面抓取——**用户确认 OE WS 确有余额帧(已含挂单占用),只是没抓** → Step 5 加第三类 WS 帧捕获 → `generate_account_state`。结论:§5.5/§5.6"OE 被动 WS"与 Q17"OE 信 WS 不再减"**前提成立**(WS 帧存在),无设计矛盾,仅现代码未实现。§5.5 两条审计项标 ✅ + Step 5 实写;OE README 新增 `oe-adapter-5.ws.{1,2}`。**Q11.2 RiskEngine bypass 粒度澄清**:指 Debug `skip_check_size` 能否只跳父类 min_quantity(测试便利),非生产 Risk 职责,生产不受影响。 |
| 2026-05-21 (#19) | **新增设计原则 P11「无自然归属的跨组件协议单独成章」+ 写进 hook(用户要求把"单独成章机制"固化)**: §2 加 P11。**主判据 = 有无单一自然归属**(一方是契约定义者、其余只消费的生产者/消费者或宿主/调用方不对称):有 → 放主方小节 + 交叉引用;没有(一群对等组件共维同一不变量)→ 单独成横切章节(§6.x)。**数量(≥3)降为辅助提示**——用户质疑"为何 ≥3 不 ≥2":≥2 会过度触发(几乎一切交互都涉 2 组件且多有生产者/消费者归属),而真正判据是"无主";≥3 只是无主情形的常见代理,2 个纯对等无主协议同样适用。反例 = 同步协议(4 方无主)曾误置 §6.8 后提升 §6.10(#18)。同时在 `test-sync-reminder` hook footer(`.claude/hooks-config.json`)加 P11 nudge(已随主判据修订同步),每次改 `docs/arbitrage/*.md` 自动提醒。权威定义在 P11(单一真理源),hook 只指针。已验证 JSON 合法 + dry-run 正确。 |
| 2026-05-21 (#18) | **§6.8.7 提升为独立章节 §6.10「组件间同步:健康检查 ⊥ 执行」(归类修正)**: 原 §6.8.7 挂在"§6.8 Health check + Execution 简化"下,但同步是 **strategy ⊥ 健康检查 ⊥ 执行 三方协调协议**(strategy / OE 健康检查 / PM 健康检查 / execution session 四组件共同实现同一契约),非健康检查内部细节。内容整体平移到 §6.10(章首加"四方共同契约"说明),不改实质。更新 6 处活引用(§4.0 Q19 行、§5.4 索引、§5.8×2、§6.8.5 钩子、§7 Q19 行)+ READMEs(settlement、strategy×2)→ §6.10;修订记录 #15/#17 与 §6.10 章首"由 §6.8.7 提升"作历史保留不动。 |
| 2026-05-21 (#17) | **新增 §4.0 全局推进总览(导航,非新设计)**: 7 个 Step 状态表(各自关联的横切 Q + 章节)+ Q1–Q19 一句话结论索引,作单一仪表盘,避免在 1700 行 / 17 条修订记录里翻找。同时把 §6.8.7 健康检查互斥消息语义写死:**OE/PM 各维护各的健康检查、各发各的 `health_check.*`,但因 Q19 全局互斥,消费方用 ref-count 并成一个全局 `_health_check_active`**(count>0 即放弃,容许两 venue 并存 count=2),非两个独立互斥域;`_execution_active` 同理。strategy README 4.15 验收补 ref-count。无组件行为变化。 |
| 2026-05-21 (#16) | **Q18c:钉死 merge/redeem 接口宿主 = PM `ExecutionClient` 薄子类(三层结构)**(用户追问"接口放哪实现"): §5.8 原写"PM 健康检查持(或注入)contract service"未指明宿主类。核实 `generate_*_status_report` 是 `PolymarketExecutionClient` 方法(execution.py:660/367)+ 钱包 creds 在 ExecutionClient + 健康检查住 ExecutionClient → 宿主唯一解 = **PM ExecutionClient 薄子类**。三层钉死:宿主/触发 = ExecutionClient 子类;编排 = `PolymarketSettlement` **普通类**(`src/arbitrage/settlement/settlement.py`,组合持有,平移 cleanup.py,**非内联**);IO = `contract.py`。**纯度取舍挑明**:Q18 一轮曾反对 merge/claim 进 ExecutionClient,Q18b 折叠后实际回到 ExecutionClient,用"组合独立类而非内联"缓解。改动:§5.8 加三层表 + 纯度取舍段、§7 Q18 行补 Q18c;READMEs:settlement 加三层宿主、pm-adapter health.4 标注宿主+不内联。 |
| 2026-05-21 (#15) | **Q18b + Q19(用户同步收口两件事)**: **(Q18b)** merge/redeem 从"独立 Actor + 自调度"改为**并入 PM 健康检查 tick**(都要拉 `/positions`,合并避免双拉);`TxResult` **不作健康判据**(失败 log+下次重试,不影响 venue_connected/leg_settled);`PolymarketSettlementActor` 独立组件作废,`contract.py` 仍保留作 IO。**(Q19)** 新增**健康检查 ⊥ 执行 全局互斥**(§6.8.7):任一健康检查跑→strategy 放弃所有机会,任一执行在飞→健康检查全推迟;NT msgbus 消息(`health_check.*`/`execution.*` 前后都发)+ 本地镜像;**执行同步前置**=strategy pre-check `if _health_check_active: 放弃`;单 asyncio loop 串行无需锁(置位在首个 await 前、清位 finally)。改动:§5.8 改写、§6.8.4 加 merge/redeem 步、§6.8.5 加 execution.* 消息、新增 §6.8.7、§5.4 落地索引加互斥 pre-check、§4 映射行、§7 Q18 更新+新增 Q19。READMEs:settlement 改并入健康检查、pm-adapter health.1 加 merge/redeem+互斥、strategy 加互斥 pre-check 用例、oe-adapter 加互斥让路用例。 |
| 2026-05-19 (#14) | **Q18:merge / claim 集成进 NT = 独立 `PolymarketSettlementActor` + 周期扫描**(用户要求集成既有自研实现): 核实 merge/claim 为链上 CTF 操作(`contract.py:PolymarketContractService` 经 Builder Relayer 调 `mergePositions`/`redeemPositions`),**上游 PM ExecutionClient 零对应**(grep 确认),不可被"删自研用上游"误伤。用户拍板:归属 = 独立 NT Actor(`src/arbitrage/settlement/actor.py`,持 contract.py;不挂 ExecutionClient);触发 = 周期扫描(NT clock 自重排,redeem 须等链上结算故非事件)。数据源 = PM Data API `/positions` 原始(NT cache 丢了 redeemable/mergeable)。旧 `cleanup.py` 编排平移进 Actor、`contract.py` 保留作 IO。新增 §5.8(Step 8)+ §4 映射行 + §7 Q18;新建 `tests/arbitrage/settlement/README.md`(settlement-8.1~8.8)。 |
| 2026-05-19 (#13) | **Q17 修正:健康检查完全不碰余额 + 可用余额按 venue 非对称**(用户告知 OE WS 已含占用 + 拍板余额靠事件): (1) **OE WS 余额已含挂单占用**(用户确认)→ 推翻 #12 的"PM/OE 统一自扣":`_check_balance` 按 `venue` 分支——**PM 自扣** `total − Σ(PM 在途挂单)`,**OE 直接信** WS 余额不再减(否则双重扣减)。(2) **健康检查不再拉余额**(两 venue 都是):PM 完全靠上游事件(连接+链上成交确认),OE 完全靠 WS 被动推;§6.8.3 / §6.8.4 健康检查动作里余额已移除,report 通路只剩持仓/挂单。改动:§6.8.3 / §6.8.4 去余额 + 加"不碰余额"说明;§5.5 PM 健康检查行 / 账户表 / §5.6 图 / §6.8.1 概览表 / §5.6 落地索引去"周期兜底拉余额";§5.6 Q17 块改非对称表;`_check_balance` docstring 按 venue 分支;§7 Q17 行更新 + 两条"触发频率"还需展开项标 ✅。READMEs:risk-6.3b 改非对称、risk-6.5 去健康检查拉取;pm-adapter health.1 / account.1 去余额;oe-adapter 头部加 Q17 + health.1/2 去余额。 |
| 2026-05-19 (#12) | **Q17:PM 余额刷新机制纠错 + 挂单占用自扣**(用户提问触发,核实上游代码): (1) 上游 `_update_account_state`(execution.py:287)是**事件驱动**——连接时 + 链上成交确认(`POLYMARKET_FINALIZED_TRADE_STATUSES`,2077-2079 / 1959),**无周期 timer**;NT 也无默认 `QueryAccount` 轮询(全库仅 messages.pyx 反序列化处实例化)。原文档多处"PM 主动周期 timer / clock.set_timer"措辞**错误,已纠正**为"事件驱动 + §6.8.4 健康检查 tick 周期兜底"(§5.5 表 / §5.6 图 / §5.6 表)。(2) PM 上报 `reported=True/locked=0/free=total`(Polymarket 链上不托管未撮合单),`CashAccount.apply()` 见 reported 清空 NT 自算 `_balances_locked`(cash.pyx:178-179)→ cache `free` 恒 total。锁定:**`_check_balance` 自算可用 = `total − Σ(cache.orders_open 在途名义额)`,不信 `balance_free()`**;OE 同理。§7 新增 Q17;risk README `risk-6.3` 改自算 + 新增 `risk-6.3b`、`risk-6.5` 改事件驱动;PM adapter README 新增 `pm-adapter-5.account.{1,2}`。**顺带修正** §5.5 line 449 遗留矛盾(PM 健康检查"不走 report 通路"→ 已随 Q-L 翻盘改为走 report 通路)。 |
| 2026-05-19 (#11) | **澄清 `global_min_rebate_sum()` 扫描范围 = 仅有持仓的 active pair**(用户提问触发): 核实旧 `position.py:373` 遍历 `self._positions.values()`(只有持仓的比赛)。**没下过单 / 无持仓的比赛不进遍历,不触发 None**——否则日程表里只要有未交易比赛 global 就恒为 None,fail-closed 把系统焊死。`None` 仅来自"有持仓的 active pair 里有腿 `leg_settled=false`";active pair 无 entry(历史导入等)按 Q-G1 放行正常计入。改动:§6.9.3 `global_min_rebate_sum` docstring 加扫描范围 + 三分支;risk README `risk-6.9.5` 验收补"只遍历 active pair"、新增 `risk-6.9.5b`(未交易比赛不致 None)。 |
| 2026-05-19 (#10) | **Q16 补充:`_check_rebate_gates` 数据源 + settled gate 语义锁定**: (1) **cache 不存 rebate** —— rebate 纯 pull,`_check_rebate_gates` 持 `ArbitragePortfolio` 引用调其方法现算,不读"已存 rebate"、不重复算法。(2) **settled gate**: entry 不存在(从没下过单)→ 放行(Q-G1);某 pair 任一腿 false 使 `global_min_rebate_sum()` 返回 `None` → **fail-closed deny**(用户拍板,拦截新开仓,等健康检查 reconcile 结算齐后 global 恢复实数自动放开;撤单走另一命令通路不受影响,无死锁)。区分两层:Portfolio 方法返回 `None`(数据语义)vs 熔断门限把 `None` 解释为"挡新单"(消费语义)。改动:§5.6 `_check_rebate_gates` docstring + "还需展开"两条尾巴标 ✅、§7 Q16 行补充;risk README `risk-6.9.12` 验收行改为 fail-closed deny、`risk-6.7.7` 拆为 absent→放行、新增 `risk-6.7.8`(global None→deny)/ `risk-6.7.9`(数据源=Portfolio 引用)。 |
| 2026-05-19 (#9) | **Q16 锁定:止盈/止损/全局熔断归属 = `ArbitrageRiskEngine._check_rebate_gates`(逐 submit deny)**: 经讨论"完全遵循 NT"——核实 NT `RiskEngine` 只管 order 合法性 / 名义额 / 频率 / 余额保证金 / `TradingState`(HALTED/REDUCING),不含盈利性概念;NT 的 TP/SL 原语(bracket / STOP_* / TRAILING / ContingencyType)是**挂 venue 的价格触发单**,不适配 way_rebate 指标。结论:本系统 tp/sl/global_sl 唯一动作是"挡新单",而挡单是 RiskEngine 本职,故三门限全进 `_check_order` 子类(平移自旧 `services/risk/service.py:check_risk`)。**别开新仓语义 = 逐 submit deny,无 TradingState 翻闸 / 无监测 Actor / 无频率**(用户拍板"别开新仓");撤单走另一命令通路不受影响(补偿撤单照常)。分工:Strategy 判 `min_rebate_rate`(机会值不值得),Risk 判 tp/sl/global 硬停(触线一律不许开),正交。同时**修正 §5.6 `_check_order` 签名 bug**:NT 父类是 `cpdef bint _check_order(self, Instrument instrument, Order order)`(两参),原文档漏 `instrument`。改动:§5.4 边界表 + 分工注、§5.6 代码块 + 拆解表 + 落地索引 + "还需展开"清单、§6.9.6 调用方表、§7 新增 Q16 行。需同步 `tests/arbitrage/risk/README.md`。 |
| 2026-05-19 (#8) | **§5.4 / §5.5 / §5.6 各加"📍 落地索引"表(导航,非新增设计)**: Step 4/5/6 正文末尾各追加一张索引,把分散在 §6.8(health/session/timeout)、§6.9(ArbitragePortfolio)、§6.6(Debug)、§6.7(锁)的横切结论按"本步要落地什么"映射回该 Step,并列出对应测试 README 用例文件与编号范围。**不复制横切内容**(避免双份漂移),§6.x 仍为单一真理源,Step 正文只做实施待办清单。目的:启动某步实施时读 §5.x 即得完整待办,无需跨 1500 行文档手工拼接。无组件行为/边界/数据流变化,测试 README 无新用例。 |
| 2026-05-19 (#7) | **§6.8.3 OE 时间维度 staleness 模型锁定(用户拍板)**: **不做立即刷新 / 不起独立 staleness 轮询循环 / 不做 watchdog**;每 competition 收帧只写 `last_update_ns` 变量,**仅在健康检查 tick** 内判超时 → 刷新。检测延迟 = 阈值 + 最多一个 `health_check_interval_sec`,用户确认可接受;实施时需保证 `health_check_interval_sec` 在旧 `_staleness_check_interval`(30s)量级,否则 stale 发现太慢。旧 `_staleness_monitor_loop`(`odds_client.py:1472`,独立 30s 循环)/ `_staleness_monitor_task` / `_staleness_check_interval` 折叠进健康检查 tick 后删除。OE README `oe-adapter-2.health.1` 验收点补"仅 tick 评估不立即刷新",新增 `oe-adapter-2.health.1b`(无独立 staleness 循环)。 |
| 2026-05-19 (#6) | **三处时间控制全部改用 NT `Clock` 原语(P1,用户要求"尽量复用 NT 时间控制")**: (1) §6.8.4.5 健康检查循环 —— `asyncio.Event + monotonic() while 循环` 改为 NT `clock.set_time_alert_ns` **自重排 one-shot alert**:callback 内 `try/finally` 跑健康检查 + 末尾按当前 config 重排,trigger=`set_time_alert_ns(now, override=True)`(NT past/now 即时 fire),status=`clock.next_time_ns(name)`,时间=`clock.timestamp_ns()`,关停 NT 自动 `cancel_timers()`;旧 `_health_check_loop` / `_next_health_check_at` / `_health_check_event` 标删除。(2) §6.8.5 Q15 execution 超时 —— 改为 NT `clock.set_time_alert_ns(f"exec_timeout_{coid}", submit_ts+timeout, callback)` **一次性 alert**,terminal 抢先 `cancel_timer` 取消,绝对超时靠 alert 不重设(partial 不调 set_time_alert),per-session timer 名用 client_order_id 唯一;不用 `asyncio.wait_for`。(3) §6.8.3 OE 时间维度 staleness —— `last_update_ns = clock.timestamp_ns()` 记录 + `clock.timestamp_ns() - last_update_ns > 阈值` 判定,不用 wall-clock。OE/PM README schedule.* / timeout.* / health.1 用例措辞同步改为 NT clock 原语,加系列说明引。 |
