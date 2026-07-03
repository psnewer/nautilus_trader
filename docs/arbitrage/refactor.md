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
| 7 | WebGatewayActor(控制台:启停 + 配置) | 落地(2026-06-21,#120 收敛) | Q16 Q17 | §5.7 |
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
| Q13 ✅ | 健康检查下沉各 adapter;execution 退化为单一职责 session,移除 recovery。⚠️ `leg_settled`/状态维度部分已被 #108 `VenueExecutionLiveness` 取代 | §6.8 / synchronization §8.5 |
| Q14 ✅ | `outcome_exposures` / `outcome_shares` 等持仓领域指标 → `ArbitragePortfolio` 子类(kernel swap)。⚠️ settled gate 已被 #108 退役,`way_rebate` 系列接口已被 #121 退役 | §6.9 / risk §4.2 |
| Q15 ✅ | execution tracking 绝对超时(NT clock 一次性 alert),超时即停不补救 | §6.8.5 |
| Q16 ✅ | 单场 profit gates → `ArbitrageRiskEngine._check_profit_gates` 逐 submit deny(别开新仓):所有 outcome `net_profit > share*match_tp` 止盈拦,所有 outcome `net_profit < share*match_sl` 止损拦;全局止盈/止损退役 | §5.6 |
| Q17 ✅ | PM 余额事件驱动(连接+链上成交确认)、健康检查不拉余额;可用余额按 venue 非对称(PM 自扣挂单 / OE 信 WS) | §5.5 §5.6 |
| Q18 ✅ | merge/claim 保留自研;**Q18b** 并入 PM 健康检查 tick(非独立 Actor)、结果不作健康判据;**Q18c** 宿主 = PM ExecutionClient 薄子类(三层:宿主/`PolymarketSettlement` 编排/`contract.py` IO) | §5.8 §6.8.4 |
| Q19 ✅ | 健康检查 ⊥ 执行互斥历史设计。⚠️ #105/#108 后 `health_check.*`/strategy 健检前置与 strategy liveness pre-check 退役,页锁 + Risk liveness gate 接管 | §6.10 / synchronization §8 |
| Q20 ✅ | strategy 机会快照隔离:开跑时冻该 pair 的订单簿+持仓+instrument_info,全程用拷贝免受新成交扰动;安全闸走 live;NT 无原生快照故自建;strategy 内部不单独成章。⚠️ venue liveness 安全闸由 Risk 读,Strategy 不读 | §5.4 |
| Q21 ✅ | **strategy 框架锁定(2026-05-24)**: scope-priority(pair_id > comp > sport,**挂载存在锁定不降级**)+ 每 scope 单策略(不并行)+ 策略 = 套利树 + 补救树(asyncio.gather 并行;套利 hit 阻断补救 fire)+ condition 嵌套树(self_hits 信号量布尔表达式 → sub_conditions 互斥 / 叶子 checktion list AND + action 1 个)+ evaluate 无副作用(返 EvalResult,fire 由顶层做)+ SignalStore 双状态(persistent 写后保留 / transient 用后即清)+ BoolExpr AND/OR/NOT 表达式树 + scope-priority 自建(NT 无原生)。详细设计 `architectures/strategy/architecture.md` | §5.4 / Step 4 |
| Q28 ✅ | **Venue Registry / Capability 第二阶段(2026-07-01)**:SE 第一阶段已验证 adapter 基础能力(place/cancel/WS/discovery),继续推完整实盘 E2E 会扩大硬编码债务;先收敛 venue enablement/factory/概率模型/size 公式等同类规则到静态 registry,真实 venue identity 与 PM anchor/settlement 约束保留。详细设计 `architectures/_cross-cutting/venues.md` | 横切 venues |
| Q29 ✅ | **PMSPORTS event anchor(2026-07-02)**:保留 PM discovery,但让 PMSPORTS 也执行公开 Gamma discovery,产出 `.PMSPORTS` non-tradable synthetic event instruments 参与 matching;`.PMSPORTS` 只做 event anchor/lifecycle,不进入 Strategy/Risk/Execution 套利流。PM/OE/SE 均退为 tradable venues 匹配到 PMSPORTS anchor。详细设计 `architectures/_cross-cutting/sports-event-anchor.md` | 横切 sports anchor |

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
| `services/execution/`(套利决策算法) | `src/arbitrage/strategy/`(`StrategyEvaluator(Actor)` + config 策略树;~~`ArbitrageStrategy(Strategy)`~~ 早期设想,**Q21(2026-05-24)改为 Actor 框架**——scope 优先级 + 双树 + condition 树,NT 无原生) | 仅保留套利决策逻辑(planner 类) | §5.4(待 Step 4) |
| `services/execution/`(其余: tracker / orchestrator / service / session 等) | (无对应物) | **整体删除**;NT `Strategy` + `ExecutionEngine` + `MessageBus` 替代订单追踪、事件分发、生命周期 | §5.4(待 Step 4) |
| `services/risk/` | `src/arbitrage/risk/engine.py`(`ArbitrageRiskEngine` NT 子类) | NT `RiskEngine` 标准管道透明拦截做余额检查;**账户状态维护归 ExecutionClient**(PM 主动 / OE 被动 WS,写 NT Cache);**告警让前端自己看**(WebGatewayActor 订阅 AccountState 事件转 JSON 推浏览器,无独立告警 Actor);Strategy 不引用 Risk | §5.6(待 Step 6) |
| `services/strategy/service.py` 中的 `_check_and_adjust_size` Step 1(深度缩放) | `ArbitrageStrategy._adjust_share_by_liquidity` hook | Strategy 内部职责;读 NT `cache.order_book(...)` | §5.4(Step 4) |
| `services/strategy/service.py` 中 `_check_and_adjust_size` Step 2(最小限额门控)+ `MIN_SIZE_POLYMARKET/_ORBITEXCH` 常量 + `check_min_size` 函数 | (无对应物) | **整体删除**,完全用 NT 自动检查 `instrument.min_quantity`(应用层不更严) | §5.6 |
| `services/web_gateway/` | `src/arbitrage/web/`(`actor.py`+`app.py` FastAPI 路由) | 外壳替换为 Actor + FastAPI 协程同 loop。**控制台已落地(2026-06-21)**:TradingState 启停(boot 默认 HALTED)+ 配置编辑(C 混合,经 `command.arb.*` 命令)+ `/ws` 推 state。legacy pipeline/run/subscribe 在 NT 无意义 → 不做;只读监控 endpoint #120 移除(看日志) | §5.7(控制台落地) |
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

#### 5.2.3 架构修正(#58,2026-06-04):InstrumentRefresher → DataClient 原生周期发现 **【✅ 已实现 #59;详细设计下沉至 `discovery/architecture.md §3.3` + `matching/architecture.md §3.3/§4.4`,live smoke10 验】**

> **why 见修订记录 #58**。§5.2.2 的"独立 Actor refresher"被验证为**从零重造了 NT 原生 `DataClient._update_instruments`**(binance/bybit 范式),且本次 3 个 bug(Gap E pending-task / cache 桥接缺失 / topic 通配)都是脱离原生路径的症状。本块 supersede §5.2.2 的独立 Actor 设计;**当前代码仍是 §5.2.2(refresher 在用、已修可跑),本块是下个 slice 的目标设计**。

**取向**:发现 add/update 走 **(A) 全原生**(DataClient 拥有周期发现);**eviction 另起独立机制**;refresher Actor 退役。

**组件改动面(接口级)**:
1. **PM DataClient**(`adapters/polymarket/data.py`):`_connect` 加 `self._update_instruments_task = self.create_task(self._update_instruments(interval))`(已有 `_send_all_instruments_to_data_engine`);新 `_update_instruments(interval)` = `while: sleep; await provider.load_all_async(); _send_all_instruments_to_data_engine()`(+ try/except + CancelledError);`_disconnect` cancel task。
2. **OE DataClient**(`adapters/orbitexch/data.py`,改动最大 —— 现**完全无** instrument 灌入):加 `_send_all_instruments_to_data_engine()`(镜像 PM:`for inst in provider.get_all().values(): self._handle_data(inst)`);`_connect` 加 `load_all_async` + `_send_all` + `_update_instruments` task;`_disconnect` cancel。
3. **MatchingActor**(`matching/actor.py`):删 `msgbus.subscribe(InstrumentsRefreshed)` + `on_data` + `_last_refresh_ns`;`on_start` 改 `clock` 自重排 timer → `_maybe_match`;**`_both_recent()` 2×窗口 latch → "两 venue `cache.instruments(venue)` 非空" latch**;match 逻辑(`match_events`/`_emit_pair`)不变。
4. **Eviction reaper**(新):触发 = `expiration_ns < now` 扫描(PM)+ 现有 settlement 流(`arb_execution._settlement`/`leg_settled`);**不用 `InstrumentClose`(Gap β:适配器不发)**;动作 = `PairRegistry.unregister_pair(pair_id)`(已存在未接线)+ 清活跃集;**cache 内 instrument 留着**(Gap:NT 无 instrument 删除 API;纯内存,有界于赛事量)。归属 P11 待定(倾向接 execution settlement 主方)。
5. **config / bootstrap**:PM/OE data client config 加 `update_instruments_interval`(秒);`bootstrap`/`launcher` 删 refresher 的 `_RuntimeDeps` + `add_actor`;`refresher.py` + `InstrumentsRefreshed` event 退役。

**已验框架假设(本次)**:
- **Gap α**:`initialize(reload=True)` 仅 config `load_all=True` 才重跑;smoke 实测 PM `initialize()` 加载 0 → `_update_instruments` **直调 `provider.load_all_async()`**,不走 initialize 的 config 闸。
- **Gap β**:无 `InstrumentClose` 发布 → eviction 用 `expiration_ns`(PM,`data.py:228` 已查)+ settlement(OE `start_ts=0` 无 expiration → 仅 settlement)。
- `create_task`:`LiveMarketDataClient` 提供(binance/bybit 在用),可用。

**Q4/Q5 落点**:Q4(失败 venue 不参与)自然成立(失败 → cache 留 last-good 或空,latch + 增量 matching 不误删);**Q5 2×窗口退役**,降为 cache-非空 latch;"多 venue 一失败不挡其他"自然成立。

**sub-decision(已定)**:① matching 触发 = 自 timer + cache-非空 latch(不订 `on_instrument` 免 108×/轮;不复活 `InstrumentsRefreshed`);② refresher 独有的运行时改 interval(msgbus)+ `on_save/on_load` 持久化(Q3/Q6)**迁移中降级,按需经 DataClient config 恢复**。

**开放项(#59 已决)**:① matching timer 间隔 → **锁 10s**(`cfg.discovery.refresh_interval_secs` 默认 10);② **pair expiration 只遵循 PM**(用户定)→ `_maybe_match` 只按 PM `expiration_ns` 过滤/eviction,OE 不参与判定(`_reap_stale_pairs` PM 腿过期驱动);③ reaper 归属 → **matching**(eviction 判据是 instrument `expiration_ns` cache 静态属性、非结算状态 → 不耦合 execution;原"挂 execution settlement"的考虑因 ② 作废)。OE 侧"市场关闭/结算"清理(独立于 PM expiration,若需)留后续接 `arb_execution` settlement。

**落地建议**:独立 slice / 独立会话(动 2 个 data client + 退役 refresher + 新 reaper + bootstrap;独立提交 + 独立 live smoke)。

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
| **机会评估**(这个套利机会够不够赚才开,`min_rebate_rate`) | **Strategy 内部职责** | Strategy 从订单簿/快照持仓计算策略自己的机会收益;不读 Portfolio `way_rebate` 系列接口 |
| **深度缩放**(根据流动性算应该下多少 share) | **Strategy 内部职责** | `_adjust_share_by_liquidity` hook,Strategy 算订单参数时自己处理 |
| **最小限额检查**(`size ≥ instrument.min_quantity`) | **NT RiskEngine 自动** | NT 自带,无需扩展(应用层不更严,删除 `MIN_SIZE_*` 常量) |
| **余额检查**(够不够下单) | **`ArbitrageRiskEngine`(NT RiskEngine 子类)** | NT 自动拦截 `submit_order` 管道 |
| **单场 profit gates**(止盈 `match_tp` / 止损 `match_sl`) | **`ArbitrageRiskEngine._check_profit_gates`** | NT 自动拦截;所有 outcome 绝对利润跨过 `share*threshold` 才 deny = 别开新仓(Q16 #116,详见 §5.6) |

> **机会评估 vs 单场 profit gates 的分工(Q16)**: Strategy 判"这单值不值得做"(`min_rebate_rate`,正向门槛);Risk 判"该场是否已经整体赚够或整体恶化,不允许继续开新仓"(反向硬停)。两者正交。TP/SL 放 Risk 而非 Strategy,是因为它在本系统里不是挂 venue 的价格止损单,**唯一动作就是"挡新单"**,而挡单正是 NT `RiskEngine` 的本职。撤单走另一命令通路,不受这些 deny 影响。全局止盈/止损已撤掉。

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
  - instrument_info(含 outcome role / in_play 等策略计算所需元数据)
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
| §6.8.2 | 已失效:#108 后 Strategy 不读 `leg_settled` / liveness;venue 执行健康由 Risk gate 统一拦截 |
| §6.10 | 已失效:#108 后 Strategy 不维护 `health_check.*` 镜像;全局执行兜底改为直读 `is_execution_active` callable + per-pair in-flight gate |
| §6.8.5 | 接受 execution 的 cancel-only/submit+track session 语义:strategy **每轮全量重算**,不假设上轮 submit 还在排队;残留挂单时当次 submit 被丢弃,下轮重发(可能是另一意图) |
| §6.8.6 | 补偿撤单 / 撤后再下 / 单腿失败 / 裸单窗口(Q-F)**全归 strategy**,execution 不再 recovery —— 此为 Step 4 核心待设计项 |
| §6.9.3 / §6.9.6 | 已被 #121 取代:Strategy 不调 Portfolio `way_rebate` 系列接口;机会收益由策略树基于快照订单簿/持仓自行计算 |
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
    """扩展 NT RiskEngine,加应用层余额检查 + 单场 profit gates。
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
        # 应用层补充 2:单场 profit gates(只挡开新仓;撤单走另一命令通路不受影响)
        if not self._check_profit_gates(order):
            return False
        return True

    # ─── Hook 点(Debug 子类可覆盖)───
    def _check_balance(self, order: Order) -> bool:
        """读 cache.account_state 判断够不够下单。可用余额按 venue 非对称(Q17):
        - PM: available = total − Σ(PM cache.orders_open 在途名义额)(reported free=total 不含占用,自扣)
        - OE: available = cache 余额(WS 已含挂单占用,不再减,否则双重扣减)
        """
        ...

    def _check_profit_gates(self, order: Order) -> bool:
        """单场止盈/止损硬停。
        触线一律 deny(= 别开新仓),与 strategy 的机会评估(min_rebate_rate)正交。

        数据源:pair_id 经 PairRegistry;exposure **持 ArbitragePortfolio
        引用调 outcome_exposures(pair_id)**(cache 不存门控结果,纯 pull 现算,§6.9),不重复算法。

        门限(任一触发 → return False):
            1. match_tp:所有 outcome net_profit > share*match_tp → deny(已赚够,别再加仓)
            2. match_sl:所有 outcome net_profit < share*match_sl → deny(该场恶化,别再加仓)

        全局止盈/止损退役;global_sl/global_min_rebate_sum 不再参与 Risk 门控。
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
| 单场止盈 `match_tp` | `ArbitrageRiskEngine._check_profit_gates`(所有 outcome `net_profit > share*match_tp` 才 deny) |
| 单场止损 `match_sl` | 同上 |
| 全局累计止损 / 循环熔断 `global_sl` | **退役**;字段仅为旧配置兼容,不再参与 Risk 门控 |
| 持仓限额 | NT `RiskEngine` 自带或扩展 |

**Step 6 启动时还需展开**:
- NT `RiskEngine` 的具体可扩展性(子类化是否完整支持 / `_check_order` 是否是合适的 hook 点)
- ~~上游 PM `_update_account_state` 触发频率~~ ✅ 已锁定(Q17):事件驱动,无周期 timer,健康检查不拉;可用余额 `_check_balance` 按 venue 非对称自算(PM 自扣 / OE 信 WS)
- OE WS 帧中余额信息的解析路径(Step 5 启动时审计 OE WS 协议)
- ~~循环熔断的归属~~ ✅ #116 修订:全局止盈/止损退役;Risk 只保留单场 profit gates,且不翻 `TradingState`、不起 Actor
- ~~profit gate 数据源~~ ✅ #116 修订:持 ArbitragePortfolio 引用调 `outcome_exposures(pair_id)`(cache 不存门控结果,纯 pull 现算)
- ~~settled gate 失败时保守放行 vs 拦截~~ ✅ #108/#116 修订:settled gate 退役;venue liveness 由 `_check_required_venues_alive` 负责,全局 `None` 不再作为门控信号
- `match_tp` 留在 risk:动作纯为"挡新单";若未来把止盈改成策略性平仓/减仓,再回迁 Strategy。

**📍 落地索引(Step 6 实施清单 —— 横切结论单一真理源在 §6.x,本步只需逐项落地)**:

| 横切章节 | 本步要落地什么 |
|---|---|
| §6.9.1~6.9.5 | `ArbitragePortfolio(Portfolio)` 子类:当前保留 `outcome_exposures(pair_id)` / `outcome_shares(pair_id)` 两个 Python 方法;`way_rebate` / `min_way_rebate` / `way_rebates_by_venue` / `global_min_rebate_sum` 已被 #121 退役 |
| §6.9.3 settled gate | 已失效:#108 退役 settled gate,#121 退役 `way_rebate` 系列接口;venue 执行健康统一由 Risk `VenueExecutionLiveness` gate 负责 |
| §6.6 | `DebugArbitrageRiskEngine._check_order` 子类覆盖 `skip_check_size`(跳过 NT 父类最小限额检查) |
| §5.6 正文 | `ArbitrageRiskEngine._check_balance` 读 cache.account_state 做余额检查;`_check_profit_gates` 做单场 match_tp/match_sl(逐 submit deny,Q16 #116);`_check_order` 签名两参 `(instrument, order)`;NT 父类自动管 min/max_quantity;**无 BalanceMonitorActor**、**无熔断 Actor / TradingState 翻闸** |

| 测试用例文件 | 覆盖范围 |
|---|---|
| `tests/arbitrage/risk/README.md` | risk-6.1~6.7(透明拦截/min_quantity/余额检查/venue liveness/profit gates/share limit/账户维护)+ risk-6.9.x(ArbitragePortfolio swap / outcome_exposures / outcome_shares / 旧 settled gate 退役记录) |
| `tests/arbitrage/debug/README.md` | `DebugArbitrageRiskEngine` skip_check_size |
| `tests/arbitrage/web/README.md` | web 控制台用例;只读 positions 监控 endpoint 已由 #120 移除 |

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

**结果回流(无需显式 publish 事件)**: merge/redeem 成功后链上持仓变化 → 同一/下次 PM 健康检查的 position report 通路自然反映 → Portfolio outcome 指标 pull-based 调用即重算。不发事件、不直接改 cache。

**与执行互斥**: merge/redeem 在健康检查 tick 内跑,自动受 §6.10 全局互斥保护(执行在飞时整个健康检查 tick 跳过,merge/redeem 也不会在执行期改链上持仓)。

**实施时还需展开**:
- merge/redeem 在 tick 内的次序(reconcile 先 / merge-redeem 后);失败不阻断 reconcile
- `PolymarketContractService` creds 如何与 execution config 共享 / 注入(NT factory 层;builder/relayer creds 为额外配置)
- `_execute_with_proxy` 的 monkey-patch derive 线程安全(patch 全局模块引用;健康检查在 NT 单 loop 内串行跑,确认与其它 PM 操作不并发——§6.10 互斥已大幅降低风险)
- merge/redeem 频率是否要低于健康检查(如每 N 个 tick 跑一次,避免频繁上链);Step 实施时定
- Debug 子类化(P10:`skip_settlement` / mock TxResult)

---

### 5.9 Sports 比分信号接入(实时赛事状态 → strategy + eviction)**【✅ 已实现 #60;详细设计:`adapters/polymarket/sports.py` + matching/strategy architecture.md】**

> **起因**:eviction 原用 gamma `end_date_iso`(#59)、settlement 原用 Data API `redeemable` —— **用户判定两者都不准**;改接 PM **Sports WebSocket** 拿真实赛事信号,顺带把比分引入平台供 strategy 用。**NT 的 PM 适配器对此零原生**(只连 CLOB `market`/`user` WS;无任何比分数据类型、从不连 sports-api 端点 —— 本会话全库 grep 确认)。

**需求**:连 `wss://sports-api.polymarket.com/ws` → 实时赛事状态 → ① **eviction**(准确"比赛结束")② **strategy** 实时比分 / in-play 信号。

**外部接口实采(本会话 live 验证)**:
- 端点 `wss://sports-api.polymarket.com/ws`,**无订阅、无鉴权**;协议层 keepalive(偶发 text `"ping"`→回 `"pong"`);**事件驱动、稀疏**(仅状态变化推,120s 仅 3 帧)。
- 载荷 `sport_result`:`gameId`(int,**映射键**)/ `leagueAbbreviation`(实见 nhl/mlb/fif/wnba)/ `homeTeam`/`awayTeam`(格式逐 league 异:wnba 三字码 `MIN`、fif 全名 `Mexico`)/ `status`/`score`/`period`/`elapsed`/`live:bool`/`ended:bool`/`finished_timestamp`(**仅 ended 时有,不能当漏-ended 的兜底**)/ 嵌套 `eventState`。**无 `slug`/date 字段**(文档不准)。
- **映射验证**:gamma `/events?series_id=` 的 event 带 `event["gameId"]`,**与 sports WS gameId 同值**(wnba 13002300 双向对上);**ATP series 10365:36 events 全有非空 gameId(tennis 覆盖键就绪 ✓)**;gamma event 本身也带 `live/ended/score/period`(REST 版同款信号)。

**决策(全定)**:
- **D1 客户端**:独立 `LiveMarketDataClient` 子类(client_id `PMSPORTS` —— **名不含 `-`**,否则 NT node_builder `partition("-")[0]` 前缀路由到 POLYMARKET 主 factory,#60 smoke 抓出),`_connect` 开 WS firehose → **`msgbus.publish` 裸发到 `data.SportsGameUpdate*`**(消费者 `msgbus.subscribe("data.{Type}*")` 带 #58 `*` 通配;**注**:`_handle_data` 走 DataEngine.process 只认内置/CustomData,裸自定义 Data 报 "unrecognized type",#60 smoke 抓出 → 改裸 publish)。不塞进 CLOB DataClient(异协议:无订阅 / 不同 keepalive)。
- **D2 事件**:`@customdataclass SportsGameUpdate(Data)`(gameId/league/home/away/status/score/period/elapsed/live/ended/finished_ts + ts)。
- **D3 映射 = `gameId` 直配**:`arb_provider` 发现时把 `event["gameId"]` 抽进 `instrument.info["game_id"]`(新增第 7 key);matching 建 pair 时建 `game_id→pair_id` 索引;sports update 经 gameId 查 pair。**不用模糊队名 / slug**(实采证实无 slug、队名格式逐 league 异)。
- **D4 eviction = 纯 `ended:true`**(用户定):收 `ended:true` → gameId→pair_id → `unregister_pair`。**无兜底** —— 漏 `ended` 就不清(可接受);`finished_timestamp` 与 `ended` 绑定不能当 fallback。**替换 slice A(#59)的 expiration reaper**(`end_date_iso` 不准)。
- **D5 strategy**:`SportsGameUpdate` → `signal_collector`(slice 9 seam,actor 注释预留"比分/比赛开始"接入点)→ `SignalStore`(按 pair_id)→ 条件树读 score/live/period;`in_play` 可改用 sports `live` 作权威源(替当前 OE `info["in_play"]`)。
- **D6 firehose 过滤**:全收,消费端按已知 game_id 忽略无关(便宜)。

**改动面(接口级)**:① `arb_provider` 抽 `event["gameId"]`→`info["game_id"]` ② `SportsGameUpdate` 事件类 ③ 新 sports DataClient(WS + ping/pong + parse `sport_result`)④ `game_id→pair_id` 索引(matching 维护)⑤ matching 订 `SportsGameUpdate`、`ended`→reap(**替** expiration reaper)⑥ strategy `signal_collector` 接 ⑦ factory + launcher 注册 `PMSPORTS` data client。

**P11**:`SportsGameUpdate` 单一自然归属 = 生产者 sports DataClient(消费者 matching/strategy 只读,非对称)→ 挂 data 主方 + 跨引用,**不入横切**。`game_id→pair_id` 索引 + eviction 归 matching(PairRegistry 唯一写者)。

**前置风险 / 待验**:tennis 的**实时 WS 推送**未直接实采(实采时巴黎凌晨无网球在比;但 gamma ATP events 全有 gameId,映射键已就绪)→ 实现后挑有网球时段 live smoke 验 WS 真推 tennis。**注**:eviction 也可改用 gamma REST `event["ended"]`(发现已轮询,~60min 延迟);但 sports WS 本就为 strategy 的**实时**比分而建,eviction 顺势用 WS `ended`。

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

> **⚠️ `start()` 并发安全(2026-06-21 live SIGABRT 修)**:实际实现中 OE Data/Exec 各自 `_connect` 都会 `await browser_manager.start()`(幂等保护:`if _context is not None: return`)。但 NT **并发**连两个 client → 两个 `start()` 协程在第一个 `await launch()` 完成置 `_context` **之前**都越过守卫 → **并发双开 Chromium** → macOS crashpad 竞争 → SIGABRT(`TargetClosedError`,OE Data+Exec `_connect` 同时挂)。修:`start()` 加 `asyncio.Lock` 串行首次启动 + 进锁后 double-check。测试 `tests/arbitrage/adapters/orbitexch/test_browser_manager_sharing.py::test_concurrent_start_launches_browser_once`(3 并发 start → 仅 launch 1 次)。**注:此 SIGABRT 若 lock 后仍偶发,则属 macOS/Playwright 环境问题,非本竞态。**

#### Page 命名约定

| 消费者 | Page name | 用途 |
|---|---|---|
| `OrbitExchInstrumentProvider` | `"discovery"` | 列举赛事/市场 |
| `OrbitExchDataClient` | `f"comp-{sport_id}_{competition_id}"`(**每 competition 一页**,#68) | competition 页 WS 帧拦截(价格);新开/刷新统一 `_open_or_reload_competition_page` |
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

> ⚠️ **失效指针(2026-06-15 #108)**:`leg_settled` 已退役,不再作为 Strategy/Portfolio/Risk 的安全闸。新机制为 `VenueExecutionLiveness`(`venue_order_alive` + `venue_position_alive`),由 execution/reconciliation 写、Risk 读,详见 `_cross-cutting/synchronization.md §8.5`。本节以下保留为历史设计记录。

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
| OE health check 看到 `settled=false` | reload **execution 页**(#68 拆页后;非 competition 页)→ `CURRENT_BETS` WS 重推 → 该腿 `settled=true`(**不碰余额**,Q17)。落点/安全闸见 §4.3 |
| PM health check 成功拉取 持仓/挂单(**不拉余额**,Q17) | PM 侧该 competition 所有方向 `settled=true` |
| **非 execution 触发的 venue 事件**(推迟到达 / 手动在 venue 操作 / 历史挂单成交,含 partial) | **进 NT 标准管道**(`generate_order_*` → ExecutionEngine → cache + topic + Strategy 回调);**不创建** `leg_settled` entry;若该方向已有 entry(以前 execution 过)则置 `settled=true`,否则跳过 |

**边界澄清**: `leg_settled` 不是 fill 事件的通用 sink,而是 **execution 启动后通讯通道是否存活的指示**。execution 没启动过的方向上发生 venue 端事件(WS push),走 NT 标准路径通知 Strategy 即可,不会凭空创建 `leg_settled` entry。这保证 entry 集合 = "曾经发起过 execution 的 (competition, direction)" 集合,边界清晰。

---

#### 6.8.3 OE adapter 健康检查(吸收原 OE 网页监控)

> ⚠️ **本节是决策日志(为什么 + 定了什么);详细"怎么做"以 execution `architecture.md §4.3` 为单一真理源。** 本节原文写于 **#68 拆页前**,"reload 页面 → 拉持仓/挂单 / DOM 帧采集"基于"单页同出赔率+持仓"的旧心智,**已被 #68 推翻**(页拆成 competition 赔率页 / execution 持仓挂单页;恢复 = reload→该页 WS 重推,非 DOM 抓;两触发维度落点不同)。落地分期(Phase 1 时间维度 ✅ / Phase 2 状态维度代码已接、安全闸默认关)均见 §4.3。

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
- 刷新动作 / 数据回写 / 落地分期: **详见 §4.3(单一真理源)** —— reload→该页 WS 重推、走 NT 标准 report 通路(`generate_*_status_report`,不覆盖 cache);#68 后两触发维度落点(时间维度→competition 页 / 状态维度→execution 页)与 Phase 1/2 状态都在那。
- **健康检查不碰余额**(2026-05-19 Q17):OE 余额走 WS 被动推(reactive,已含挂单占用),WS 活着即新;健康检查的职责是**保证 WS/页面活着**,WS 死了 reload/重连后 WS 自然重推余额。**不在健康检查里拉余额**(OE 也无 REST 可拉)。
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
        self._run_health_check()     # OE: 刷页+对账;PM: 拉持仓/挂单+对账(不拉余额,Q17)
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

> 2026-06-09 #85 校准:OE `placeBets` venue 回执确认返回 `status=OK + offerIds`;NT `OrderAccepted`
> 事件本身无独立日志锚点,但成功路径会调用 `generate_order_accepted`,且下一轮 cancel-only 能撤旧
> cache open order。若订单 accepted 但未成交/未撤销,submit+track session 继续等到 30s 绝对超时,
> 这是 Q15 默认语义。

| Session 类型 | 触发条件 | 动作 | session 结束条件 |
|---|---|---|---|
| **cancel-only** | strategy 调 submit 时,该 instrument 上有**残留挂单**(原 "stale orders" 措辞修正) | 撤掉残留挂单(**丢弃**当次传入的 submit) | venue 推 CANCELED **或** 超时 |
| **submit+track** | strategy 调 submit 时,无残留挂单 | 下单 → 等成交 | venue 推 terminal(FILLED / CANCELED / REJECTED / EXPIRED) **或** 超时 |

**两种 session 都 track 到 terminal 或 timeout 二选一**(不分种类的契约统一)。

**超时机制**(Q-T 四项已拍板,2026-05-19):

| 维度 | 决定 |
|---|---|
| **超时触发动作** | session **直接结束**,**不**自动发起撤单 / 重试 / 任何补救;order 在 venue 端保持当时状态(`ACCEPTED` / `PARTIALLY_FILLED` 等),由 strategy 后续轮次决定怎么处理。#85 live 已校准:accepted 后未成交等待到 timeout 属当前语义 |
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
| `way_rebate` 计算的触发时机 | Strategy 设计 / Portfolio | ✅ **历史已解决,现被 #121 取代**:当前 `way_rebate` 系列接口退役;Portfolio 只保留 `outcome_exposures/outcome_shares` pull-based 指标 |
| Strategy 端补救 / 撤后再下 / 跨腿对冲 | Step 4 | ⏳ defer 到 Step 4 |
| 裸单飘着的窗口(Q-F) | Step 4 strategy 设计 | ⏳ defer 到 Step 4 |
| `bug_compensating_cancel_missing` 闭环 | Step 4 strategy 设计 | ⏳ defer 到 Step 4 |

---

### 6.9 ArbitragePortfolio: way_rebate 等领域指标(Q14,2026-05-19 锁定;#121 后部分失效)

> ⚠️ **现状覆盖(2026-06-22 #121;2026-06-28 更新)**:本节最初设计的 `way_rebate` / `min_way_rebate` / `way_rebates_by_venue` / `global_min_rebate_sum` 当前接口已退役。现行代码保留 `outcome_exposures(pair_id)` 供 Risk 单场 profit gates 使用,保留 `outcome_shares(pair_id)` / `outcome_shares_for_venue(pair_id, venue)` 供 Strategy `share_limit` action 使用。Risk 不再执行 share limit adjusted-size gate。以下 `way_rebate` 设计内容保留为历史决策记录,不作为当前实现依据。

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
- `share`: 按 outcome 聚合后的最大实际 share,即 `max(Σ share_if_wins(leg) for same outcome)`;单腿 PM=`size`,OE=`size*price*fx`。同一 outcome 若同时有 PM/OE 持仓,先相加再参与分母,避免 numerator 已聚合但 denominator 只取单腿 max 导致 rebate 被放大。`mean_rebate` 下单会让各 outcome share 相同;若 live probe / 手动仓位导致 outcome share 不同,用最大 outcome share 归一化
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

        Settled gate (2026-05-19; ⚠️ 已被 #108 退役):
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
| `ArbitrageRiskEngine._check_profit_gates` | 每次 `submit_order` 经 RiskEngine 拦截时(逐 submit) | 读 `outcome_exposures(pair_id)` 与 `share*match_tp/match_sl` 绝对金额阈值比较,触线 deny(Q16 #116,§5.6) |

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
| **Q13** | **健康检查归属 + execution 简化** | ✅ **已锁定(§6.8)**: 健康检查下沉到各 adapter;execution 退化为单一职责 session(cancel-only 或 submit+track)。⚠️ **#108 修正**:旧 `leg_settled=false` 状态维度/两层状态不再作为现状安全闸,改为 `VenueExecutionLiveness` order/position alive,由 Risk 统一门控;旧文字保留历史。 | Step 2 / Step 4 / Step 5 |
| **Q14** | **Portfolio outcome 领域指标归属** | ✅ **已锁定(§6.9,#121 修正)**: 子类化 `ArbitragePortfolio(Portfolio)` 加 Python 方法;当前只保留 `outcome_exposures(pair_id)` / `outcome_shares(pair_id)`。`way_rebate` / `min_way_rebate` / `way_rebates_by_venue` / `global_min_rebate_sum` 已退役。执行健康 fail-closed 只在 Risk 的 `VenueExecutionLiveness` gate。 | Step 4 / Step 6 |
| **Q18** | **merge / claim(链上结算)集成进 NT** | ✅ **已锁定(2026-05-19,§5.8)**: merge/claim 是链上 CTF 操作,**上游 PM ExecutionClient 无对应物**(只包 CLOB),本工程自研保留。数据源 = **PM Data API `/positions` 原始**(NT cache 丢了 redeemable/mergeable)。merge=同 condition≥2 outcome 持仓合并(amount=min);redeem=redeemable 门控。`contract.py` 保留作 IO。**归属 Q18b 修正(2026-05-21)**:**不设独立 Actor**,merge/redeem **并入 PM 健康检查 tick**(§6.8.4,复用其 `/positions` 拉取避免双拉);`TxResult` **不作健康判据**(失败 log+下次重试);redeem 结算滞后由健康检查周期性兜住;`cleanup.py` 编排平移进健康检查路径。**Q18c 钉死宿主(2026-05-21)**:三层 = 宿主/触发 **PM `ExecutionClient` 薄子类**(唯一同时有 `generate_*_status_report`+钱包 creds+健康检查 tick)→ 编排 `PolymarketSettlement` 普通类(组合,非内联)→ IO `contract.py`;纯度取舍已知,用组合缓解 | Step 5 / §6.8.4 |
| **Q20** | **strategy 机会快照隔离** | ✅ **已锁定(2026-05-21,§5.4;#121 修正)**: 为避免订单规划+执行被新成交扰动,strategy 在机会评估开跑时取 **per-pair 快照**(冻订单簿所需值 + 持仓 + instrument_info),全程用拷贝直到该次套利结束(双腿 terminal/timeout/放弃)再丢弃,下一轮重取新鲜快照。**安全闸走 live 不冻**;⚠️ #108 后 venue liveness 由 Risk 读取,Strategy 不读 `leg_settled`/liveness。 | Step 4 |
| **Q19** | **健康检查 ⊥ 执行 互斥粒度 + 机制** | ✅ **历史锁定(2026-05-21,§6.10)**。⚠️ **#105/#108 后现状修正**:`health_check.*` / strategy `_hc_running` / strategy 健检前置随 NT reconciliation + OE 页锁迁移退役;strategy 不看 venue liveness,统一由 Risk gate 拦。旧机制保留为迁移前记录。 | Step 4 / Step 5 |
| **Q17** | **PM 余额刷新机制 + 挂单占用** | ✅ **已锁定(2026-05-19,§5.5 / §5.6)**: (1) **刷新机制**:上游 `_update_account_state` 是**事件驱动**(连接时 + 链上成交确认 `POLYMARKET_FINALIZED_TRADE_STATUSES`),**无周期 timer**;NT 也无默认 `QueryAccount` 轮询(全库仅反序列化处实例化);周期兜底归 §6.8.4 健康检查 tick。原文档"PM 主动周期 timer"措辞已纠正。(2) **挂单占用(按 venue 非对称)**:**PM** 上报 `reported=True/locked=0/free=total`(链上不托管未成交单),`CashAccount.apply()` 见 reported 清空 NT 自算 locked(cash.pyx:178-179)→ cache `free` 恒 total → `_check_balance` 自算 `total − Σ(PM cache.orders_open 在途名义额)`;**OE** WS 上报**已含挂单占用**(用户确认 2026-05-19)→ `_check_balance` **直接信 cache 余额不再减**(否则双重扣减)。`_check_balance` 内按 `order.instrument_id.venue` 分支。(3) **健康检查不碰余额**:PM 完全靠事件、OE 完全靠 WS;§6.8.3/§6.8.4 健康检查动作里余额已移除 | Step 5 / Step 6 |
| **Q16** | **单场止盈/止损 profit gates 归属** | ✅ **#116 修订(§5.4 / §5.6 / §6.9.6)**: `match_tp/match_sl` 进 `ArbitrageRiskEngine._check_profit_gates`,**逐 submit deny = 别开新仓**;判定看所有 outcome 的绝对 `net_profit` 是否跨过 `share*threshold`;**不**翻 NT `TradingState`、**不**起监测 Actor、**无频率**。全局止盈/止损退役,`global_sl` 仅为旧配置兼容字段。venue 执行健康由 `_check_required_venues_alive` 独立门控,且仍不翻 NT `TradingState`。 | Step 4 / Step 6 |
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
| 2026-07-02 (#185) | **MatchedPair / Web `/matched_pairs` 删除旧 PM/OE 投影字段**。承接 #177 与用户“删掉兼容兜底,把架构调通”的要求,`MatchedPair` schema / `to_dict` / `from_dict` 不再携带或保存 `pm_instrument_ids` / `oe_instrument_ids`;旧 payload 中这两个 key 只会被忽略,不会补齐也不会保存在事件对象。Matching 主契约只剩 `anchor_instrument_ids` / `tradable_instrument_ids` / `venue_instrument_ids`;Strategy 只消费 `tradable_instrument_ids`;Web `/matched_pairs` 同步删除 `pm_instrument_ids` / `oe_instrument_ids` / `pm_teams` / `oe_teams` 输出,仅保留 `venue_instrument_ids` / `venue_teams` 与 `external_*` 展示视图,不得反向影响 Matching 事件。详细设计见 matching §3.2、sports-event-anchor §6 与 web `/matched_pairs`;matching/web 测试 README 已同步。验证:`pytest tests/arbitrage/web/test_web_gateway.py tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/strategy/test_evaluator.py` → 54 passed。 |
| 2026-07-02 (#184) | **Matching normalizer 删除 `instrument.info["venue"]` 兜底**。继续防止旧/测试字段掩盖真实 venue 主路径:`events_from_instruments()` 现在只从 Venue Registry `venue_id_from_instrument_id(instrument.id)` 或测试 fixture 的 `instrument.id.venue` 取得 venue;若 venue 为空,该 instrument 直接跳过,不再读取 `instrument.info["venue"]`。这样 Provider/InstrumentId 缺 venue 会在 matching 输入层暴露,不会被 info dict 旁路补齐。详细设计见 matching §4.1;matching 测试 README 已同步。验证:`pytest tests/arbitrage/matching/test_normalizer.py tests/arbitrage/matching/test_actor.py tests/arbitrage/matching/test_engine.py tests/arbitrage/matching/test_matched_pair_event.py` → 33 passed。 |
| 2026-07-02 (#183) | **Matching 内部 pair_id suffix 参数从 external 收口到 tradable 命名**。继续清 venue 插拔第二阶段旧语义残留:`MarketMatchingActor._emit_pair()` 调 `_pair_id_for` 时不再使用 `external_venue` 参数名,改为 `tradable_venue`;SE 缺 OE 仍可独立匹配的测试名同步从 `test_sharpexch_external_venue_matches_without_orbitexch` 改为 `test_sharpexch_tradable_venue_matches_without_orbitexch`。行为不变:显式 PM tradable anchor 路径仍按 tradable venue 追加 suffix,PMSPORTS non-tradable anchor 聚合路径仍关闭 suffix。matching 测试 README 已同步。验证:`pytest tests/arbitrage/matching/test_actor.py` → 12 passed。 |
| 2026-07-02 (#182) | **Strategy Action 移除 `share` 兜底,Strategy defaults 不再暴露 `fx`**。用户裁定:无论 condition 还是 checktion,传给 Action 的 `legs/candidates` 都应已带 `share_if_wins` 或 `qty`;`share` 只属于 Check 规划阶段的可选 override,不属于 `place_bets` / `share_limit`。落地:`PlaceBetsAction` 不再接收 `share`,只按 `qty_overrides > leg.qty > leg.share_if_wins` 推最终 qty,缺 sizing 时整次 abort;`ShareLimitModification` 不再接收 `share`,单 legs 缺 sizing 清空,candidate 缺 sizing 丢弃;registry 将未知/错误 Action params 包装为 `StrategyConfigError`,旧 `place_bets.params.share` / `share_limit.params.share` fail-fast。进一步收口 FX 边界:`EvalContext.strategy_defaults` 只暴露 `share/max_leg_share`,`fx` 不进入 Strategy defaults,只留在 adapter 入站/出站边界。详细设计见 strategy §3.7/§3.8;测试 README 与 e2e README 已同步。验证:`pytest tests/arbitrage/strategy/test_check_action_registry.py tests/arbitrage/strategy/test_action_share_limit.py tests/arbitrage/strategy/test_action_place_bets.py tests/arbitrage/strategy/test_mean_rebate_e2e.py tests/arbitrage/strategy/test_json_loader.py tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/e2e/test_mean_rebate_cancel_only.py` → 89 passed;`pytest tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/strategy/test_action_share_limit.py tests/arbitrage/strategy/test_check_one_side_rebate.py tests/arbitrage/strategy/test_mean_rebate_e2e.py` → 39 passed。 |
| 2026-07-02 (#181) | **ArbContext keyed map 共享对象创建收口到 `ctx_map_get_or_create`**。承接 #180,OE/SE factory 都有“从 `browser_manager_by_venue` / `browser_lock_by_venue` 取对象,缺失则创建并回写”的样板。本轮把该模式收口到 `bootstrap.ctx_map_get_or_create(ctx, attr, key, factory)`,OE `_shared_oe_browser_manager`、SE `_shared_se_browser_manager` / `_shared_se_browser_lock` 改用它。行为不变:Data/Exec 仍复用同一个 venue browser manager,SE 仍复用同一个 browser lock;只是共享件生命周期 helper 不再散落在 adapter factory。详细设计见 common §6、data/execution/discovery factory 接线说明;测试 README 同步 risk/OE/SE adapter。验证:`pytest tests/arbitrage/risk/test_bootstrap.py tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py tests/arbitrage/adapters/sharpexch/test_factories.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` → 116 passed。 |
| 2026-07-02 (#180) | **Venue discovery config builder 进入 VenueDescriptor**。承接 #156/#179,`to_arb_context_init_kwargs()` 不再手写 `(ORBITEXCH, to_oe_scraper_config)` / `(SHARPEXCH, to_se_discovery_config)` 列表,而是遍历 `enabled_tradable_venues(cfg)`,按 descriptor 的 `discovery_config_builder` 生成 `discovery_config_by_venue` keyed map。OE/SE 专属 builder 仍保留各自 payload/浏览器字段逻辑,但新增 external venue 时只需在 descriptor 声明 discovery builder,dispatcher 不再维护 venue 列表。Web `/matched_pairs.external_venue` 同步改为只读 `venue_instrument_ids` 的 external venue,不再从 instrument id 后缀反推。详细设计见 common §7、configuration ArbContext 表、venues §3/§5.2、web `/matched_pairs`;测试 README 同步 venues/web。验证:`pytest tests/arbitrage/common/test_venues.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py tests/arbitrage/adapters/sharpexch/test_factories.py` → 130 passed;`pytest tests/arbitrage/web/test_web_gateway.py tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/strategy/test_evaluator.py` → 55 passed。 |
| 2026-07-02 (#179) | **VenueDescriptor 展示分组去 legacy 命名**。承接 #177/#178,`legacy_group` / `is_legacy_primary_venue()` 已只服务 Web `pm_*` / `external_*` 兼容输出,不再参与 Matching 主路径。为避免继续把 PM-primary 旧语义带进 venue registry,本轮改名为 `display_group` / `is_primary_display_venue()`,明确这是 Web 展示分组,不表达 matching anchor、tradable venue 或 settlement capability。行为不变;Web 仍从 `venue_instrument_ids` 派生旧展示字段。详细设计见 common §7、venues §3;Web README 同步。验证:`pytest tests/arbitrage/common/test_venues.py tests/arbitrage/web/test_web_gateway.py`。 |
| 2026-07-02 (#178) | **删除 MatchResult 旧 PM/OE alias,并统一 PMSPORTS sports 配置语义为继承**。承接 venue registry 主路径收口,`MatchResult` 不再暴露 `polymarket_event` / `orbitexch_event` 旧别名,只保留 `anchor_event` / `tradable_event`,避免测试继续把 PM/OE 语义当匹配算法 API。PMSPORTS 目标 competitions 的文档措辞从“fallback/兼容读取旧 discovery”改为“`data_sources.sports_status.sports` 为空时继承 `discovery.polymarket.sports`”,表达这是当前默认配置继承关系,不是旧配置逃生口。详细设计见 matching §2/§4.2、sports-event-anchor §4、venues §5.2;测试 README 同步 matching/discovery。验证:`pytest tests/arbitrage/matching/test_engine.py tests/arbitrage/matching/test_actor.py tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/web/test_web_gateway.py tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/adapters/polymarket/test_sports.py` → 131 passed。 |
| 2026-07-02 (#177) | **MatchedPair 旧 PM/OE 字段不再回填主字段**。承接用户“删掉兼容兜底,把架构调通”的要求,进一步收口 #169/#170:`MatchedPair.__post_init__` 只允许从 `venue_instrument_ids` 补 `tradable_instrument_ids`,不再从 `pm_instrument_ids` / `oe_instrument_ids` 或 instrument id 后缀反推 `venue_instrument_ids`/`tradable_instrument_ids`,也不再自动派生旧投影字段;Web `/matched_pairs` 分组只读 `venue_instrument_ids`,旧 `pm_*`/`oe_*` 仅作为输出展示视图。这样 keyed map 缺失会在 tests/runtime 直接暴露,不会被旧字段掩盖。详细设计见 matching §3.2、web `/matched_pairs`;测试 README 同步 matching/strategy/web。验证:`pytest tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/matching/test_actor.py tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/web/test_web_gateway.py` → 66 passed。 |
| 2026-07-02 (#176) | **配置错误路径继续 fail-fast:PM `ws_url` 只接受 base URL + loader section 类型检查**。承接“配置错就报错”原则,`venues.polymarket.ws_url` 现在只允许配置上游 `PolymarketWebSocketClient` 需要的 base URL(`/ws` 或 `/ws/`,dispatcher 只补尾斜杠);若写成旧 full channel endpoint(`/ws/market` / `/ws/user`)直接抛 `ConfigError`。原因:上游 client 会自行拼接 channel,静默归一会掩盖旧配置仍在用,而不归一会生成 `marketmarket` / `marketuser` 这类错误 endpoint。同时 loader 在 env 注入前检查 `venues` / `venues.<venue>` 显式值必须是 JSON object,避免用户写 `null`/数组时绕过统一 `ConfigError` 变成普通 `AttributeError`;缺省 section 仍按 schema 默认 + env 注入补空 object。详细设计见 configuration §5/§6;测试 README 同步 config。验证:`pytest tests/arbitrage/config/test_dispatcher.py tests/arbitrage/config/test_loader.py` → 108 passed。 |
| 2026-07-02 (#175) | **删除当前 NT schema 中剩余死配置字段**。承接 #171/#174 的“配置错就报错”原则,`RiskSectionConfig` 删除未被 NT runtime 消费的 `execution_enabled` / `health_check_interval_sec` / `match_overrides`;`ExecutionSectionConfig` 删除旧 session/staleness 字段 `tracking_check_interval_sec` / `max_failure_retries` / `staleness_timeout_sec`;PM schema 删除旧 odds_client 地址字段 `user_address` / `eoa_address`(当前上游 PM ExecClient 自行从 CLOB client/funder 推导 wallet/user address);Strategy schema 删除旧服务栈信号定义表 `strategy.signals`(当前 `self_hits` 直接读运行时 `SignalStore`);OE/SE venue schema 删除旧服务栈执行/API/浏览器占位字段 `api_url` / `zoom_level` / `page_refresh_sec` / `cdp_url` / `default_persistence` / `default_order_type` / `discount` / `take_off` / `market_order_enabled` / `supported_market_types`;同时删除没有运行时消费路径的顶层 `discovery.enabled` / `matching.enabled` / `risk.enabled` / `execution.enabled`。这些字段在当前 dispatcher/adapter/web 风控链路均不读取,继续留在 schema 只会让旧配置被误认为生效;旧 `src/arbitrage/services/` 历史栈不动。详细设计见 configuration §5;测试 README 同步 config/strategy。验证:`pytest tests/arbitrage/config/test_loader.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` → 153 passed;`pytest tests/arbitrage/web/test_web_gateway.py` → 26 passed;`pytest tests/arbitrage/strategy/test_json_loader.py tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/strategy/test_mean_rebate_e2e.py` → 54 passed。 |
| 2026-07-02 (#174) | **删除 NT 配置中的 `risk.global_sl` 字段**。用户早已定全局止盈/止损退役,在严格 schema 下继续保留 `global_sl` 作为“兼容字段”会让旧配置仍被接受,与“配置错就报错”不一致。本轮从 `RiskSectionConfig` / `ArbRiskParams` / dispatcher 删除 `global_sl`,Risk runtime 只保留单场 `match_tp/match_sl` profit gates;旧 `risk.global_sl` 现在统一走 unknown-field schema mismatch。老 `src/arbitrage/services/` 历史栈不动。详细设计见 risk §3.1、configuration §5;测试 README 同步 risk。验证:`pytest tests/arbitrage/config/test_dispatcher.py tests/arbitrage/risk/test_engine.py`。 |
| 2026-07-02 (#173) | **Strategy JSON 删除旧单字段 `action` 兼容入口,只接受 `actions` 数组**。承接本轮去 legacy 兜底原则,`condition_from_json` 不再把 `"action": {...}` 静默转成 `actions=[...]`,而是在 decode 时抛 `StrategyConfigError` 并提示使用 `actions` list。示例配置、E2E strategy 测试与 configuration 详细设计均改为 `actions` 数组;保留负向测试覆盖旧字段拒绝。详细设计见 configuration §7.3、strategy JSON loader;测试 README 同步 strategy。验证:`pytest tests/arbitrage/strategy/test_json_loader.py tests/arbitrage/strategy/test_mean_rebate_e2e.py tests/arbitrage/config/test_loader.py`。 |
| 2026-07-02 (#172) | **删除 Strategy Check/Action 中无效 `fx` 参数**。承接 #171,`fx` 只属于顶层 `ArbitrageParams` 与 OE/SE adapter 入站/出站换汇边界;Strategy 中间层已经统一 USD stake 口径,`OneSideRebateCheck` / `MeanRebateRecoveryCheck` / `ShareLimitModification` 的 `fx` 构造参数只保存不读取,会继续形成旧配置逃生口。本轮删除这些参数和旧注释,保留 `share` / `max_leg_share` 的显式 strategy override 语义。详细设计见 strategy §7;测试 README 同步 strategy。验证:`pytest tests/arbitrage/strategy/test_check_mean_rebate_recovery.py tests/arbitrage/strategy/test_action_share_limit.py tests/arbitrage/strategy/test_check_one_side_rebate.py tests/arbitrage/strategy/test_mean_rebate_e2e.py`。 |
| 2026-07-02 (#171) | **删除 `risk.share/max_leg_share/fx` 旧迁移入口,配置 schema 统一拒绝未知字段**。承接 #124 的 ownership 迁移和本轮去 legacy 兜底原则,旧配置字段不再迁移到 `arbitrage.*`,也不允许被 msgspec 静默忽略;所有 ArbConfig Struct 统一继承 `ConfigStruct(forbid_unknown_fields=True)`,所以 `risk.share` / `_doc` / 拼错字段都走同一类 schema mismatch。`RiskSectionConfig` 删除这三个字段,Strategy action 显式 params override 与顶层 `ArbitrageParams` 语义不变;`arb_config.example.json` 同步删除 `_doc` 和旧 `execution.health_check_interval_sec`。详细设计见 configuration §5;测试 README 同步 config。验证:`pytest tests/arbitrage/config/test_loader.py tests/arbitrage/config/test_dispatcher.py`。 |
| 2026-07-02 (#170) | **Web `/matched_pairs` 新字段不再用旧 PM/OE 字段拼接兜底**。承接 #169,继续把兼容逻辑限制在明确边界内:WebGateway 仍输出 `pm_*` / `oe_*` / `external_*` 兼容展示字段,但 `tradable_instrument_ids` 只原样投影 `MatchedPair.tradable_instrument_ids`,不再在 Web 层用 legacy projection 拼一个新字段。这样页面/API 能暴露上游事件主字段是否真实存在,不会被 Web 兼容层掩盖。详细设计见 web §8.4,测试 README 同步 web。验证:`pytest tests/arbitrage/web/test_web_gateway.py`。 |
| 2026-07-02 (#169) | **MatchedPair 旧字段兼容归一收口到事件类,Strategy 只读 `tradable_instrument_ids`**。承接 #166/#168,继续删除会掩盖 keyed-map 主路径的下游 fallback:`MatchedPair.__post_init__` 现在负责把旧代码直构的 `pm_instrument_ids` / `oe_instrument_ids` 回填为 `tradable_instrument_ids` / `venue_instrument_ids`;`StrategyEvaluator._ensure_obd_subscribed` 不再 fallback 读取旧 PM/OE 字段,只消费 `tradable_instrument_ids`。这样旧 payload 兼容有单一归属,而 Strategy 测试可直接暴露 keyed map 是否接通。详细设计见 matching §3.2、strategy `on_start`/OBD 订阅说明;测试 README 同步 matching / strategy。验证:`pytest tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/web/test_web_gateway.py`。 |
| 2026-07-02 (#168) | **删除旧 PM-primary anchor 命名 helper**。承接 #167,`anchor_venues()` / `enabled_anchor_venue_ids()` / `is_anchor_venue()` 只剩测试/文档引用,生产路径已不再使用;为避免与 PMSPORTS event anchor 语义混淆,从 `common.venues` 删除这些旧名 helper。`legacy_group` 与 `is_legacy_primary_venue()` 保留,只服务 `MatchedPair`/Web 旧 PM/OE 兼容投影。详细设计见 common §7、venues §4。验证:`pytest tests/arbitrage/common/test_venues.py` 通过。 |
| 2026-07-02 (#167) | **删除 MarketMatchingConfig 旧 PM/external 输入字段,matching 配置入口只剩 anchor/tradable**。承接 #166,继续防止旧入口掩盖 Venue Registry 通路:`MarketMatchingConfig` 删除 `pm_venue` / `oe_venue` / `external_venues`,actor 初始化只读取 `anchor_venue` / `tradable_venues`;dispatcher 不再生成 legacy `external_venues`;`enabled_external_venues()` / `enabled_external_venue_ids()` helper 删除。Web 的 `external_*` 输出字段仍保留为前端兼容视图,不属于 matching 配置入口。详细设计见 matching §3.3、venues §5.2/§5.3、configuration dispatcher 表;测试 README 同步 matching / venues / config / SE adapter。验证:`pytest tests/arbitrage/common/test_venues.py tests/arbitrage/matching/test_actor.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` 通过。 |
| 2026-07-02 (#166) | **MatchedPair keyed venue map 成为事件主通路,PM/OE 旧字段降为派生兼容投影**。承接 #158/#165,继续避免旧字段掩盖 venue 插拔路径:`MarketMatchingActor` 发布 `MatchedPair` 时不再手工维护 `pm_instrument_ids` / `oe_instrument_ids`,只传 `venue_instrument_ids` / `tradable_instrument_ids` / `anchor_instrument_ids`;`MatchedPair.__post_init__` 按 Venue Registry legacy primary 规则自动派生旧 PM/OE 投影,旧 payload 反序列化仍按后缀回填 venue map。详细设计见 matching §3.2/§4.2,测试说明见 matching README。验证:`pytest tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/matching/test_actor.py tests/arbitrage/web/test_web_gateway.py tests/arbitrage/strategy/test_evaluator.py` 通过。 |
| 2026-07-02 (#165) | **删除 ArbContext 旧 venue 专属字段兜底,keyed map 成为唯一 factory 注入通路**。用户指出兼容字段会掩盖 keyed map 是否真正接通,故在 #161-#164 的"优先读取"基础上继续收口:从 `ArbContext` 与 `to_arb_context_init_kwargs` 删除 `pm_session_timeout_secs` / `oe_session_timeout_secs` / `se_session_timeout_secs`、`oe_scraper_config` / `se_discovery_config`、`*_aliases`、`*_instrument_provider`、`*_browser_manager/lock` 等专属投影;PM/OE/SE Data/Exec factories 与 PM/PMSPORTS provider 不再读取旧字段 fallback,只读 `session_timeout_secs_by_venue` / `discovery_config_by_venue` / `*_aliases_by_venue` / `target_competitions_by_data_source` 等 keyed map。新增 `ctx_map_require` 用于必需 keyed 值 fail-fast,缺 session timeout 不再静默用默认值。详细设计见 common §6、configuration §8、data/discovery/execution factory 段;测试 README 同步 discovery / OE / launcher / risk。验证:`pytest tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py tests/arbitrage/adapters/sharpexch/test_factories.py tests/arbitrage/adapters/polymarket/test_sports.py tests/arbitrage/debug/test_debug_exec_factories.py tests/arbitrage/execution/test_factories.py tests/arbitrage/risk/test_bootstrap.py` 通过。 |
| 2026-07-02 (#164) | **ArbContext keyed map 读写 helper 收口到 bootstrap**。在 #162/#163 三类 factory 已切 keyed map 优先后,移除 PM/OE/SE factory 内重复的 `_ctx_map_get/_ctx_map_set`,新增 `bootstrap.ctx_map_get` / `ctx_map_set` 作为统一 helper;行为不变,只是防止后续 adapter 继续复制同类函数。同时修正 launcher 测试 README 中 InstrumentRefresher/4 actor 的过时描述。测试:`pytest tests/arbitrage/adapters/polymarket/test_sports.py tests/arbitrage/debug/test_debug_data_factories.py tests/arbitrage/debug/test_debug_exec_factories.py tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py tests/arbitrage/adapters/sharpexch/test_factories.py` 通过。 |
| 2026-07-02 (#163) | **PM/PMSPORTS factories 也切到 ArbContext keyed map 优先**。在 #162 OE/SE factory 迁移后,继续把 PM/PMSPORTS 补齐:`PolymarketSportsLiveDataClientFactory` 与 `ArbPolymarketInstrumentProvider` 优先读取 `target_competitions_by_data_source["PMSPORTS"]` / `competition_to_sport_by_data_source["PMSPORTS"]`;`ArbPolymarketLiveDataClientFactory` 回写 `instrument_provider_by_venue["POLYMARKET"]`;`ArbPolymarketLiveExecClientFactory` session timeout 优先读 `session_timeout_secs_by_venue["POLYMARKET"]`。专属 `pm_*` 字段继续兼容,行为不变。详细设计见 data/discovery/execution;测试说明见 PM adapter / discovery / debug README。 |
| 2026-07-02 (#162) | **OE/SE factories 开始消费 ArbContext venue keyed map**。承接 #161,`OrbitExchLiveDataClientFactory` / `ArbOrbitExchLiveExecClientFactory` 与 `SharpExchLiveDataClientFactory` / `ArbSharpExchLiveExecClientFactory` 已优先读取 `discovery_config_by_venue`、`*_aliases_by_venue`、`session_timeout_secs_by_venue`、`browser_manager_by_venue`;构造后同步回写 `instrument_provider_by_venue` / `browser_manager_by_venue` / SE `browser_lock_by_venue`,专属 `oe_*` / `se_*` 字段保留兼容。行为不变,但 factory 注入入口从 venue 专属字段迁到 registry map。详细设计见 common §6、data/discovery/execution 相关 factory 段;测试说明见 OE/SE adapter 与 debug README。 |
| 2026-07-02 (#161) | **ArbContext 增加 venue/data-source keyed registry map**。承接 venue 插拔第二阶段,在保留现有 `pm_*` / `oe_*` / `se_*` factory 字段的同时,新增 `session_timeout_secs_by_venue`、`discovery_config_by_venue`、`sport_aliases_by_venue` / `competition_aliases_by_venue`、provider/browser 通用槽位和 PMSPORTS data-source target maps。dispatcher 先按 enabled trading venues / enabled data source 派生这些 map,launcher bootstrap 验证注入;行为不变,先建立后续 adapter/factory 去硬编码的统一入口。详细设计见 common §6、configuration §8;测试说明见 config/launchers README。 |
| 2026-07-02 (#160) | **清掉 common utils 中残留的应用层 size gate**。早期 Q12 已决定 `MIN_SIZE_POLYMARKET` / `MIN_SIZE_ORBITEXCH` / `check_min_size` / `adjust_share_by_liquidity` Step 2 删除,最小下单额完全交给 instrument 元数据 + NT RiskEngine;本轮把 `src/arbitrage/common/utils.py` 和 `utils_api.md` 中残留实现/文档删掉。旧 services 栈不再从 common utils 导入 size gate:strategy service 只保留私有流动性等比例缩放,execution recovery 不再用应用层最小额判断提前完成。详细设计见 common §intro、risk §1/§3.1;测试说明见 risk/strategy README。 |
| 2026-07-02 (#159) | **venue 插拔语义清理:launcher / dispatcher / docs 不再把 registry 路径描述成 legacy fallback**。承接 #158,当前代码仍保留显式 legacy PM-anchor 输入兼容,但不再称为默认兜底;launcher 顶部说明和测试 README 从“PM+OE 四 client / InstrumentRefresher”更新为 enabled data source + enabled venue registry 注册路径,`sports_status` 缺失的错误文案也改为 PMSPORTS data source 语义。行为不变;详细设计见 configuration / matching / venues,测试说明见 launchers/matching README。 |
| 2026-07-02 (#158) | **收回 legacy primary/external 默认兜底**。用户指出 legacy 兜底会让测试无法判断当前 venue registry / `venue_instrument_ids` 修订是否真实生效。定论:当前 Matching/Web 运行路径必须依赖显式 `anchor_venue/tradable_venues`、`venue_instrument_ids` 或可从 instrument id 后缀识别的真实 venue;缺字段/缺后缀时不再默认补 PM/OE。`legacy_primary_venue_id()` / `legacy_external_venue_id()` 删除;legacy PM-anchor 单 venue pair_id 统一带 venue 后缀,默认 dispatcher 的 PMSPORTS 聚合路径不变。详细设计见 matching §3.2/§3.3/§4.3、web §8.4;测试说明见 matching/web/venues README。 |
| 2026-07-02 (#157) | **legacy external 兼容兜底也收口到 Venue Registry**。#154 已把旧 `pm_*` 回填从 `POLYMARKET` 常量改为 `legacy_primary_venue_id()`,但 Matching legacy PM-anchor pair_id 规则和 Web 旧 `oe_instrument_ids` 回填仍直接把 `ORBITEXCH` 当兜底。本轮新增 `legacy_external_venue_id()`,仅服务旧 `oe_*` payload/前端兼容回填和 legacy pair_id 规则;当前返回 ORBITEXCH,生产 PMSPORTS anchor 聚合路径行为不变。详细设计见 common §7、matching §3.2/§4.3、web §8.4;测试说明见 venues/matching/web README。 |
| 2026-07-02 (#156) | **Dispatcher 的 OE/SE discovery 启用判断改为 descriptor `config_key` 统一派生**。在 #155 后继续收口配置接线:保留 `to_oe_scraper_config` / `to_se_discovery_config` 两个 adapter 专属构造函数,但内部通过 `_enabled_venue_discovery_sections(cfg, venue)` 读取 Venue Registry descriptor 的 `config_key`,统一判断 runtime venue enabled + `discovery.<venue>.enabled`,不再让每个函数各自手写 config 路径判断。行为不变;专属 config 构造仍读取各自 `cfg.venues.<key>` 字段。详细设计见 configuration §8;测试说明见 config README。 |
| 2026-07-02 (#155) | **PM settlement 启用判断收口到 Venue Registry capability**。#151–#154 已把 launcher/factory/web/matching/risk 的主要 venue 判断收口到 registry,但 `_make_pm_settlement()` 仍直接以 `POLYMARKET` 常量判断是否启用。本轮新增 `enabled_settlement_venues(cfg, "polymarket_ctf")`,launcher 通过 descriptor capability + enablement 决定是否初始化 PM CTF settlement;PM 专属 `PolymarketSettlement` 业务本身暂不泛化、不下沉,仍按前序 TODO 保留。详细设计见 `_cross-cutting/venues.md §5.1`、common §7;测试说明见 venues README `venues-7` 与 launchers README。 |
| 2026-07-02 (#154) | **legacy `pm_*` 字段回填也收口到 Venue Registry**。#151 已把 primary/external 判断改用 `is_legacy_primary_venue()`,但旧 payload 缺 `venue_instrument_ids` 时,`MatchedPair.from_dict()` 和 Web 回填仍直接把 `pm_instrument_ids` 放进 `POLYMARKET`。本轮新增 `legacy_primary_venue_id()` helper,用于旧 `pm_*` 兼容字段的默认 primary venue 回填;Matching/Web 不再在回填路径写死具体 venue。当前行为不变(PM 仍是唯一 legacy primary),真实多 venue schema 仍以 `venue_instrument_ids` / `tradable_instrument_ids` 为准。详细设计见 common §7、matching §3.2、web §5/§8.4。 |
| 2026-07-02 (#153) | **Risk 余额模型命名从 PM/OE 收口到 probability/decimal capability**。`_check_balance` 已经按 Venue Registry `odds_model` 分流,但内部自扣函数仍叫 `_pm_open_notional`,文档也把自扣模型写成 PM 专属。本轮仅做命名/文档收口:`_probability_open_notional` 表达 probability venue 自扣在途挂单,decimal venue 继续直接信 WS/cache free。行为不变,当前实例仍是 PM=probability、OE/SE=decimal。详细设计见 risk §3.1;测试说明见 risk README `risk-6.3b`。 |
| 2026-07-02 (#152) | **Web 控制台展示层去 PM/external 写死**。为配合 venue 插拔化第二阶段,Web 静态页不再把 `POLYMARKET` 固定成唯一主腿、把其它 venue 固定成 external 展示:Discovery 统计/过滤从 `/instruments` 实际 venue 动态生成,Matching 表按 `venue_teams` 展示全部 venue,Odds 表按 role 展示全部 venue。`/odds` leg 新增 `odds_model`,前端按 Venue Registry 模型把 decimal odds 换算成隐含概率,probability odds 原样显示。旧 `/matched_pairs` 的 `pm_*`/`oe_*`/`external_*` 字段仍保留为兼容视图,真实结构继续看 `venue_instrument_ids` / `tradable_instrument_ids`。详细设计见 web §5/§8.4;测试说明见 web README `web-7.13`。 |
| 2026-07-02 (#151) | **legacy primary/external 投影也收口到 Venue Registry helper**。在 #150 `legacy_group` 基础上新增 `is_legacy_primary_venue()`;MatchingActor 聚合路径、`MatchedPair.from_dict()` 回填旧 `pm_instrument_ids/oe_instrument_ids`、Web `/matched_pairs` 的 `external_*` 兼容视图都改用该 helper 划分 primary/external,不再在投影层直接写 `venue != POLYMARKET`。旧 `is_anchor_venue()` 保留为 alias。真实多 venue 结构仍以 `venue_instrument_ids` / `tradable_instrument_ids` / `anchor_instrument_ids` 为准,生产 matching 行为不变。 |
| 2026-07-02 (#150) | **VenueDescriptor 清掉 PM `role=anchor` 旧语义**。PMSPORTS 已是当前 matching anchor,PM 只是 enabled tradable venues 之一;继续在 venue capability 里把 POLYMARKET 标成 `role="anchor"` 会误导后续插拔化设计。本轮把 `VenueDescriptor.role` 改为 `legacy_group`,POLYMARKET=`primary`,OE/SE=`external`;旧 `anchor_venues` / `is_anchor_venue` helper 保留为兼容名,明确只表示 PM primary 兼容分组,不代表 PMSPORTS matching anchor。生产 matching 行为不变。详细设计见 `_cross-cutting/venues.md` 与 common §7。 |
| 2026-07-02 (#149) | **收回 PMSPORTS 常规显式配置面:默认复用 PM sports**。用户确认现阶段 PMSPORTS discovery 与 PM discovery 使用同一套 sports 过滤,没有必要在 `arb_config.example.json` 或 Web 控制台重复配置 `data_sources.sports_status.sports`。代码保留 `data_sources.sports_status` schema / registry / dispatcher fallback 能力,用于未来 provider/ws_url 或 PMSPORTS 目标与 PM 分离时覆盖;常规配置不写该段,由默认值启用 PMSPORTS,目标 competitions fallback 到 `discovery.polymarket.sports`。本轮删除 example 中显式 `data_sources` 段,Web Discovery Config 收回 PMSPORTS 标签页,详细设计见 configuration / venues / sports-event-anchor / web。 |
| 2026-07-02 (#148) | **Web Discovery Config 增加 PMSPORTS data source 标签页**。承接 #147,PMSPORTS target competitions 已迁到 `data_sources.sports_status.sports`,控制台配置面同步新增 PMSPORTS 标签页编辑该字段;Polymarket / OrbitExch / SharpExch 标签页仍分别编辑 `discovery.<venue>.sports`;OE/SE 的 `page_load_timeout_sec` / `staleness_timeout_sec` 继续作为统一 browser discovery UI 值同步写入两个 venue 段。详细设计见 web §8.2,测试说明见 web README `web-7.13`。 |
| 2026-07-02 (#147) | **PMSPORTS target competitions 迁到 `data_sources.sports_status.sports`**。在 #146 独立 data source 基础上,给 `SportsStatusDataSourceConfig` 增加 `sports: list[SportFilter]`;dispatcher 的 PMSPORTS target 派生优先读取 `data_sources.sports_status.sports`,为空时兼容 fallback 到旧 `discovery.polymarket.sports`。这样 OE+SE-only 配置不再需要把 PMSPORTS 目标赛事写在 PM discovery 命名下;旧配置仍可工作。同步 arb_config.example、configuration / venues / sports-event-anchor 设计和 config/discovery 测试 README。验证:`pytest tests/arbitrage/config/test_dispatcher.py tests/arbitrage/common/test_venues.py tests/arbitrage/launchers/test_arb_node.py` → 119 passed。 |
| 2026-07-02 (#146) | **PMSPORTS 从 trading venue descriptor 剥离为独立 data source**。新增 `data_sources.sports_status` 配置段(默认 enabled,provider=`polymarket_sports`,可覆盖 `ws_url`)与 `DataSourceDescriptor` / `DATA_SOURCE_REGISTRY["sports_status"]`;`build_trading_node_config` / `register_factories` 先注册 enabled data sources,再注册 enabled trading venues。因此 PM disabled + OE+SE enabled 时会注册 `PMSPORTS`/OE/SE,不再因 PM trading venue 关闭 fail-fast;缺 sports data source 时仍 `ConfigError`。PMSPORTS target competitions 暂继续复用历史 `discovery.polymarket.sports`,但不再受 `venues.polymarket.enabled` 影响。同步 configuration / venues / sports-event-anchor / common / sharpexch 设计和 config/launcher/venue/SE adapter 测试 README。验证:`pytest tests/arbitrage/common/test_venues.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` → 117 passed。 |
| 2026-07-02 (#145) | **PMSPORTS anchor / venue capability 文档状态对齐**。第二阶段继续前先清理设计文档漂移:当前代码默认已是 `PMSPORTS` non-tradable anchor + enabled tradable venues 聚合匹配,PairRegistry/MatchedPair/Strategy OBD+snapshot/ended eviction 均已落地;旧 PM anchor 只保留为 legacy fallback。同步修正 `sports-event-anchor.md` 的成熟度与落地状态、`venues.md` 的 enablement 语义、`common`/`matching`/`sharpexch` 设计中的“当前仍 PM anchor”表述。当前剩余真实限制是 PMSPORTS data source 仍挂在 `POLYMARKET` descriptor 下,所以 OE+SE-only 仍 fail-fast;下一步第二阶段应先把 PMSPORTS 从 trading venue descriptor 中剥离为独立 data source。纯文档纠偏,无代码行为变化。 |
| 2026-07-02 (#144) | **SE probe/生产 BrowserManager 与 profile 复用边界澄清**。确认 SE 生产 Data/Exec factory 与 `scripts/se_*_probe.py` 都经 `nautilus_trader.adapters.sharpexch.browser_manager.PlaywrightBrowserManager`,该导出复用 OE browser manager 的 anti-detection/visibility 初始化(`AutomationControlled` 参数、固定 user-agent、隐藏 `navigator.webdriver`、模拟 plugins、页面 visible spoof),因此这些设置在生产 launcher 生效。区别是 probe 的 `--user-data-dir` 只是命令行覆盖;生产若要复用人工 Cloudflare 验证过的 profile/cookie,必须显式配置 `venues.sharpexch.user_data_dir`。`arb_config.example.json`、configuration/sharpexch 设计与 config/SE adapter 测试 README 已同步,并补 `test_dispatcher.py` 断言 SE Data/Exec config 透传 `user_data_dir`。 |
| 2026-07-02 (#143) | **SharpExch prices WS liveness 收口 + 第一阶段验收边界澄清**。补齐 SE competition prices WS 的静默断流检测:复用 OE #109 handler 内部模型,`SharpExchWebSocketHandler` 可选接入 NT `clock + liveness_timeout_secs`,仅目标 feed(competition 页为 `prices`)的非空入向帧刷新 `_last_frame_ns`,time-alert 到期无帧则触发 `on_disconnect("liveness_timeout")`;`SharpExchDataClient` 把 `venues.sharpexch.staleness_timeout_sec` 接入该 handler,现有 `close:prices` / `liveness_timeout` reload gate 复用不变。清理 SE 文档漂移:`matched` currentBets 已由真成交 probe 验证,不再标待验证;完整真钱套利端到端 E2E 明确延后到 venue 插拔化第二阶段完成后再测,第一阶段只收口 adapter probes 与 skip node smoke。sharpexch 详细设计、adapter 测试 README 与 e2e README 已同步。 |
| 2026-07-02 (#142) | **Web Discovery Config 对齐三 venue 与统一 browser discovery 参数**。配置 schema 已有 `discovery.sharpexch`,但页面仍只露 PM/OE sports textarea,且 `page_load_timeout_sec` / `staleness_timeout_sec` 被 UI 当 OE 专属。按用户裁定改为:Polymarket / OrbitExch / SharpExch 三标签页分别编辑 `discovery.<venue>.sports`;timeout/staleness 作为 OE/SE 共用 UI 值,保存时同步写入 `venues.orbitexch` 与 `venues.sharpexch`。不改 schema/dispatcher。web 详细设计与测试 README 已同步。 |
| 2026-07-02 (#141) | **Web `/matched_pairs` 补齐 3+ venue 聚合展示字段**。PMSPORTS anchor 默认聚合 PM/OE/SE 到同一 pair 后,旧 `external_venue` 只能表达第一个非 PM venue。本轮保持旧字段兼容,新增 `external_venues` 与 `venue_teams` map;控制台 Matching 表优先按 `venue_teams` 展示全部 non-PM venue。生产交易路径不变。web 详细设计与测试 README 已同步,新增 `test_matched_pairs_exposes_all_venue_teams_for_aggregated_pair`。 |
| 2026-07-02 (#140) | **补齐 PMSPORTS anchor 不进 Strategy 快照的离线验收**。新增 `test_snapshot_uses_tradable_pair_ids_not_anchor_ids`:即使 `PairRegistry` 同 pair 登记了 `.PMSPORTS` anchor id,`build_snapshot()` 仍只取默认可交易腿,order_books/positions/instrument_info 均不包含 anchor。配合既有 OBD 订阅测试,订阅+快照入口已证明跳过 PMSPORTS anchor;最终 submit spec 仍待端到端 smoke。测试 README 已同步。 |
| 2026-07-02 (#139) | **补齐 PMSPORTS anchor ended eviction 离线验收**。新增 `test_pmsports_anchor_ended_evicts_aggregated_pair`:PMSPORTS synthetic anchor 聚合出的 pair 收到 `SportsGameUpdate.ended=True` 后,`PairRegistry` unregister、`_emitted_pairs` 清理、`_ended_games` 记录,下一轮不会重新 match。生产逻辑不变;matching 测试 README 已同步。 |
| 2026-07-02 (#138) | **PMSPORTS 聚合路径 legacy projection 命名澄清**。`MarketMatchingActor` non-tradable anchor 聚合路径内部变量从 `pm_ids/other_ids` 改为 `legacy_pm_ids/legacy_other_ids`,明确它们只负责填充 `MatchedPair.pm_instrument_ids/oe_instrument_ids` 兼容字段;真实多 venue 结构仍以 `venue_instrument_ids` / `tradable_instrument_ids` 为准。行为、schema、pair_id 均不变。matching 详细设计与测试 README 已同步。 |
| 2026-07-02 (#137) | **MatchResult 字段从 PM/OE 命名泛化为 anchor/tradable**。第二阶段 venue 插拔继续清旧语义:`src/arbitrage/matching/engine.py` 的 `MatchResult` 主字段改为 `anchor_event` / `tradable_event`,`MarketMatchingActor` 改读新字段;旧 `polymarket_event` / `orbitexch_event` 保留为只读兼容 alias。算法、pair_id、匹配结果不变。详细设计见 matching §1/§4.2;测试 README 已同步。 |
| 2026-07-02 (#136) | **PMSPORTS anchor 聚合 pair_id 去掉 OE 哨兵语义**。`MarketMatchingActor` 的 non-tradable anchor 聚合路径原先为了复用“OE 不加 suffix”的兼容规则,向 `_pair_id_for` 传 `external_venue=ORBITEXCH`。本轮改为显式 `include_venue_suffix=False`:同一 PMSPORTS event 下 PM/OE/SE tradable venues 仍共用基础 pair_id,但实现不再把 OE 当 pair_id 生成哨兵。详细设计见 matching §4.3;测试 README 已同步。 |
| 2026-07-02 (#135) | **Venue enabled 判断收口到 registry helper**。在 #134 基础上新增 `is_venue_enabled(cfg, venue)`:通过 `VenueDescriptor.config_key` 读取 `venues.<key>.enabled`。dispatcher 的 PM/OE/SE discovery context gate 与 launcher 的 PM settlement enabled gate 改走该 helper;专属 config 构造仍读取各 venue 子配置字段。行为不变,只是移除运行编排层对 `cfg.venues.polymarket/orbitexch/sharpexch.enabled` 路径的重复认知。详细设计见 `_cross-cutting/venues.md` 与 configuration §8;测试计划已同步。 |
| 2026-07-02 (#134) | **Venue Registry 增加 tradable venue helper,避免把 runtime venue 与 PMSPORTS client 混淆**。第二阶段继续小步收口:`enabled_tradable_venues()` / `enabled_tradable_venue_ids()` 当前等价于 enabled runtime venue(PM/OE/SE 都是可交易 venue),但语义明确不包含 `.PMSPORTS` sports client;`to_market_matching_actor_config()` 改用该 helper 填 `tradable_venues`。行为不变,只是把 matching 输入的“可交易 venue”意图写进 API。详细设计见 common §7 与 `_cross-cutting/venues.md`;测试计划已同步。 |
| 2026-07-02 (#133) | **MatchedPair legacy payload 回填也收口到 Venue Registry helper**。补齐 #131 的一个边角:旧 `pm_instrument_ids` / `oe_instrument_ids` payload 在 `MatchedPair.from_dict()` 中回填 `venue_instrument_ids` 时,不再手写字符串 suffix 解析,统一走 `venue_id_from_instrument_id()`;未知 suffix 只保留在兼容旧字段/`tradable_instrument_ids`,不会进入 venue map。纯内部解析入口统一:不改 schema、不改 matching 算法、不改 pair_id。测试 README 已同步。 |
| 2026-07-02 (#132) | **Strategy share_limit 分支从 PM/OE 名称收口到 odds_model**。继续第二阶段 venue capability,只改 `ShareLimitModification` 内部 remaining 计算入口:probability venue 用真实 `venue` 查 `portfolio.outcome_shares_for_venue(..., venue.lower())` 并做单腿独立限额;decimal odds venue 继续按 merge 后净 share 计算 remaining。行为对当前 POLYMARKET/OE/SE 不变,但不再把非 decimal 分支写死为 `"polymarket"`。详细设计见 strategy §3.8;测试 README 已同步。 |
| 2026-07-02 (#131) | **venue 解析入口统一到 Venue Registry helper**。延续 #130,只收口内部读取点:`events_from_instruments` 与 PMSPORTS 聚合路径的 `_venue_of` 先经 `venue_id_from_instrument_id()` 解析 NT `InstrumentId` / 字符串后缀,再兼容测试 fixture 的 `instrument.id.venue` / `info["venue"]`;Risk/Portfolio 中从 order/position instrument 取 venue 的兜底入口同样改走该 helper。纯重构:不改匹配算法、不改 pair_id、不改 anchor/tradable 数据流、不改 Risk 门控顺序。详细设计见 matching §4.1;测试 README 已同步。 |
| 2026-07-02 (#130) | **Venue Registry helper 小切片收口**。在 #129 schema 清理后,继续推进第二阶段 venue capability,但不做完整 venue graph / adapter 重构。本轮只把上层散落的能力判断收回 `src/arbitrage/common/venues.py`:新增 `enabled_anchor_venue_ids` / `enabled_sports_firehose_venues` / `enabled_sports_client_ids` / `is_anchor_venue` / `is_probability_odds_venue` / `venue_preference_rank`;`mean_rebate` 同概率 tie-break 改走 `venue_preference_rank`;Web matched-pair venue 解析、Matching legacy projection 常量、launcher PM/PMSPORTS 校验改为 sports firehose capability 校验。行为保持当前 PM/PMSPORTS anchor + enabled tradable venues 模型不变(当前静态 registry 仍只有 POLYMARKET 提供 PMSPORTS client)。详细设计见 common §7 与 `_cross-cutting/venues.md`;测试 README 已同步。验证:`pytest tests/arbitrage/common/test_venues.py tests/arbitrage/strategy/test_check_mean_rebate.py tests/arbitrage/web/test_web_gateway.py tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/matching/test_actor.py tests/arbitrage/launchers/test_arb_node.py` → 104 passed。 |
| 2026-07-02 (#129) | **MatchedPair schema 先行清理:旧 PM/OE 字段降级为兼容投影**。用户指出 #128 后仍让 `oe_instrument_ids` 承载“所有非 PM tradable venue”会在 PMSPORTS + 3+ venue 下语义含糊,应先处理。定论并落地:Matching 的真实事件契约改为 `anchor_instrument_ids` / `tradable_instrument_ids` / `venue_instrument_ids`;`pm_instrument_ids` / `oe_instrument_ids` 保留给旧消费者,但不再作为多 venue 真理源。Strategy OBD 订阅改读 `tradable_instrument_ids`,Web `/matched_pairs` 输出 venue map 并保留旧 `external_*` 视图。详细设计见 matching §3.2,消费者说明见 strategy/web architecture;测试 README 已同步。验证:`pytest tests/arbitrage/matching/test_matched_pair_event.py tests/arbitrage/matching/test_actor.py tests/arbitrage/strategy/test_evaluator.py tests/arbitrage/web/test_web_gateway.py` → 59 passed。 |
| 2026-07-02 (#128) | **PMSPORTS event anchor 首个代码落地**。在 #127 设计基础上落地三块:① `PolymarketSportsInstrumentProvider` 复用公开 Gamma `/sports` + `/events?series_id=` 目标 discovery,每场产一条 `.PMSPORTS` non-tradable synthetic `BettingInstrument`,写 Q9 6-key + `game_id` + `tradable=false` + `anchor=true`;`PolymarketSportsDataClient._connect` 先 load anchors 灌 NT cache,再继续开 Sports WS firehose,并按 60min 周期重抓。② `MarketMatchingConfig` 增 `anchor_venue/tradable_venues`;dispatcher 当前输出 `anchor_venue=PMSPORTS`,`tradable_venues=enabled_venue_ids(cfg)`,legacy `external_venues` 保留兼容。`MarketMatchingActor` 对 non-tradable anchor 走聚合路径:同一 PMSPORTS event 下匹配到的 PM/OE/SE 可交易腿合成一个 pair,PairRegistry 默认只返回可交易腿,anchor ids 单独登记。③ launcher 仍要求 POLYMARKET enabled,但理由从“PM anchor”修正为“PMSPORTS anchor 当前随 PM sports data client 注册”;OE+SE-only 仍待把 PMSPORTS 剥离成独立 data source。测试:`pytest tests/arbitrage/adapters/polymarket/test_sports.py tests/arbitrage/matching/test_pair_registry.py tests/arbitrage/matching/test_actor.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` → 116 passed。 |
| 2026-07-02 (#127) | **PMSPORTS event anchor 设计锁定**。用户澄清不是让 PMSPORTS 替代 PM venue anchor,也不删除 PM discovery,而是让 PMSPORTS 也执行公开 Gamma discovery,产出 `.PMSPORTS` 后缀的 non-tradable synthetic event instruments 参与 matching,但不进入套利流。定论:新增横切设计 `architectures/_cross-cutting/sports-event-anchor.md`。核心边界:PM discovery 保留并继续产出可交易 `.POLYMARKET`;PMSPORTS discovery 产出 event-level `.PMSPORTS` anchor,`tradable=false`;Matching 目标语义从 `PM tradable instruments × external venues` 迁到 `PMSPORTS event anchor × enabled tradable venues`;PairRegistry/MatchedPair 后续需区分 anchor ids 与 tradable ids;Strategy/Risk/Execution 默认只消费 tradable ids,不能订阅/下单/风控 `.PMSPORTS`。本轮仅设计与文档指针,无代码行为变化。 |
| 2026-07-01 (#126bh) | **SharpExch 真单 place+cancel 通过 + SE USD 原生口径修正**。用户明确授权后多次跑 `scripts.se_place_cancel_probe --confirm`。先定位到失败链路:① Cloudflare 偶发拦 `sport/details`,probe 增 `--challenge-wait` 并支持 `--market-id/--selection-id` 跳过 discovery;② 真实 place 触达后 EX002 的根因之一是 SE payload 被错误按 `size/fx` 发成 9.02,低于 SE 最小 stake 12;③ 跳过 discovery 时 `document.cookie` 可能读不到 CSRF,executor 增 browser context cookies 兜底注入。依据手动 capture 与 live `CURRENT_BETS.currency=USD`,修正 SE 为 USD 原生:Provider `min_notional=Money(12,USD)`,ExecutionClient runtime 的 `BALANCE` / `CURRENT_BETS` / fill / place payload 均不乘除 fx,`_current_fx()` 固定 1.0。最终 live 通过:指定 `market_id=1.259494210,selection_id=8960879`,下 `BACK @100,size=12` 得 `venue_order_id=22157223`,收到 `CURRENT_BETS` working 单(`remaining=12.0,matched=0.0`)并派生 1 条 status report,随后 `_cancel_order` 撤掉,finally 兜底活单数 0。测试:`pytest tests/arbitrage/adapters/sharpexch/test_provider.py tests/arbitrage/adapters/sharpexch/test_probe_script.py tests/arbitrage/adapters/sharpexch/test_executor.py tests/arbitrage/adapters/sharpexch/test_execution_translation.py tests/arbitrage/adapters/sharpexch/test_execution_client.py tests/arbitrage/adapters/sharpexch/test_factories.py` → 87 passed。 |
| 2026-07-01 (#126bg) | **SharpExch “已登录但没下单”卡点定位并修复**。用户观察到 headed browser 已登录且弹窗消失,但 probe 没有下单。复跑 dry-run 后确认代码并未进入 `_submit_order`:先是 `se_login` 对 SE iframe app 仍按主 URL `**/customer**` 等待,登录成功后可能白等;随后 discovery 若 browser fetch 挂起也会静默停在 `sport/details`。修正:① `_wait_after_login` 优先等 customer iframe,再 fallback 主 URL;② `se_dismiss_post_login_popup` 增 `timeout_ms`,登录/probe 路径用短超时,避免手动关弹窗后两个 context 各等 7s;③ `se_fetch_json` 增 AbortController 超时并返回 `status=0/fetch_error`;④ `se_place_cancel_probe` 打印 `sport/details` request/response 并支持 discovery/fetch 超时。验证:不带 `--confirm` 的 dry-run 已越过登录/弹窗/`sport/details` 分页(5 页 200)、发现 490 条 SE instrument,构造 `BACK @100,size=12` 待下单对象;按设计未调用 placeBets。真单 place+cancel 仍需用户另行明确授权跑 `--confirm`。测试:`pytest tests/arbitrage/adapters/sharpexch/test_probe_script.py tests/arbitrage/adapters/sharpexch/test_provider.py tests/arbitrage/adapters/sharpexch/test_executor.py tests/arbitrage/adapters/sharpexch/test_execution_translation.py tests/arbitrage/adapters/sharpexch/test_execution_client.py` → 81 passed。 |
| 2026-07-01 (#126bf) | **SharpExch place/cancel 按真实前端请求校准**。用户确认后,用 headed Playwright 登录 SE 并监听用户手动触发的 `placeBets` / `cancelBets`,捕获到成功 BACK@100,size=12 下单与随后撤单:SE 成功下单 payload 使用 `persistenceType=LAPSE`、`page=competition`、`showLayOddsEnabled=false`、`betUuid={market}_{selection}_{handicap}__{ts}-{suffix}`,普通订单不带 `fillOrKill=false`;撤单请求使用 `CURRENT_BETS` 中的完整 open-bet 对象而不是最小 `{offerId,betType}`。据此修正 SE executor/execution 映射、`x-device=DESKTOP` header、默认最小 stake=12 USD 口径,并保留 `se_request_capture.py` 作为手动抓包工具(脚本不自行下单/撤单)。边界:自动 `se_place_cancel_probe` 的最终真单复验在提交前遇到 SE Cloudflare `sport/details` 403,因此本条只说明真实前端 place/cancel schema 已校准,不声称自动 probe 全链路已 live 通过。测试:`pytest tests/arbitrage/adapters/sharpexch/test_provider.py tests/arbitrage/adapters/sharpexch/test_probe_script.py tests/arbitrage/adapters/sharpexch/test_executor.py tests/arbitrage/adapters/sharpexch/test_execution_translation.py tests/arbitrage/adapters/sharpexch/test_execution_client.py` → 80 passed。 |
| 2026-07-01 (#126be) | **Web matched pairs / odds 展示从 OE 写死泛化为 external venue**。继续 venue 插拔收口,不改交易链路:WebGatewayActor 接收 `MatchedPair` 时保留旧 `oe_instrument_ids` / `oe_teams` 兼容字段,新增 `external_venue` / `external_instrument_ids` / `external_teams`,从第二边 instrument id 后缀推断真实 external venue(PM+SE 时为 SHARPEXCH)。控制台页面 Discovery 统计/过滤增加 SHARPEXCH;Market Matching 列名改为 External venue;Odds Monitor 对所有非 PM venue(OE/SE)按十进制赔率 `1/odds` 转隐含概率并可同时列出多个 external venue。测试:`pytest tests/arbitrage/web/test_web_gateway.py` → 24 passed;compileall 通过。 |
| 2026-07-01 (#126bd) | **Venue 插拔第二阶段 TODO 明确暂缓**。用户指出 `pm_settlement` 归属应和 PM reconciliation 触发点共址,更合理地放回 Polymarket execution adapter/factory,而不是由 launcher 构造后塞进通用 `ArbContext`。本轮不改代码,把它和 `MarketMatchingActor` 仍默认 PM anchor 一样列为后续 venue 插拔第二阶段 TODO:① 泛化非 PM anchor matching;② 下沉 `pm_settlement` 到 PM adapter/factory。同步修正 SE 设计中过时的“PM anchor 必须开启”启动校验描述为“launcher 只校验 2+ runtime venues,matching PM anchor 语义后续泛化”。无行为变化,未新增测试。 |
| 2026-07-01 (#126bc) | **Venue enablement 上下文层收口**。承接 #126bb 的“任意 2+ runtime venues”注册层规则,继续把 dispatcher / launcher 上下文层对齐到同一开关:PM disabled 时 `pm_event_slug_tags` / `pm_competition_to_sport` 为空且 `_make_pm_settlement` 直接跳过;OE/SE disabled 时对应 `oe_scraper_config` / `se_discovery_config` 为 `None`,即使 `discovery.<venue>.enabled=true`。这样 `venues.<venue>.enabled` 成为 data/exec/factory/liveness/discovery-context/PM settlement 的统一 runtime 前置;`discovery.<venue>.enabled` 只在该 venue runtime 已开启后继续控制发现。边界:不改变当前 `MarketMatchingActor` 的 PM anchor 语义。详细设计见 configuration §6/§8。测试:`pytest tests/arbitrage/config/test_dispatcher.py tests/arbitrage/launchers/test_arb_node.py` → 90 passed;compileall 通过。 |
| 2026-07-01 (#126bb) | **Venue enablement 校验从“PM anchor + external”改为“任意 2+ venues”**。用户要求不要在启动注册层强制 PM,只要求打开两个以上 venue。调整 `validate_venue_enablement`:统计 `venues.polymarket/orbitexch/sharpexch.enabled`,少于 2 个才 `ConfigError`;`build_trading_node_config` / `register_factories` / `prepare_runtime_state` 也同步尊重 `venues.polymarket.enabled=false`,不注册 `POLYMARKET` / `PMSPORTS`。因此启动层可表达 PM+OE、PM+SE、OE+SE、PM+OE+SE。边界:当前 `MarketMatchingActor` 仍默认以 PM 为 `pm_venue`,OE+SE 只代表 runtime 注册层可表达,非 PM anchor 的匹配语义后续再泛化。测试:`pytest tests/arbitrage/launchers/test_arb_node.py` → 43 passed。 |
| 2026-07-01 (#126ba) | **Venue runtime enablement 第一片:PM anchor + 可插拔 external venues**。用户指出继续用 PM+OE 默认硬编码会导致 SE smoke 也打开 OE,因此先暂停“强接 SE”并落第二步最小插拔层。新增 `venues.polymarket.enabled` / `venues.orbitexch.enabled`(SE 已有 `enabled`),launcher 统一校验:PM 仍是 matching anchor,`venues.polymarket.enabled=false` 直接 `ConfigError`;至少需要一个 external venue enabled(OE 或 SE)。`build_trading_node_config` / `register_factories` / `prepare_runtime_state` 均按 `venues.*.enabled` 注册 data/exec config、factory 与 `VenueExecutionLiveness`;`to_market_matching_actor_config` 从 enabled external venues 推导 `external_venues`,支持默认 PM+OE、PM+SE(`orbitexch=false,sharpexch=true`)与 PM+OE+SE。示例配置显式写 PM/OE enabled 以保留现有实盘默认。测试:`pytest tests/arbitrage/launchers/test_arb_node.py tests/arbitrage/config/test_dispatcher.py tests/arbitrage/config/test_loader.py tests/arbitrage/matching/test_actor.py` → 112 passed。详细设计见 configuration §6/§8 与 sharpexch §3.5。 |
| 2026-07-01 (#126az) | **SharpExch skip node smoke 复验 execution hard gate 通过**。在 #126ay 修正 `se_login` 后,用 `/tmp/arb_config_se_smoke.json` 再次零下单启动 NT node(`strategy.enabled=false`,`debug.skip_execution=true`,`web.enabled=false`):SE ExecutionClient 路径收到 `SE balance frame routed` 与 `SE CURRENT_BETS routed: bets=0`,随后 `SharpExch login successful`,DataClient-SHARPEXCH connected,启动对账阶段 `Reconciliation for SHARPEXCH succeeded`,TradingNode 进入 RUNNING。此前 #126ax 的“8s 内仍无 CURRENT_BETS 重推 / execution 快照 hard gate 未过”是登录判定误把未授权 customer iframe 当成已登录后的观测,当前结论改为 execution empty snapshot 与 startup reconciliation 已过零下单 smoke。仍未下单/撤单;真单 place+cancel 需用户另行明确授权。 |
| 2026-07-01 (#126ay) | **SharpExch 登录判定修正:customer iframe 不等于已授权**。用户手动网页登录后确认 DevTools 能看到 `BALANCE` / `CURRENT_BETS`,与 #126ax 的自动 probe 只见 `OPEN_BETS_COUNTER:DENIED` 冲突。复查发现 `se_login(...)` 只要看到 `portal.sharpxch.com/customer` iframe 就直接返回,但 SE 未授权状态也可能预加载 customer iframe,导致自动链路未提交凭据。修正为登录表单优先:有 username 表单时必须填账号密码并提交,只有确认无登录表单时才把 customer iframe 当作可复用登录态。复跑 zero-order probe(不启动 TradingNode、不下单):`sport/details` 251 个 Tennis events,competition 页 30s 采样 `price_frames=80/parsed_price=80,general_frames=20/parsed_general=4,balance_frames=2,current_bets_frames=2`;样本中仍可能有 `OPEN_BETS_COUNTER:DENIED/CLOSED`,不作为 execution hard gate。测试:`pytest tests/arbitrage/adapters/sharpexch` → 179 passed。 |
| 2026-07-01 (#126ax) | **SharpExch execution general 业务帧补验收锚点 + reload-then-report 对齐 OE**。SE data 行情链路已在 #126aw 过 hard gate 后,继续补 execution 侧零下单可验证项:`SharpExchExecutionClient` 对首个有效 `BALANCE` 打印一次 `SE balance frame routed`,对首个 `CURRENT_BETS` 打印一次 `SE CURRENT_BETS routed: bets=N`,并维护 `_balance_frames_seen` / `_current_bets_frames_seen` 供离线测试断言。随后发现 skip smoke 中 SE 登录和 general WS 均成功,但 SE 不像 OE 一样主动推 BALANCE/CURRENT_BETS 业务帧;因此把 OE 的执行快照机制最小移植到 SE:handler 增 `on_frame` 任意帧回调刷新 `_last_frame_ns`,report 入口只信“有 CURRENT_BETS 快照且 general WS 新鲜”,否则 single-flight reload execution page 并等待 CURRENT_BETS 重推,超时 fail-closed 标记 SE order/position liveness dead。复跑 skip node smoke:SE 登录、general/prices WS、Data OBD 均正常,reload-then-report 被触发,但 8s 内仍无 CURRENT_BETS 重推(`SE exec reload: CURRENT_BETS not repushed before timeout`),说明下一步需要 DevTools/probe 找 SE 主动 balance/current bets API 或 WS subscribe 请求;execution 快照 hard gate 未过。这不代表真单 place/cancel 已验证。测试:`pytest tests/arbitrage/adapters/sharpexch` → 178 passed;compileall 通过。 |
| 2026-07-01 (#126aw) | **SharpExch skip node smoke 补齐行情硬门槛**。在 #126av 的 PM↔SE matching/subscription 基础上,为 `SharpExchDataClient._on_price_frame` 增加与 OE 对齐的低噪声 live 锚点:首个已路由行情帧打印 `SE price frame routed`,首次实际发布 NT `OrderBookDeltas` 打印 `SE OrderBookDeltas published`;两者各只打印一次,不是高频诊断日志。随后用 `/tmp/arb_config_se_smoke.json` 零下单烟测复跑(`strategy.enabled=false`,`debug.skip_execution=true`,`web.enabled=false`):node RUNNING 后出现 PM↔SE `MatchedPair ...|SHARPEXCH`、两条 `.SHARPEXCH` `SubscribeOrderBook`、SE competition orders/prices WS first frame、`SE price frame routed: market_id=1.259592581, runners=2, subscribed_selections=2` 与 `SE OrderBookDeltas published: instrument_id=1-259592581-19924831-None.SHARPEXCH, deltas=7`。仍未下单/撤单;真单 place+cancel 需用户另行明确授权。测试:`pytest tests/arbitrage/adapters/sharpexch/test_data_client.py tests/arbitrage/adapters/sharpexch/test_data.py` → 69 passed;compileall 通过。 |
| 2026-06-30 (#126) | **SharpExch 第一阶段离线切片落地**。在 #125 设计基础上新增 `nautilus_trader/adapters/sharpexch/` 初始子树(`config.py` / `discovery_client.py` / `providers.py` / `__init__.py`),先不接 launcher/factory/真实 browser。`ArbConfig` 增 `discovery.sharpexch` + `venues.sharpexch`,loader 从 `SHARPEXCH_USERNAME` / `SHARPEXCH_PASSWORD` 注入凭证并对 JSON 凭证发 `ConfigWarning`,dispatcher 增 `to_sharpexch_data_client_config` / `to_sharpexch_exec_client_config` / `to_se_discovery_config`。SE discovery 第一片只解析 portal `sport/details` payload:保留 `Match Odds`,按 competition 过滤,runner 映射 `home/draw/away`;Provider 输出 `BettingInstrument` + Q9 六统一 key,默认 `min_notional=Money(7*fx, USD)`。测试:68 targeted passed(`tests/arbitrage/adapters/sharpexch` + config loader/dispatcher)。**边界**:这不代表 SE 已可实盘启动或下单;DataClient/ExecutionClient/factory/liveness/risk required venues 接线仍按 `architectures/sharpexch/architecture.md` 后续分片。 |
| 2026-06-30 (#126b) | **SharpExch 三不原则 + Wimbledon 当前化 + discovery 请求描述补齐**。用户明确 SE 接入必须“不改架构、不改逻辑、不改流程”,已写入 `architectures/sharpexch/architecture.md §1`:SE 只能作为新增 venue adapter 同级接入,不重组 PM/OE/Strategy/Risk/Execution/Matching;不改变现有套利/风控/barrier/FX/profit/share-limit 逻辑;在 SE data/exec/factory/launcher 完整接线前不得把半成品塞进现有 runtime 启动链路。同时把当前示例 competition 从已过期的 `Men's Roland Garros 2026` 改为 `Men's Wimbledon 2026`(历史修订记录不改)。代码上补 `sport_details_request` 与注入式 `json_fetcher`:可为 Tennis/Wimbledon 构造 `POST /customer/api/sport/details?page=0&size=60` + body `id="2"`,但默认不主动联网;无 provider/fetcher 时 `SharpExchDiscoveryClient.discover_events()` 仍返回空列表。测试:101 targeted passed(SE + config + launcher),验证现有 launcher 初始化不被 SE 污染。 |
| 2026-06-30 (#126c) | **SharpExch price parser / OrderBookDeltas 纯映射切片**。继续遵循三不原则,只在 `nautilus_trader/adapters/sharpexch/` 内新增离线可测模块,不接 runtime。新增 `SharpExchMessageParser.parse_price_message` 解析 BIAB/OE 型 `multiple-market-prices` 帧,兼容 `bdatb/bdatl` dict 档与 `batb/batl` list 档;新增 `se_runner_to_book_deltas` 把 runner 快照转 NT `OrderBookDeltas`(CLEAR + BACK→BUY + LAY→SELL)。这只是 DataClient 前置纯函数,尚未实现 SE LiveDataClient/WS handler/订阅 routing。测试:108 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126d) | **SharpExch general WS parser 纯函数切片**。继续只改 SE adapter 子树,不接 ExecutionClient/runtime。`SharpExchMessageParser` 新增 `parse_general_frame` 与 `parse_order_message` 兼容别名,按顶层 key 解析 `BALANCE` / `CURRENT_BETS`,支持 dict/list payload 与嵌套 JSON 字符串;输出只做类型拆分与 float 解析,不做 FX/账户币种换算(后续 SE ExecutionClient 入站边界处理)。测试:114 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126e) | **SharpExch NT order → legacy order 纯映射切片**。继续不接 runtime。新增 `nautilus_trader/adapters/sharpexch/execution.py` 中的 `SharpExchLegacyOrder` 与 `nt_order_to_legacy_order`:从 SE `BettingInstrument` 取 `market_id/selection_id/selection_handicap`,NT `BUY`→`BACK`,NT `SELL`→`LAY`,无 handicap sentinel→0,缺 market/selection→None。为遵循“三不原则”,当前未改共享 `src.arbitrage.common.order_models.Venue` enum,而是在 SE 子树内保留本地 order dataclass;出站 FX 换算仍留给后续 SE executor/place_order 边界。测试:119 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126f) | **SharpExch CURRENT_BETS 入站 FX normalization 纯函数切片**。继续不接 runtime。`nautilus_trader/adapters/sharpexch/execution.py` 增 `normalize_current_bets_to_usd`:把 SE `CURRENT_BETS` 中 `size*` 字段与 `liability`、`profitNet/netProfit`、`profitGross`、`profit`、`pnl` 等金额字段乘以 `fx`,价格字段 `price` / `averagePrice` 不变;`fx<=0` fallback 1.0;非数字字段原样保留。测试:121 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126g) | **SharpExch CURRENT_BETS fill/order-progress 纯函数切片**。继续不接 runtime。`execution.py` 增 `current_bets_to_fills(bets, prev_matched)` 与 `bet_order_progress(bet)`:前者把非增量 `CURRENT_BETS` 快照按 `offerId` + 累积 `sizeMatched` 派生新增成交 delta,仅在 delta>0 且 `averagePrice>0` 时产出;后者从单 bet 派生 accepted/partially_filled/filled/unknown 与 market/selection/side/original_qty/filled_qty/avg_px/price。测试:136 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126h) | **SharpExch CURRENT_BETS position 聚合纯函数切片**。继续不接 runtime。`execution.py` 增 `current_bets_to_positions(bets)`:按 `(marketId, selectionId)` 聚合 matched 注单,BACK=LONG、LAY=SHORT,`net=ΣBACK_matched−ΣLAY_matched`;主方向均价只用净方向一侧的 matched 加权,反向成交只抵减净额;net zero / unmatched 跳过。测试:142 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126i) | **SharpExch BALANCE → AccountBalance 纯函数切片**。继续不接 runtime。`execution.py` 增 `se_balance_to_account_balances(balance)`:在 SE 入站金额已归一成 USD 口径后,生成 NT `AccountBalance(total=free=balance USD, locked=0 USD)`,对齐 OE `BALANCE` 语义;`__init__.py` 仅导出该 SE 子树纯函数,不改变 launcher/factory/真实 ExecutionClient 流程。测试:143 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126j) | **SharpExch placeBets payload 纯函数切片**。继续不接 runtime/Playwright/真实下单。`execution.py` 增 `se_order_to_place_bets_payload(order, fx)`:沿 OE/SE 同源 `/customer/api/placeBets` 结构生成 `{market_id:[bet_data]}`,在出站边界把 USD `order.size` 除以 fx 转 venue stake,生成 `betUuid`,夹紧赔率到 `[1.01,1000]`,映射 `FOK`。bad fx 直接拒绝。测试:147 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126k) | **SharpExch placeBets 响应解析纯函数切片**。继续不接 runtime。`execution.py` 增 `parse_place_bets_response(response, market_id, bet_uuid)`:解析 OE/SE 同源响应,成功时从 `offerIds[betUuid]` 取 `venue_order_id`,缺精确 key fallback 第一个 offer id,再缺 fallback `bet_uuid`;全局错误(`error`/非 200 `code`/空响应/非法响应)与市场级错误统一返回失败 message。测试:152 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126l) | **SharpExch cancelBets payload / 响应解析纯函数切片**。继续不接 runtime/Playwright。`execution.py` 增 `se_order_to_cancel_bets_payload(market_id, venue_order_id)` 与 `parse_cancel_bets_response(response)`:前者生成 OE/SE 同源 `/customer/api/cancelBets` body `{market_id:[{"offerId": venue_order_id,"betType":"EXCHANGE"}]}`,缺 market/offer id 返回 `None`;后者把无 error 的 dict 视为成功,空响应/非法响应/`error` 视为失败。真实 cookies/CSRF/page.evaluate 仍待 executor 切片。测试:155 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126m) | **SharpExchExecutor page-bound 薄封装切片**。继续不接 NT runtime/launcher/真实浏览器。新增 `nautilus_trader/adapters/sharpexch/executor.py`:`SharpExchExecutor` 不创建浏览器、不登录、不持账户状态,只在调用方传入 page 后用 `page.evaluate` 调 `/customer/api/placeBets` / `/customer/api/cancelBets`,并复用 SE payload/response 纯函数;fake page 离线测试覆盖 place/cancel payload 发送、成功解析、无 page、bad fx、缺 ids。真实 `SharpExchExecutionClient` 的 session 管理与 NT event 生成仍待后续切片。测试:159 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126n) | **SharpExchWebSocketHandler 基础 SockJS 分发切片**。继续不接 DataClient/ExecutionClient/runtime。新增 `nautilus_trader/adapters/sharpexch/websocket_handler.py`:`SharpExchWebSocketHandler` 监听 Playwright `page.on("websocket")`,按 URL 分型 `multiple-market-prices`→prices、`general`→orders,解包 SockJS `a[...]` 业务帧并分发给 price/order callbacks;`o`/`h` 心跳与 `[` 开头上行 subscribe 帧不进业务 callback;支持 first-frame 低噪声日志、active websocket 列表与 close callback。为守住小步原则,本切片未搬 OE 的 NT clock liveness/reload 机制,DataClient 接线仍待后续。测试:167 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126o) | **SharpExch price frame → OrderBookDeltas routing helper 切片**。继续不接 DataClient runtime。`data.py` 增 `se_price_message_to_book_deltas(message, routing, ts)`:把 handler 收到的 SE price WS message 经 `SharpExchMessageParser` 解析后,按后续 DataClient 维护的 `selection_id -> InstrumentId` routing 表生成 `OrderBookDeltas` 列表;未订阅 runner、空档 runner、非法消息均跳过。测试:170 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126p) | **SharpExch instrument routing 纯 helper 切片**。继续不接 DataClient runtime。`data.py` 增 `se_routing_entry_from_instrument(inst)` / `se_update_market_routing(routing, instrument_id, inst)` / `se_remove_market_routing(routing, instrument_id)`:从 SE `BettingInstrument` 读取 `market_id` 与 `selection_id`,统一转字符串后维护 `market_id -> selection_id -> InstrumentId` routing;缺字段返回 `None`/`False`,由后续 DataClient 记录 warning 并跳过。测试:174 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126q) | **SharpExch competition page ref/url 纯 helper 切片**。继续不接 DataClient runtime。`data.py` 增 `se_competition_page_ref_from_instrument(inst)` 与 `se_competition_page_url(base_url,sport_id,competition_id)`:从 `BettingInstrument.event_type_id`/`competition_id` 生成 page key `{sport_id}_{competition_id}` 与 competition 页 URL `{base_url}/customer/sport/{sport_id}/competition/{competition_id}`,缺 sport/competition 返回 `None`。测试:177 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126r) | **SharpExch WS 摘要 helper 切片**。继续不接 DataClient runtime。`data.py` 增 `se_websocket_summary(handler)`:读取 handler `get_active_websockets()` 并生成 `ws_count=N, ws_types={...}` 摘要,用于后续 competition 页 open/reload 日志锚点,对齐 OE 观测输出。测试:179 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126s) | **SharpExch competition page open/reload 可注入 helper 切片**。继续不接 DataClient runtime/真实 browser。`data.py` 增 `se_open_or_reload_competition_page(...)`:调用方显式传入 `browser_manager`、`comp_pages`、`comp_handlers`、callbacks;新开时执行 `create_page -> handler.start -> bring_to_front -> goto -> registry 写入`,reload 时复用已有 page 并 `bring_to_front -> reload`,新开失败时 stop handler + close page。fake browser/page 测试覆盖顺序、reload、失败清理。测试:182 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126t) | **SharpExch disconnect reload 判定 helper 切片**。继续不接 DataClient runtime/task。`data.py` 增 `se_should_reload_on_disconnect(...)`:只对 `liveness_timeout` / `close:prices` 返回 true,并处理 disconnecting、reload-in-flight、页不存在、冷却窗防护;允许 reload 时写入 `comp_last_reload_ns`,但不创建 task。测试:187 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126u) | **SharpExch disconnect reload 组合 helper 切片**。继续不接 DataClient runtime/task、不启动真实 browser。`data.py` 增 `se_reload_competition_on_disconnect(...)`:先复用 `se_should_reload_on_disconnect(...)` 判定,再用 `comp_reloading` 包住 `se_open_or_reload_competition_page(...)`;gate 拒绝/缺 page ref 时 no-op,异常向上抛但清掉 reload-in-flight 标记。测试覆盖 reload 已有页、gate skip、异常清理、缺 page ref 不写 cooldown。测试:191 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126v) | **SharpExch 订阅状态计划 helper 切片**。继续不接 `SharpExchDataClient` runtime。`data.py` 增 `se_subscription_plan_from_instrument(...)` / `se_update_subscription_state(...)`:从单个 SE instrument 同时派生 routing key 与 competition page ref,并一次性维护 `market_id -> selection_id -> InstrumentId`、`market_id -> page_key`、`page_key -> (sport_id,competition_id)` 三张状态表;坏 instrument 不写状态。测试:195 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126w) | **SharpExch ensure competition page helper 切片**。继续不接 `SharpExchDataClient` runtime/task。`data.py` 增 `se_ensure_competition_page(...)`:调用方传入订阅 plan、当前 `comp_pages` 与注入式 open-page coroutine;页已存在返回 `already_open`,缺页调用 open-page,坏 plan no-op,open-page 异常向上抛。测试:199 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126x) | **SharpExch 开页失败重试判定 helper 切片**。继续不接 `SharpExchDataClient` runtime/task。`data.py` 增 `se_should_reopen_missing_page(...)`:用于开页失败后的延迟重试醒来时判定是否继续,只有未关停、目标 page 仍未打开、且 `market_to_page_key` 仍有订阅 market 指向该 page_key 时返回 true。测试:201 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126y) | **SharpExch 开页失败显式重试组合 helper 切片**。继续不接 `SharpExchDataClient` runtime/task。`data.py` 增 `se_reopen_missing_page(...)`:复用 `se_should_reopen_missing_page(...)` 判定,再从 `comp_page_refs` 取 sport/competition 调注入式 open-page;gate 拒绝/缺 page ref 时 no-op,open-page 异常向上抛。测试:204 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126z) | **SharpExch market-level price routing helper 切片**。继续不接 `SharpExchDataClient` runtime。`data.py` 增 `se_market_price_message_to_book_deltas(...)`:输入后续 DataClient 实际维护的 `market_id -> selection_id -> InstrumentId` routing,按 price frame 的 market_id 找 selection routing 并输出 `{market_id,in_play,runners,subscribed_selections,deltas}`;未路由 market / 非法消息返回 `None`,已路由但空档时保留 frame 元信息并返回空 deltas。测试:207 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126aa) | **SharpExch routed book-delta publish helper 切片**。继续不接 `SharpExchDataClient` runtime。`data.py` 增 `se_publish_routed_book_deltas(...)`:输入 `se_market_price_message_to_book_deltas(...)` 的 routed payload,逐个 `OrderBookDeltas` 调注入 publish,并可选按 instrument 写入 `in_play`;返回实际发布数量,空 payload / 空 deltas no-op。测试:209 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ab) | **SharpExch price frame handle helper 切片**。继续不接 `SharpExchDataClient` runtime。`data.py` 增 `se_handle_price_frame(...)`:组合 market-level routing 与 publish 两步,返回 routed payload 并追加 `published_count`;未路由 frame 返回 `None`,已路由但空档时返回 frame 元信息且 `published_count=0`。测试:212 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ac) | **SharpExch 订阅状态移除 helper 切片**。继续不接 `SharpExchDataClient` runtime。`data.py` 增 `se_remove_subscription_state(...)`:解订时删除目标 instrument 的 selection routing;market 空掉后同步删除 `market_to_page_key`;若 page_key 已无任何 market 引用,再删除 `comp_page_refs`,避免开页失败重试在解订后误判仍被订阅。测试:215 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ad) | **SharpExchDataClient 离线骨架切片**。新增 `SharpExchDataClient` 类但仍不接 factory/launcher/runtime。骨架可构造,维护 market/page routing 与 competition page registry;`_subscribe_order_book_deltas` 复用 subscription helper 注册状态并调用注入式 open/reload,`_unsubscribe_order_book_deltas` 清状态,`_on_price_frame` 复用 `se_handle_price_frame(...)` 发布 deltas 并写 `instrument.info["in_play"]`。真实 `_connect` browser 生命周期、factory/launcher 接线仍待后续切片。测试:220 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ae) | **SharpExchDataClient disconnect-driven reload 离线接线**。继续不接 factory/launcher/runtime。`SharpExchDataClient._on_comp_disconnect` 复用 `se_should_reload_on_disconnect(...)`,只对 `close:prices` / `liveness_timeout` 建 reload task;`_reload_comp_on_disconnect` 调同一 open/reload 方法,失败时只 log 并清 reload-in-flight,避免后台 task 异常泄漏。测试:223 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126af) | **SharpExchDataClient 开页失败延迟重试离线接线**。继续不接 factory/launcher/runtime。`_subscribe_order_book_deltas` 开页失败不向上抛,记录 warning 并排 `_delayed_reopen`;`_delayed_reopen` sleep 后复用 `se_reopen_missing_page(...)`,若仍订阅且 page 仍缺失则重开,解订后 no-op,失败时再排下一轮。为离线可测新增 `_schedule_task` 调度入口,生产仍走 `loop.create_task`。测试:227 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ag) | **SharpExchDataClient lifecycle 离线骨架接线**。继续遵守“不改架构/不改逻辑/不改流程”,只按 OE DataClient 范式补 SE 类内部生命周期,不注册 factory/launcher、不跑真实 browser。`_connect` 启动注入的共享 browser manager、首轮 `provider.load_all_async()`、把 provider instruments 送入 DataEngine,并按配置启动 `_update_instruments`;`_disconnect` 取消周期发现 task、停止已开 competition handlers、清空 page registry,不关闭共享 browser;`_update_instruments` 单轮失败只 log,下一轮继续。测试:230 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ah) | **SharpExchExecutionClient 离线骨架切片**。继续不接 factory/launcher/runtime、不启动真实 browser。新增 `SharpExchExecutionClient(ArbExecutionSessionMixin, LiveExecutionClient)`,账号 `SHARPEXCH-001`,base currency `USD`;`_connect` 显式早失败,防半成品误接实盘。离线接入 general 帧:`BALANCE` 按 `balance*fx` 生成 USD `AccountState`,`CURRENT_BETS` 维护 USD 快照、标记 `VenueExecutionLiveness`,并对已 join 的 `offerId` 生成增量 `generate_order_filled`;接入 `_submit_order` session gate + executor result → accepted/rejected、异常立刻 reject+end session,`_cancel_order` 走 `SharpExchExecutor.cancel_order`,`_modify_order` 固定 reject。仍待:真实登录/page/WS `_connect`、order/position reports、factory/launcher。测试:241 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ai) | **SharpExchExecutionClient reconcile reports 离线切片**。继续不接 factory/launcher/runtime,不做真实 page reload。`SharpExchExecutionClient` 基于 `_current_bets` 快照新增 `generate_order_status_reports` / `generate_order_status_report` / `generate_position_status_reports`:order report 复用 `bet_order_progress`,优先 join NT order,否则按 `marketId+selectionId` 反查 SE instrument;position report 复用 `current_bets_to_positions`;无 CURRENT_BETS 快照时 fail-closed 标记 SE order/position liveness dead 并返回空/None。仍待:真实 `_connect` 后的 reload-then-report 与 live smoke。测试:245 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126aj) | **SharpExch factories 离线构造切片**。继续不注册 launcher、不启动真实 browser。新增 `browser_manager.py` 作为 SE adapter 内独立入口(当前复用 OE `PlaywrightBrowserManager`)与 `factories.py`:`SharpExchLiveDataClientFactory` / `ArbSharpExchLiveExecClientFactory`;`ArbContext` 增 `se_session_timeout_secs` / `se_discovery_config` / `se_*_aliases` / `se_instrument_provider` / `se_browser_manager`。Data factory 复用/回写 `ctx.se_browser_manager`,按 `ctx.se_discovery_config` 选择占位 `InstrumentProvider` 或 `SharpExchDiscoveryClient + SharpExchInstrumentProvider`,并注入 fx;Exec factory 要求 `venue_liveness`,复用同一 browser manager,注入 session timeout / pair registry / pair_inflight / fx。仍待:TradingNode config 与 `register_factories` 正式把 SE 接入 runtime。测试:250 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126ak) | **SharpExchExecutionClient page/WS 生命周期离线切片**。继续不注册 launcher、不跑真实 browser/live。`SharpExchExecutionClient._connect` 从早失败改为 adapter 内部最小生命周期:启动共享 browser manager、创建 `"execution"` page、先挂 `SharpExchWebSocketHandler` 再导航 `login_url`,未登录时沿用 OE 型 username/password/login button `_login`,并生成 0 USD 初始 `AccountState`;`_disconnect` 停 WS handler 并清 page,不关闭共享 browser。fake Playwright 测试锁住“先监听再导航”和初始账户状态。仍待:真实 SE 登录 selector/post-login 行为、general WS live smoke、reload-then-report、launcher 注册。测试:250 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126al) | **SharpExch launcher opt-in runtime 接线**。继续不跑真实 browser/live,但把 SE 接入 TradingNode 构建链路改为显式开关:新增 `venues.sharpexch.enabled=false` 默认值;默认 PM/OE runtime 不变,`build_trading_node_config` / `register_factories` / `prepare_runtime_state` 均不含 `SHARPEXCH`;显式 `true` 时加入 SE data/exec client config、注册 `SharpExchLiveDataClientFactory` / `ArbSharpExchLiveExecClientFactory`,并把 `SHARPEXCH` 纳入 `VenueExecutionLiveness` 初始 venue 集。`to_arb_context_init_kwargs` 现在注入 `se_session_timeout_secs`,且仅在 SE venue enabled 时注入 `se_discovery_config`;aliases 同形注入。边界:这只是 runtime wiring 的 opt-in 离线接线,真实登录/WS live smoke 与 matching 多外部 venue 泛化仍待后续。测试:256 targeted passed(SE + config + launcher)。 |
| 2026-06-30 (#126am) | **MatchingActor 支持 PM + 多 external venues**。为 SE runtime opt-in 后的端到端链路清硬编码:保留 PM 为锚点 venue,`MarketMatchingConfig.external_venues` 默认 `("ORBITEXCH",)`,当 `venues.sharpexch.enabled=true` 时 dispatcher 输出 `("ORBITEXCH","SHARPEXCH")`;`MarketMatchingActor._maybe_match` 对每个 external venue 独立读取 cache 并匹配,某个 external 缺失不阻塞其它 external。`MatchedPair.oe_instrument_ids` 暂作兼容字段承载当前 external legs;PM↔OE pair_id 保持历史格式,PM↔SE 追加 `|SHARPEXCH`,避免同场 PM↔OE 与 PM↔SE 覆盖同一个 PairRegistry / `_emitted_pairs`;sports ended eviction 从 `game_id→pair_id` 改为 `game_id→set[pair_id]`。边界:Strategy 的 SE 概率/size 支持仍待下一片。测试:289 passed,7 skipped(`matching + SE + config + launcher`)。 |
| 2026-06-30 (#126an) | **Strategy 第一阶段接入 SharpExch odds/size 语义**。按用户确认,这不是第二阶段可插拔抽象,只把现有 OE 分支扩成 OE/SE: `_venue_of` 识别 `.SHARPEXCH`,`mean_rebate` / `one_side_rebate` / `mean_rebate_recovery` 对 SE 使用 OE 类 decimal odds 概率(`1/odds`)与 USD stake qty 语义,`place_bets` 对 SE 使用 `size=share/odds`,strategy 层 `share_limit` 对 SE 使用对应 venue 的 outcome shares 并按 OE 类 merge remaining 缩放。PM 语义不变,adapter 边界 fx 仍在 SE/OE 入站/出站处理。测试:`pytest tests/arbitrage/strategy` → 155 passed。 |
| 2026-06-30 (#126ao) | **Risk 第一阶段接入 SharpExch venue identity 与 liveness**。继续不做第二阶段可插拔抽象,只补 SE 在现有 Risk 契约里的必要识别:`_venue_from_leg_key` 支持 `se:*` / `sharpexch:*` → `SHARPEXCH`,使 opportunity liveness gate 对 PM↔SE 两腿 fail-closed;概率门控对 SE `BettingInstrument` 继续按 decimal odds `1/price`;余额门控把 SE 作为非 PM venue,直接信 adapter 入站后的 USD free;`ArbitragePortfolio._leg_from_position` 对 `BettingInstrument` 保留 `instrument.id.venue.value.lower()`,避免把 SE 持仓归到 OE,从而 `outcome_shares_for_venue(pair_id,\"sharpexch\")` 可供 Strategy share_limit 正确读取。测试:`pytest tests/arbitrage/risk` → 44 passed;SE+strategy+config+launcher+matching+risk 集合 → 488 passed,7 skipped。 |
| 2026-06-30 (#126ap) | **SharpExch 独立 zero-order probe 落地,暂停继续强接套利链路**。用户指出继续把 SE 白名单式塞进 Strategy/Risk/Barrier/Web 会提前制造第二阶段重构债;但 SE 网站事实(login/iframe/API/WS/schema)仍需先通过真实浏览器分析。定论:先新增 `scripts/se_probe.py`,作为独立 SE 网站事实探针:真登录、`sport/details` fetch、competition 页打开、prices/general WS 监听、可选脱敏样本输出;**不启动 TradingNode、不注册 Matching/Strategy/Risk、不调用 placeBets/cancelBets**。默认只打印摘要,`--write-dir` 才写 `se_probe_redacted.json`。离线测试覆盖脱敏、样本摘要、competition URL 生成。真实运行仍需用户明确要求启动浏览器。测试:`pytest tests/arbitrage/adapters/sharpexch/test_probe_script.py` → 3 passed;`python -m compileall -q scripts/se_probe.py ...` 通过。 |
| 2026-07-01 (#126aq) | **SharpExch zero-order probe 实跑 + iframe context 修正沉淀进 adapter**。用户授权继续后实跑 `scripts/se_probe.py`:确认 SE 登录成功后主 URL 仍停在 `sharpxch.com/player/`,真实 app 在 `portal.sharpxch.com/customer` iframe;`sport/details` 必须在 customer iframe context 内 fetch,否则主页面 origin 会 `Failed to fetch`;competition 页可打开,orders/prices WS active,price frame 可按当前 BIAB/OE 型 parser 解析,`sport/details` 返回 60 个 Tennis events(含 `Men's Wimbledon 2026`)。落地 `nautilus_trader/adapters/sharpexch/web.py` 统一 customer URL/frame/login/fetch helper;`scripts/se_probe.py` 复用 helper;`SharpExchLiveDataClientFactory` 给 discovery 注入 browser `json_fetcher`;`SharpExchExecutionClient._login` 改为等待 customer iframe,不再等 `networkidle`;`SharpExchExecutor` 的 place/cancel 在 `se_customer_context(page)` 内执行,避免出站 API 跨 origin。边界:仍未启动 SE node、未进入 Matching/Strategy/Risk、未下单/撤单;skip node smoke 与真单 place+cancel 另行授权。测试:`pytest tests/arbitrage/adapters/sharpexch` → 164 passed;`python -m compileall -q nautilus_trader/adapters/sharpexch scripts/se_probe.py tests/arbitrage/adapters/sharpexch` 通过;zero-order probe 实跑通过。 |
| 2026-07-01 (#126ar) | **SharpExch discovery 从第一页验证补成分页 + 登录后弹窗处理**。用户指出 `Men's Wimbledon 2026` 不止 60 场,此前 probe 只证明了 `sport/details?page=0&size=60` 可用,不能代表完整 discovery。落地:`sport_details_request` 支持 `page/size`;`SharpExchDiscoveryClient` 的 `json_fetcher` 路径分页请求,直到短页/空页/下一页无新 `marketId`/100 页上限;`scripts/se_probe.py` 同步分页并打印 competition 计数。用户提醒 SE 登录后有弹窗,因此 `se_login` 增 `se_dismiss_post_login_popup`:等待 `div[class*=\"_postLoginPopup_\"]` 可见后点击主页面区域,无弹窗静默继续。zero-order probe 实跑:5 个 `sport/details` 请求,242 个 Tennis events,`Men's Wimbledon 2026` 64 个、`Women's Wimbledon 2026` 64 个;未启动 node、未下单/撤单。测试:`pytest tests/arbitrage/adapters/sharpexch/test_probe_script.py tests/arbitrage/adapters/sharpexch/test_discovery_client.py tests/arbitrage/adapters/sharpexch/test_factories.py` → 22 passed。 |
| 2026-07-01 (#126as) | **SharpExch competition 页 lifecycle 校准为 `domcontentloaded`**。继续推进 SE node smoke 前置修正:SE customer/competition 页会长期持有 WS,沿用 OE 型 `networkidle` 容易在开页/刷新时卡住。落地:`se_open_or_reload_competition_page` 新开与 reload 均改为 `wait_until=\"domcontentloaded\"`;handler 仍保持先 start 再 goto,确保捕获导航后新建 WS。同步 SE adapter 测试期望。测试:`pytest tests/arbitrage/adapters/sharpexch/test_data.py tests/arbitrage/adapters/sharpexch/test_data_client.py` → 66 passed。 |
| 2026-07-01 (#126at) | **SharpExch competition 页 open/reload 增加 prices 首帧短等与 frame count 摘要**。`domcontentloaded` 只保证 DOM 可用,不能证明 SockJS prices feed 已连上;zero-order probe 曾出现 competition 页最终 active WS 为空但早前 price frame 已解析的观测,需要更清晰的生命周期锚点。落地:`SharpExchWebSocketHandler.get_frame_counts()` 只读暴露各类 WS 入向帧计数;`se_websocket_summary` 增加 `frame_counts`;`se_open_or_reload_competition_page` 在新开后短等 `prices>=1`,reload 时以刷新前 count 为 baseline 短等下一帧。等不到只 warning,不改变订阅/重试流程。测试:`pytest tests/arbitrage/adapters/sharpexch/test_data.py tests/arbitrage/adapters/sharpexch/test_websocket_handler.py` → 61 passed;`python -m compileall -q nautilus_trader/adapters/sharpexch tests/arbitrage/adapters/sharpexch scripts/se_probe.py` 通过。 |
| 2026-07-01 (#126au) | **SharpExch zero-order probe 复验 competition 页 WS active**。在 #126at 后实跑 `python3 -m scripts.se_probe --config arb_config.json --wait 12`:login ok;`sport/details` 5 次分页返回 240 个 Tennis events(当前赛事集,会随 SE 变化),`Men's Wimbledon 2026` 与 `Women's Wimbledon 2026` 各 64;打开 `https://portal.sharpxch.com/customer/sport/2/competition/12597512` 后 orders/prices 两条 WS 均 active,price frames=3 且 parsed_price=3。仍未启动 TradingNode、未进入 Matching/Strategy/Risk、未下单/撤单。 |
| 2026-07-01 (#126av) | **SharpExch skip node smoke 首次跑通到 PM↔SE matching/subscription,并补启动期竞态**。第一次 SE node smoke 暴露两类启动期问题:① Data discovery browser fetch 与 Exec login 并发使用同一 customer context 时,SE/Cloudflare 会偶发 `sport/details` 403 或登录 form 等待误判;② 同一 competition 的两条 SE leg 并发订阅会重复打开 competition page。落地:新增 `ctx.se_browser_lock`,Data factory 的 `sport/details` fetch 与 Exec `_connect` login 共用该锁串行化;`se_login` 在看不到即时 customer iframe 时先短等 delayed iframe,再退回 login form selector;Provider 保存并传递 `discovery.sharpexch.sports`,不再靠默认 sport;`SharpExchDataClient` 增 `_comp_pages_lock`,subscribe/delayed reopen/disconnect reload 进入 open/reload 前持锁,同场并发订阅只开一页。验证:zero-order probe 仍可分页取回 Wimbledon 64 events;skip_execution node smoke 看到 SE exec 登录/WS、SE data connected、SHARPEXCH startup reconciliation succeeded、node RUNNING、PM↔SE `MatchedPair ...|SHARPEXCH`、两条 `.SHARPEXCH` SubscribeOrderBook、DataClient-SHARPEXCH 只开一次 `2_12597512` competition page 且后续 orders/prices WS first frame。仍未观察到 SE routed price/OBD publish,留下一轮 smoke hard gate;未下单/撤单。测试:`pytest tests/arbitrage/adapters/sharpexch/test_data.py tests/arbitrage/adapters/sharpexch/test_data_client.py tests/arbitrage/adapters/sharpexch/test_factories.py tests/arbitrage/adapters/sharpexch/test_execution_client.py tests/arbitrage/adapters/sharpexch/test_probe_script.py tests/arbitrage/adapters/sharpexch/test_provider.py tests/arbitrage/adapters/sharpexch/test_discovery_client.py` targeted 通过;详设见 `architectures/sharpexch/architecture.md`。 |
| 2026-06-30 (#125) | **SharpExch 第一阶段接入设计独立成文**。用户要求先像 OE 一样把 SE 接进来,但设计另起文件,`refactor.md` 只留引用。DevTools 实测 `www.sharpexch.com` 跳到 `sharpxch.com/player/`,内嵌 `portal.sharpxch.com/customer` iframe;portal URL/API/SockJS 命名与 OE 高度同源,包括 `/customer/sport/{id}`、`/customer/api/competition/sport/{id}?showGroups=true`、`POST /customer/api/sport/details?page=0&size=60`、`/customer/ws/general`、`/customer/ws/multiple-market-prices`。决策:第一阶段新增 `nautilus_trader/adapters/sharpexch/` 按 OE 型 venue 显式接入(`SHARPEXCH`, `BettingInstrument`, `{market_id}-{selection_id}.SHARPEXCH`,config/dispatcher/launcher/liveness/matching/risk 接线);discovery 优先使用 SE portal API 而非 DOM 行扫描;第二阶段等 OE+SE 都跑通后再抽 browser-exchange 插拔框架。详细设计见 `docs/arbitrage/architectures/sharpexch/architecture.md`;测试计划见 `tests/arbitrage/adapters/sharpexch/README.md`。 |
| 2026-06-29 (#124) | **`share/max_leg_share/fx` 从 Risk 配置所有权迁到顶层 `arbitrage` 运行默认值**。用户指出这些不是 risk 本身的配置,只是策略规划 / exposure / FX 换算共享的普通运行参数。落地:`ArbitrageSectionConfig` + `ArbitrageParams`;`to_arbitrage_params(cfg)` 与 `wire_arbitrage_runtime(..., arbitrage_params=...)` 并行于 `to_arb_risk_params`;`ArbRiskParams` 只保留真正风控字段(`match_tp/match_sl/global_sl/min_probability/max_probability`)。Web Arbitrage 标签页现在写 `/config/arbitrage` 并 publish `command.arb.arbitrage_params`;RiskEngine 订阅后用于 profit gate 金额基数和 OE fx,StrategyEvaluator 订阅后作为 `EvalContext.strategy_defaults`。旧配置里的 `risk.share/max_leg_share/fx` 由 loader 兼容迁移到 `cfg.arbitrage.*`,新示例配置与 Web 写回均使用顶层 `arbitrage`。测试:config/dispatcher/loader + risk/bootstrap/engine + strategy/evaluator/actions + launcher + web targeted 通过。详细设计=configuration / common / web / risk / strategy。 |
| 2026-06-23 (#123) | **web 忠实照搬 legacy 完整页面 + 监控随页面加回 + OE 赔率换算概率**。用户要求"参照 legacy 页面、改/删/加",不再自己重组:`static/console.html` 复用 legacy Bootstrap 5 标签页结构(navbar 渐变 + nav-pills:Market Discovery / Market Matching / Odds Monitor / Strategy / Configuration + Config 子标签 Discovery/Matching/Arbitrage/Execution/Risk/OrbitExch + 两栏卡片布局),`GET /` serve。**加**:navbar 启停(TradingState)+ 余额;只读端点 `/accounts`/`/instruments`/`/matched_pairs`/`/odds`(#120 移除的监控随页面加回,actor 重订 `data.MatchedPair*`、读 `cache.instruments()`/`cache.order_book`);Odds 表 **OE 十进制赔率前端 `1/odds` 换算成隐含概率**(bid/ask 互换使 bid≤ask,原赔率括号留存)与 PM 概率统一。**删**(NT 无对应/死):Run Discovery/Matching、Subscribe Odds、pipeline start/stop、Execution market_order/discount/take_off、Risk global_sl/健康检查间隔/返水率面板。配套:对账间隔 `open/position_check_interval_secs` 提到 execution config 可配(原硬编码 launcher);`SkipExecutionPolymarketClient._connect` 删 stale `self._health.start()`(#109/#110 退役残留,瞬时失败时 AttributeError 卡死启动);OE 死配置字段从页面剔除(仅留 default_persistence/page_load_timeout/staleness/headless)。`/health` 澄清=web server 存活探针,非退役的交易健康检查。**live 验证**:页面 5 标签 + Config 子标签全渲染、真实 ATP Eastbourne instruments/matched pair/OE↔PM 概率、Start/Halt 实测。测试:`test_web_gateway.py` 加 `/`/`/accounts`/`/instruments`/`/matched_pairs`/`/odds` 用例,617 collected 全绿。详设=web §1-8。 |
| 2026-06-23 (#122) | **PM report 失败对齐 OE:`mark_dead + 返空`,不再 raise**。`ArbPolymarketExecutionClient` 的 `generate_order_status_report(s)` / `generate_position_status_reports` 失败时原先 `mark_*_dead(POLYMARKET)` **后 raise**;OE base(`orbitexch/execution.py:691/733`)失败是 `mark_*_dead + return []`(不抛)。PM 一抛 → NT `generate_mass_status` 失败 → **startup reconciliation「Execution state could not be reconciled」→ kernel 跳过 `trader.start()` → 所有 actor(MarketMatching/Strategy/WebGateway)卡 READY、web 不绑**(根:PM data-api 启动期偶发瞬时超时 / geoblock 403,NT 对账无重试,一次抖动即致命)。修:PM 三个 report 方法改 `mark_dead + return []`(单条 `return None`),与 OE 同模式 → **真实/skip 两模式都不再卡启动**,venue 仍正确 fail-closed(`VenueExecutionLiveness` 默认 false,靠后续成功对账自愈;**不**在容忍时 mark_alive 以免 fail-open)。撤掉先前 skip-only `_tolerant_reports`(已多余);`_RetryFailureRecorder` 仍识别 RetryManager 吞掉的失败(→ mark_dead),只是不再 raise。⚠️ 改了 PM 真实下单路径对账契约(#111 原 raise),但 mark_dead 已达 fail-closed,raise 额外只"卡对账"、启动期有害无益。**live 验证**:PM 通时正常对账 + actors RUNNING + web 起;PM 抖动时容忍不卡。测试:`test_polymarket_client.py` 四个 report-failure 用例改断言 `==[]`/`is None`,`test_debug_execution_clients.py` 清 stale `_health`;`{execution,debug,common,settlement,web,adapters/orbitexch}` 263 passed。详设=execution §4.6。 |
| 2026-06-22 (#121) | **退役 `way_rebate` 系列当前接口,Portfolio 只保留 outcome 指标**。用户重申 Risk 不需要类似 recovery 的归一化比率,只需要每个 outcome 的 `net_profit/liability`,以及 share limit 所需的 outcome share。落地:删除 `ArbitragePortfolio.way_rebate/min_way_rebate/way_rebates_by_venue/global_min_rebate_sum` 与内部 `_compute_way_rebate/_max_outcome_share`;`OpportunitySnapshot` 不再包含 `way_rebate`,Strategy 不再从 Portfolio 拉该指标;`mean_rebate_recovery` 内部 helper 从 `_way_rebate` 改名为 `_outcome_return_rates`,仍只在 recovery 树内按 outcome return rate 判断补救机会。Risk 当前只读 `outcome_exposures(pair_id)` 做单场 profit gates,读 `outcome_shares(pair_id)` 做 share limit adjusted-size gate。文档同步 risk/strategy/architecture/README;旧 `src/arbitrage/services/**` 中的 `way_rebate` 属 legacy 微服务栈,本轮不迁移不改。 |
| 2026-06-21 (#120) | **移除 web 只读监控 endpoint,web 收敛为纯控制台**。用户审视 #118 只读监控 MVP(余额/matched_pairs/way_rebate)后裁定:监控回归看日志,web 只留 #119 控制台(TradingState 启停 + 配置编辑)。移除 `/accounts`、`/matched_pairs`、`/positions/{pair_id}`、`/positions/global_min_rebate_sum` 路由 + `events.account.*` / `data.MatchedPair*` 订阅 + `_matched_pairs` 缓存 + account/matched_pair WS 推送 + `portfolio` 依赖 + 序列化 helper;`WebGatewayActor` 只剩 `events.risk`→WS 推 TradingState + 控制方法。`WebGatewayDeps` 去 `portfolio`(剩 loop/risk_engine/config_path)。**保留**:`/health`、`/ws`(推 trading_state)、控制台全部、scaffolding(uvicorn 同 loop / 端口预检 / get_running_loop / 优雅停机)。测试相应删 web-7.1/7.3/7.6/7.7,web 套件全绿。详细设计 web §1-7 重写为控制台 scaffolding(§8 控制语义不变);监控代码可从 git `ae1d397b18` 找回。**保留 SIGABRT 修(c6b1e36b,与 web 无关)+ settlement offload + codex #114-117(ae1d 内非 web 部分)。** |
| 2026-06-21 (#119) | **Step 7 控制台设计定档(配置编辑 + TradingState 启停;实现进行中)**。用户要求把 legacy 配置/监控页"迁"过来;核对 legacy `services/web_gateway/`(1968 行 index.html + ~30 `/api/*`)后定论:**pipeline start/stop、discovery/matching `run`、odds subscribe 在 NT 里无意义**(发现/匹配/订阅均连续自动)→ **全不做**;控制台只做两件 NT 成立的事。决策:**① 启停按钮 = NT 原生 `RiskEngine.set_trading_state(ACTIVE/HALTED)`**(不用 REDUCING),**boot 默认 HALTED**(仅 `web.enabled && web.start_halted` 时,launcher build 后 set;web 关则保持 NT 原生 ACTIVE 否则永不交易),**不联动 strategy 评估**(用户定;HALTED churn 接受);**② 配置编辑 = C 混合**(每次写回 `arb_config.json`;热字段 share/tp/sl/max_leg_share/refresh_interval 额外推命令即时生效,其余标"需重启");**③ seam = 方案乙 MessageBus 命令**(`command.arb.{trading_state,risk_params,refresh_interval}`,web 单一生产者 publish、risk/matching 订阅 apply,契约住 web 组件)。**对 Q16「不主动改 TradingState」的修订**:Q16 锁的是**自动门控**(profit gates/venue liveness)不碰 TradingState、走逐 submit deny;本控制台是**人工操作员熔断**显式 set TradingState,二者正交并存(risk §4.3/§4.4 已挂前向指针)。详细设计=web §8;测试 README=`web-7.8~7.11`。**实现进行中**,真金 live 验证(boot HALTED→点 Start 放行)待接线完成后单独跑。 |
| 2026-06-21 (#118) | **Step 7 WebGatewayActor 只读监控 MVP 落地(占位→实现)**。原 `web/architecture.md` 是占位(用户 2026-05-21 判暂不迁移),本轮用户拍 scope=**只读监控 + 后端 JSON/WS**(不碰交易路径;config-write / OrderBookDelta firehose / request-response 桥延后)。落地:`src/arbitrage/web/{actor,app}.py` —— `WebGatewayActor(Actor)` 与 TradingNode 同进程同 loop,`on_start` 经 `_NoSignalServer`(不抢 NT 信号)拉起 uvicorn 协程;订阅 `events.account.*`(NT Portfolio 发)+ `data.MatchedPair*`(MatchingActor 发)转 JSON 经 `/ws` 推前端(每 client `asyncio.Queue(maxsize=256)`,满丢最旧、绝不反压交易回调);HTTP GET `/accounts`、`/matched_pairs`、`/positions/{pair_id}`(way_rebate+by_venue+outcome_exposures)、`/positions/global_min_rebate_sum`(路由先于 `{pair_id}` 注册防吞)。**纯只读**:只调 cache 读 + portfolio pull,不发命令/不 publish/不写 cache。配置 `web.{enabled(默认 false),host(默认 127.0.0.1),port(默认 8080)}` 入 `ArbConfig` + `to_web_gateway_config`;launcher `add_actors` 仅 `cfg.web.enabled` 时构造(注入 `node.kernel.portfolio`+loop),关闭则零开销/不占端口。**替代旧 BalanceMonitorActor**:推余额给前端,余额低/熔断由用户看着判断。不搬 legacy `services/web_gateway/`(3353 行绑老 pipeline)。测试:`tests/arbitrage/web/test_web_gateway.py` 12 passed(Actor 广播/背压/序列化 + FastAPI TestClient HTTP/WS)。详细设计=web §1-7;测试 README=`web-7.1/7.3/7.6/7.7`。**未 live 验证**:真节点起 + 浏览器/curl 看 endpoint 待用户某次 live 跑顺带验。 |
| 2026-06-21 (#117) | **Risk 新增 share limit adjusted-size gate,barrier 不动**。用户澄清:PM/OE 两条 risk 线路必须像 profit gate 一样走同一段多边逻辑;`size adjust` 与 NT 原生 size gate 合并为一个门控顺序:先按 `risk.max_leg_share` 计算所有 expected outcome 的剩余额度并同比例缩放订单,再把 adjusted `SubmitOrder` 交给 NT 父类管道,由 PM `min_quantity=5` / OE `min_notional=7` 检查**缩放后的 size**。实现:`ArbitrageLiveRiskEngine._handle_submit_order` 前置 `_adjust_submit_order_for_share_limit`;PM requested share=`quantity`,OE requested share=`quantity*price*fx`;缩放时构造新的 `LimitOrder`(NT order quantity readonly)并保留原 `client_order_id` 与原 tags。Execution opportunity barrier 不缩放、不维护 share limit,只接收 risk pass 后的 adjusted command。配置新增 `risk.max_leg_share`(默认 `None` 关闭),`ArbitragePortfolio.outcome_shares(pair_id)` 提供当前 outcome share。测试:PM 缩放后进 execution、缩放后低于 PM min size 被 NT 原生拒绝、PM/OE 同 scale 同代码路径。详细设计=risk §3.1;测试 README=`risk-6.7.12/13/14`。 |
| 2026-06-21 (#116) | **Q16 Risk 门控从 way_rebate 比率改为 outcome 绝对利润,并撤掉全局止盈/止损**。用户澄清:Risk 不应像 recovery 树那样关心“按 outcome 聚合后的最大 share”归一化,而应直接按配置 `share`(当前示例 22.5)作为金额基数。定论:Portfolio 新增 `outcome_exposures(pair_id)` 返回每个 outcome 的 `net_profit/liability`;Risk `_check_profit_gates` 读取 live exposure,`match_tp`=所有 outcome `net_profit > share*match_tp` 时 deny,`match_sl`=所有 outcome `net_profit < share*match_sl` 时 deny;`global_sl/global_min_rebate_sum` 不再参与 Risk 门控,`global_sl` 仅为旧配置兼容字段。`way_rebate` 方法暂保留给 Strategy/Web 兼容展示,但不再驱动 Risk。补充边界:outcome 集合优先经 `PairRegistry.instrument_ids_for_pair(pair_id)` 从该 pair 的全部 instrument info 读取,避免三元盘某 outcome 暂无持仓时被“所有 outcome”判断漏掉。落地: `risk/engine.py`、`risk/portfolio.py`、`common/pair_registry.py`、`debug/risk.py`、risk/matching/debug 单测与配置示例;详细设计=risk §4.1 / matching §3.1 / common §3;测试 README=`risk-6.7.2/3/4`、`risk-6.9.2b/c`。 |
| 2026-06-21 (#115) | **`way_rebate` 分母从“最大单腿 share”修正为“按 outcome 聚合后的最大 share”**。问题:Portfolio/Risk 的 numerator 已按 outcome 聚合所有同 outcome 腿(`home` 同时有 PM/OE 仓都会加进 home 赢分支),但 denominator 仍取 `max(each leg.share_if_wins)`;若同一 outcome 跨 venue 同时持仓,分母偏小、rebate 被放大。定论:与 recovery 树一致,分母应为 `max(Σ share_if_wins(leg) for same outcome)`。落地:`ArbitragePortfolio._compute_way_rebate` 改用 `_max_outcome_share`;新增回归测试 `test_compute_way_rebate_aggregates_same_outcome_share_for_denominator`。详细设计=risk §4.1;测试 README=`risk-6.9.2`。 |
| 2026-06-21 (#114) | **PM settlement launcher 接线补齐(#110 缺口收口,真实链上待验)**。承接上一条 live 验证暴露的 `self._settlement is None`:在 `launchers/arb_node.py` 新增 `_make_pm_settlement(cfg)`,按 env 注入后的 `venues.polymarket.{private_key,funder,builder_api_*,relayer_url,polygon_rpc_url}` 构造 `OddsSubscriptionConfig` → `PolymarketContractService.initialize()` → `PolymarketSettlement`,并经 `prepare_arb_context(pm_settlement=...)` 注入 PM ExecClient factory。安全边界:① `execution.cleanup_enabled=false`、缺 `POLYMARKET_PRIVATE_KEY`/`POLYMARKET_FUNDER`、或 relayer initialize 失败 → `pm_settlement=None`,只 warning、不阻塞节点启动;② `cleanup_merge_enabled` / `cleanup_claim_enabled` 透传;③ settlement 仍由 NT 连续 position reconcile fire-and-forget 触发,single-flight 不变。测试:`pytest tests/arbitrage/launchers/test_arb_node.py tests/arbitrage/execution/test_polymarket_client.py tests/arbitrage/settlement/test_settlement.py` → 59 passed。文档:execution §4.6 / configuration §10 / PM adapter README / launcher README 已同步。**仍未声明真实 merge/redeem 可用**:Builder Relayer 链上 tx 需具备可 merge/redeem 持仓并经用户明确授权 live 验证。 |
| 2026-06-21 (#110 live 验证) | **#110 触发路径 live 验证通过;但结算对象尚未接线(slice 8/9 缺口暴露)**。skip_execution 节点实跑(`position_check_interval_secs` 临时 60s,验完已还原 300):NT 连续 position 对账**周期触发 → 调到 PM override → `mark_position_alive` → settlement dispatch 判定**,均 ✅。为可观测,override 加一条低噪声 INFO 锚点 `PM position reconcile OK: N report(s), settlement dispatched/skipped (M raw positions)`(生产约 5 分钟一条,兼作子系统心跳)。**关键发现**:锚点恒为 `settlement skipped`,因 `self._settlement is None` —— `launchers/arb_node.py` 仍 `pm_settlement=None`(slice 8/9 TODO),`PolymarketContractService`(`contract.py` 已存在)+ `PolymarketSettlement` **从未构造接线** → merge/redeem 即使账户有真持仓也是 no-op(印证 [gap_c] "merge/redeem 一直 no-op")。**结论:#110 的"触发改 NT 对账"已完成并验证;真链上结算接线(`contract_service`→`settlement` 构造 + 经 `prepare_arb_context(pm_settlement=)` 注入)留 slice 8/9 单独一轮做——属真钱上链 tx,需专门设计 + 预飞行。** 测试:`test_polymarket_client.py` 成功路径补 `_log` 守卫(override 对 `__new__` 未初始化 logger 健壮);targeted `55 passed`。详设单一真理源=execution §4.6(已补 live 验证状态)。 |
| 2026-06-19 (#113) | **PM cancel duplicate terminal 竞态修复**。live cancel-only 复测中出现 `InvalidStateTrigger: CANCELED -> CANCELED, did not apply OrderCanceled(...)`。核对代码确认:NT reconciliation 对 `OrderStatusReport(CANCELED)` 已有 `order.status != CANCELED and order.is_open` 保护,PM adapter 也已有“cache 已 CANCELED 则跳过”的 REST/USER WS 保护;漏网原因是 REST cancel success 与 USER WS cancellation 可近同时到达,两条路径都在 NT cache apply 第一条 `OrderCanceled` 前看到订单仍非 CANCELED,于是各自发布一次 terminal。修复落在 PM adapter 事件源头:新增有界 `_cancel_terminal_client_ids` 去重窗口,REST/deferred/batch/cancel-all 与 USER WS cancellation 共用 `_generate_cancel_success_event`,在 cache 状态尚未更新时也按 `client_order_id` 幂等跳过第二条。测试:`test_polymarket_cancel_success_skips_duplicate_before_cache_updates` 覆盖 cache 仍 ACCEPTED 的重复 terminal;targeted tests `37 passed`。设计=execution §3.1;测试 README=PM adapter `pm-adapter-5.2`。 |
| 2026-06-19 (#112) | **opportunity-level cancel-only 设计修正(已落地代码 + 离线单测,待 live 验证)**。live mock 真单验证暴露旧 per-client cancel-only 会在同一 opportunity 中出现“有 residual 的 venue 撤旧,无 residual 的 venue 同轮又开新单”的半边执行。用户修正触发条件:不是“任一 leg 有 residual 就整次 cancel-only”,而是“任一 leg 有 residual,且 risk-pass legs 中无显式撤单腿”才整次 cancel-only。定论:该判定归 `ArbLiveExecutionEngine` barrier,在收齐 `expected_legs` 的 Risk pass 后、release 到任何 `ExecutionClient` 前执行;触发后不 release 本轮任何新 submit,仅按 residual 调用 tracked cancel,所有新 submit 本地 deny/reject,并经统一 finish outlet 与 `PairInFlightGate` 生命周期收口。per-client `_begin_session` residual 检查保留为无 metadata / fallback。详细设计单一真理源=`architectures/_cross-cutting/synchronization.md §8.4bis`;execution 接口见 execution §3.5/§4.1;测试 README 见 execution `3.5.5~6`、e2e `9b~9c`。测试:`pytest tests/arbitrage/execution/test_engine_barrier.py -q` → 5 passed。 |
| 2026-06-16 (#110) | **PM merge/redeem 改由 NT 连续 position 对账驱动,彻底删 PM HealthCheckLoop(设计/落地中)**。背景:#109 删了 OE HealthCheckLoop 后,用户要 PM 也彻底丢健康检查、严格走对账。定:① 开 `LiveExecEngineConfig.position_check_interval_secs=300`(全局,PM merge 节奏 = OE 对账节奏;反转本会话早先关 position_check 的决定——OE churn 已被 #109/R1 reload-only-if-stale 缓解)→ NT 周期调 `generate_position_status_reports(None)`。② **一次拉喂两用**:上游 `_fetch_user_positions` 全量拉一次 /positions、stash 原始响应 `_last_raw_positions`(含 `redeemable`/`neg_risk`/`condition_id`,NT 规范化 report 丢了);override 用 stash 喂 settlement,丢弃注入的 `_positions_fetcher`、不二次拉。③ 拉失败→`mark_position_dead`+抛(venue dead);拉成功→alive。④ **settlement fire-and-forget + single-flight**(`_settlement_inflight` 守卫 + `create_task`)——链上 tx 数秒,绝不 `await` 在对账方法里,否则卡 NT 对账循环+拖慢 inflight check;tx 失败只 log、不判 dead、下周期幂等重试。⑤ 删 PM `HealthCheckLoop`/`_run_health_check`,清孤儿 `health_interval_secs`。详细设计单一真理源=execution §4.6 +(liveness)§4.3bis(4)。✅ **#109 已验证(2026-06-16 re-probe 空闲盘口)**:此前据 §4.3bis(4) 2026-06-13 probe("prices WS 因常推无心跳")担心 #109 帧-gap 在空闲盘口误判 dead;但那是**活跃盘口**假象。`oe_heartbeat_probe.py` 挑安静盘口实测:600s 内 prices 仅 5 个 data 帧、却有 **23 个心跳 `'h'`(median 25.0s,max 35.4s)**→ **prices WS 空闲时照发心跳** → #109 被动帧-gap 判活成立、`staleness_timeout=300s` 充足,**无需修**。 |
| 2026-06-16 (#109) | **OE WS 存活封装进 handler,DataClient 对称 PM、彻底去 HealthCheckLoop(设计/落地中)**。背景:#108 后用户要"严格和 PM 对齐、不要健康检查监控"。澄清物理限制:PM 靠 NT pyo3 WS client **内部主动 ping-timeout** 抓静默死亡(DataClient 只见 close/disconnect 事件);OE WS 在 Playwright 页内、注入不了 keepalive ping,**做不到纯事件**。讨论后定的对齐做法:把存活检测**封装进 `OrbitExchWebSocketHandler`**(像 PM 把 ping-timeout 藏在 client 里)——handler 被动盯入向帧(含 SockJS 心跳)+ prices WS close,经内部 lazy self-rescheduling NT clock alert(读 `_last_frame_ns`,零 per-frame churn)统一 fire `on_disconnect`;DataClient 只注册回调 → reload,**与 PM `_schedule_delayed_connect` 对称**,自身无 HealthCheckLoop / 无周期 scan / 无 watchdog;connect-retry 也事件化(开页失败→延迟重试)。**接口对称、内脏不同**(PM 主动 ping vs OE 被动盯心跳)。⚠️ 依赖 prices WS 真发心跳(未实测,上 live 前用 `oe_heartbeat_probe.py` 扩 prices 验)。退役:OE DataClient 的 `HealthCheckLoop`/staleness poll/`_comp_last_frame_ns` 等(搬进 handler);PM ExecClient 的 HealthCheckLoop(merge/redeem)保留。详细设计单一真理源=`architectures/data/architecture.md §4.3`。 |
| 2026-06-15 (#108) | **`leg_settled` 退役,锁定 `VenueExecutionLiveness` + Risk 统一门控(设计/待落地)**。背景:继续 #105/#106 后,用户指出 order/position 可信度应按 venue 拆分,PM 必须 `order_alive && position_alive` 才算可交易;Strategy 计算机会前不应看 alive,应由 Risk 统一安全出口门控。讨论后定:① 新横切共享对象命名 `VenueExecutionLiveness`,只存 `venue_order_alive`/`venue_position_alive`,**不存第三份 `venue_alive`**(派生判断);默认 fail-closed,启动后为 false/unknown,首次完整 order+position reconcile 成功后才 true。② 普通每次下单**不**置 false;只有 stuck/in-flight retry、order/position reconcile 失败/超时/response 不完整、明确断连等“执行真相不可信”路径置 false,成功拿真实 response 再置 true。③ Strategy **不读** liveness、也不再读 `leg_settled`;Strategy 只发现机会并写 `arb:expected_legs`。④ Risk 在 `_check_order` 中保留 NT 父类 `TradingState` 检查,随后新增 required venues liveness gate:从 `expected_legs` 解析 partner legs 推导 required venues(PM+OE 两腿则两条 order 都检查 PM 和 OE),任一 required venue not alive 即 deny 并发布 `risk.opportunity.leg_denied`;无 metadata 的普通订单退化为当前 venue。⑤ NT `TradingState` 保持原生全局三态(`ACTIVE/HALTED/REDUCING`),不是 bitmask,不能组合 `REDUCING|ACTIVE`,也不表达 per-venue alive;venue liveness 不同步 `set_trading_state`,避免覆盖人工 HALTED 或误伤其它 venue。⑥ `ArbitragePortfolio` 移除 `LegSettledRegistry` 依赖,纯算 positions 指标;执行健康 fail-closed 只在 Risk。详细设计单一真理源=`architectures/_cross-cutting/synchronization.md §8.5`;Risk 接口/顺序见 risk §3.1/§4.4;execution 写入约束见 execution §4.3bis/§4.4 失效指针;测试 README 已补 strategy/risk/execution/PM/OE 用例。 |
| 2026-06-14 (#107) | **opportunity execution barrier 代码落地(待 live 验证)**。按 #106 设计完成最小实现:① 新增 `src/arbitrage/common/opportunity.py` 统一 `Order.tags` metadata 构造/解析(`OpportunityMeta`、`arb:opportunity_id/pair_id/leg_key/expected_legs/intent`、`risk.opportunity.leg_denied` 常量);② `PlaceBetsAction` 每次 action fire 生成同一 `opportunity_id`,为所有真实 legs 写 `pair_id/leg_key/expected_legs`,不造 0 qty 空单;③ `make_submitter` endpoint 从 `ExecEngine.execute` 改为 `RiskEngine.execute`,并把 metadata 写入 `Order.tags`;④ `ArbitrageLiveRiskEngine._deny_order` 保留 NT 原生 `OrderDenied` 后额外发布 `risk.opportunity.leg_denied`;⑤ 新增 `ArbLiveExecutionEngine` 并由 `install_arbitrage_engines()` 替换 kernel `.LiveExecutionEngine`:risk-pass legs 先暂存,收齐 expected legs 才 `super()._execute_command` release;deny/timeout 经 `_finish` zero-session 出口,对 pending pass legs 发本地 `OrderDenied`,并释放 `PairInFlightGate`;pass 后仍由既有 `ArbExecutionSessionMixin._end_session` 在真实 session 全部完成后释放 in-flight。测试:`pytest tests/arbitrage/strategy/test_submitter.py tests/arbitrage/strategy/test_action_place_bets.py tests/arbitrage/risk/test_engine.py tests/arbitrage/execution/test_engine_barrier.py tests/arbitrage/risk/test_bootstrap.py tests/arbitrage/debug/test_bootstrap_integration.py -q` → 43 passed。文档:common/strategy/risk/execution 详设 + strategy/risk/e2e README 已同步;CLAUDE.md 仅项目索引,本次 `__init__.py` 只导出新类,无需更新。 |
| 2026-06-14 (#106) | **opportunity execution barrier 设计收口(设计/待落地)**。背景:用户指出当前 PM/OE 两腿若逐 `SubmitOrder` 过 Risk,会出现 PM 过、OE 被最小 stake/余额/三门限拦的非协调场景;同时确认希望在 **ExecutionEngine 层**等待同一机会所有腿的 Risk 结果,而不是在 RiskEngine 内异步等待。讨论后定:不改 NT `SubmitOrderList`(只支持同 `instrument_id`,不能表达跨 venue / 多 selection opportunity),继续用多条 `SubmitOrder`,但 Strategy 每条 order 写 `arb:opportunity_id/pair_id/leg_key/expected_legs/intent` tags,submitter 入口改走 `RiskEngine.execute`;Risk 保留原生 `OrderDenied`,额外发布 `risk.opportunity.leg_denied`;新增 `ArbLiveExecutionEngine` opportunity barrier,位于 Risk pass 后、ExecutionClient 前,收齐 expected legs 才 release 到 venue,任一 deny 或 NT clock barrier timeout 则 zero-session finish,对已暂存 pass legs 补本地 `OrderDenied`。**关键约束**:pass / deny / timeout 都必须进入同一个 opportunity execution finish outlet 释放 `pair_inflight`,不得在 deny 分支直接释放;timeout 使用 NT `Clock.set_time_alert_ns/cancel_timer`,只覆盖 Risk decision 收齐窗口,不替代 per-session venue watchdog。**详细设计**:`architectures/_cross-cutting/synchronization.md §8.4bis` 为单一真理源;strategy §3.9 / risk §3.3 / execution §3.5 仅写本方职责。**测试 README**:strategy `strategy-4.23~25`,risk `risk-6.7.10~11`,e2e `e2e-6~9`。 |
| 2026-06-13 (#105) | **健康检查迁移到 NT 原生 reconciliation —— 决策定向(设计/待落地)**。讨论确认:自写 HealthCheckLoop(OE 页面 reload / PM report 拉取)与 NT 原生对账高度重叠,**改用 NT 原生 reconciliation 承担"拉 order/position 对账"**,PM 的 merge/redeem(链上结算)仍自写(NT 不碰)。**为什么**:(a) NT 原生对账=启动期 `reconcile_execution_state`(engine 级 `reconciliation` 开关,遍历所有 client `generate_mass_status`;kernel 时序 `connect → await engines connected → reconcile`,故 reconcile 时 OE 已登录,但 `timeout_connection` 必须 > OE 登录最坏耗时)+ 启动后单后台 loop(in-flight check 2s **只查卡>5s 的在飞单**=stuck-order 修复非 alive 探测、open_check 默认关、position_check 默认关);(b) `reconciliation=False` 只关启动期,持续 in-flight loop 仍按 `inflight_check_interval_ms` 跑——对 OE 有 Path B 风险(回查读陈旧 `_current_bets` 快照→5 次重试后本地强判 Rejected/Canceled,而单可能真活着,踩 `bug_compensating_cancel_missing`)。**定了什么**(均见详细设计,本条只记指针):① **OE 取 order/position 接口 = reload 抽成接口、"reload-then-report" 进 OE ExecClient 自己的 `generate_*_reports`**(reload 归属从 DataClient 搬到 ExecClient,**修正 #62/§4.3 的"宿主=DataClient"**);② `_current_bets` 全量快照为 OE 单一真理源,**用户确认 CURRENT_BETS 保留全成交 bet → 快照=完整 venue 真值**,order 视图 + position 视图(聚合 `sizeMatched×averagePrice`,补 Q17 延后)都从它算,**一次 reload 喂两个视图**;③ **single-flight + 存活闸**:健康态读实时 `_current_bets` 不 reload,只在 exec WS 判死/单卡死才 reload(恢复性);④ **exec 页 WS 存活检测**仿 PM NT Rust `WebSocketClient`(心跳 10s + idle_timeout 行情 60s/账户 300s + 自动重连重订阅),但 OE 不能主动心跳(Playwright 对页面 WS 只读、不能注入 ping)→ 改**被动**:用已在收的 SockJS 服务端心跳帧做存活锚 + idle 超时;⑤ **OE 页级互斥 = ExecClient 内一把 `asyncio.Lock`** 串行所有碰页操作(place/cancel/reload)+ single-flight 合并 reload —— 因 **NT 不串行化 reconciliation⊥execution**(所有命令 `create_task`、`reconciliation_active` 只写不读、无 page/client 锁;NT 假设 venue 是无状态 REST,对 PM 成立、对 OE 共享 Playwright 页不成立);⑥ **`place_bets` 顺序提交退役 → 回到并发 gather**(页锁补在正确的资源层后,上层 workaround 多余;更紧对冲窗口;**需 live 重验**,原"同时下单丢回执"是真盘观测);⑦ **`reconcile_in_progress` 不引入**——页锁 + leg_settled 给 OE 和 PM 一样的"对账⊥执行共存",`_hc_running`/`health_check.*` 退役;唯一 PM 没有的不对称是 OE reload 阻塞页面(可能延迟 OE 腿),真正该补的是 **venue-exec-liveness 闸(待议)** 而非 reconcile_in_progress;⑧ **pair_inflight 兜底迁移**:主防线 = per-session 超时 watchdog(NT clock 绝对 alert→`_end_session`,不变);**退役 max-hold + `health_check→clear_all`**;新兜底 = `try_enter(pair)` 发现 pair 标在飞时,**若全局 execution 非 alive 且所有 leg_settled true → `clear_all` 放行**(**和 PM 一致直读 callable**:launcher 注入全局 `is_execution_active`,内部 OR PM/OE 两个 ExecClient 的 `_execution_active`=`len(_active_sessions)`;**`execution.*` 消息退役** —— 旧消费者 OE DataClient 互斥随迁移消失、无消费者)；⑧b **leg_settled 置位不变量(安全关键)**:`true ⟺ 拿到 venue 真实 response`——真实 WS 帧 + reconcile 成功(real response)→ 置 true(成功 pull = 完整快照,该 venue 所有腿);**NT Path B(重试耗尽、无真实 response、本地 fabricate)绝不 mark**,leg_settled 留 false 让 settled gate 安全继续挡住 pair,直到真实 reconcile 成功才解封(对冲 `bug_compensating_cancel_missing`);实现上 mark 从通用 `_send_order_event` 漏斗移到 venue-sourced 路径(WS handler + reload-then-report 成功点)。**核实的边界**:execution-alive 与 `_exec_count` 同在 `_end_session` 里减(同源)→ 整段没跑的泄漏由 watchdog 兜、`_exec_count` 与 exec-alive 失同步的泄漏由 try_enter 兜,两者全覆盖;用 execution-alive(权威"有无 session 在跑")而非 open-order(正常执行中也可能 leg_settled 全 true 且无 open 单)做判据。**待议项已收口(2026-06-13 同回合定)**:① **venue 死活 = reconcile 成败(统一,OE reload/PM REST 对称)→ venue-liveness 预闸(callable 直读)+ reconcile 失败持续重试**;② **OE position**:`net=ΣBACK−ΣLAY`,`avg_px=主方向侧成交量加权 averagePrice`(反向当平仓只减 qty;mixed 也据此,N/A 于 net=0);③ `timeout_connection ≥ 180s`;④ in-flight check **保持开**(OE 同步终态+页锁,极少触发);reconcile 无真实 response **明确返回失败**(不当空快照);⑤ **open/position check 默认关**(漂移再开 position_check);⑥ idle_timeout=300s(silence 触发探测 reconcile,非死活判据本身)。**仅余 live 微调(非阻塞)**:~~SockJS 心跳真实周期~~(✅ 2026-06-13 `scripts/oe_heartbeat_probe.py` 零下单实测 general WS ≈35s,`idle_timeout=300s` 定稿)、reconcile 重试 cadence/backoff、`place_bets` 并发后两腿回执重验。**详细设计**:横切同步部分=`_cross-cutting/synchronization.md §8`(页锁/状态位迁移/pair_inflight 兜底/NT 不串行化事实);OE 组件部分=execution `architecture.md`(reload 接口/single-flight/存活闸/`_current_bets` 双视图/WS 存活);data `architecture.md`(reload 宿主迁出 DataClient)。**§6.8/Q13 的"自写健康检查"详设与本条冲突处已就地挂失效指针(规则 6)**。**测试 README**:待详细设计落定后补 OE adapter / strategy / synchronization 用例。 |
| 2026-06-11 (#104) | **`mean_rebate_recovery` Check 落地并注册,支持实盘 recovery 验证**。延续 #102/#103:用户确认不重复 PM-only cancel-only,直接修改临时配置实测 recovery。落地:`MeanRebateRecoveryCheck(min_repaired_rebate, fx)` 从 snapshot 持仓计算每个 outcome 实际 share,目标取当前最大实际 share,只为缺口 outcome 选择当前 best ask 最便宜 venue 并写带最终 `qty` 的 recovery legs;补齐后的最差 rebate 必须不低于阈值。launcher 内置注册 `mean_rebate_recovery`,compensation tree 可配置 `place_bets(intent="recovery")` 下补救单并由 #103 Risk intent 契约放行 rebate gates。详细设计=strategy §3.8/§3.9;测试 README=strategy Slice 9/9.5。 |
| 2026-06-11 (#103) | **补救下单 intent 契约:recovery 跳过 Risk rebate gates,但仍走基础检查和余额检查**。背景:用户希望用 `match_sl=-0.005` 作为 live 测试闸阻止继续开新套利仓,同时不挡后续 recovery 补救单。讨论确认 `CancelOrder` 本来不经 `_check_order`,不会被 `match_sl` 拦;会被拦的是触发 cancel-only 或 recovery 的新 `SubmitOrder`。落地:Strategy `PlaceBetsAction(intent="...")` 经 submitter 写 NT `Order.tags=["arb:intent=<intent>"]`;Risk 默认按 `arbitrage` 旧行为处理,仅对 `recovery` 跳过 `_check_rebate_gates`(`match_tp/match_sl/global_sl/settled fail-closed`),不跳 NT 父类基础检查和 `_check_balance`。详细设计=strategy §3.8/§3.9 + risk §3.1;测试 README=strategy Slice 9/10a + risk `risk-6.7.6`。 |
| 2026-06-11 (#102) | **mean_rebate recovery 配置收口:复用 `place_bets`,不新增 `repair_mean_rebate` Action**。讨论确认 recovery 的目标是把不完整持仓补到 `target_share=max(actual_share_by_outcome)`,并用 `min_repaired_rebate`(如 `-0.05`)约束补齐后的最差 outcome rebate;`target/max_extra_share` 配置冗余,先不引入。落地范围先做 Action 边界支持:`PlaceBetsAction` 接受 Check 写入的 `leg["qty"]`,size 优先级为 `qty_overrides[venue] > leg["qty"] > share 公式`,因此未来 `mean_rebate_recovery` Check 可写补缺口 legs/qty 并复用 `place_bets`。`arb_config.example.json` 不启用未实现 Check,只在 strategy 详细设计保留配置形态,避免示例启动失败。详细设计=strategy §3.9;测试 README=strategy Slice 9/10a。 |
| 2026-06-11 (#101) | **`way_rebate` 分母改为最大实际腿 share,对齐 `mean_rebate` 下单语义**。背景:用户指出 `mean_rebate` 修复后各腿 rebate/share 应一致,而组合指标若继续除以配置 share,在 live probe/手动仓位/覆盖价格数量导致腿 share 不一致时会把真实持仓归一化错。**决策**:`ArbitragePortfolio._compute_way_rebate` 的分母不再读配置 `share`,改为参与计算 legs 的最大实际腿 share:PM=`size`,OE=`size*price*fx`(stake×odds×fx gross payout)。正常 `PlaceBetsAction` 会让 PM size 与 OE gross payout 都等于 action share,因此等 share 路径结果保持语义一致;非等 share 路径按最大腿归一化。测试:风险侧新增/更新 `_compute_way_rebate` 用例覆盖等 share 与非等 share。详细设计=risk §4.1;测试 README=`risk-6.9.2`。 |
| 2026-06-10 (#100) | **撤回 OE prices WS open 路径的 CDP 旁路探针,只保留 Playwright handler 单锚点**。背景:用户要求排除"open 有 CDP、reload 主要看 Playwright/或锚点不一致"带来的诊断混淆,复测 OE prices WS 时只用一条运行锚点。落地:`OrbitExchDataClient` 新开 competition page 时不再创建 CDP session / `Network.enable` / `OE CDP WS created/first frame` 日志,disconnect 也不再 detach CDP;保留 `OrbitExchWebSocketHandler` 的 `OE WS connected(type=prices)`、首帧、competition `ws_count/ws_types`、`OE price frame routed`、`OE OrderBookDeltas published`。测试:更新 `test_open_page_registers_playwright_ws_handler_before_navigation`,OE data/ws targeted tests 42 passed。详细设计=data §3.1;测试 README=OE adapter。execution/discovery 行为不变。 |
| 2026-06-10 (#97) | **PM adapter 主 CLOB client 切到 `py_clob_client_v2`**。延续 #96 live probe:去掉旧 SDK `post_only` 参数后,PM submit 不再 TypeError,但真实 POST `/order` 稳定返回 `400 {'error':'invalid order version, please use the latest clob-client'}`;独立查询仍确认 `open_order_count=0`,没有 PM 残单。按用户要求查 Polymarket 官方文档,当前 Trading / L2 client reference 明确以 `py_clob_client_v2` / `@polymarket/clob-client-v2` 为下单、批量下单、撤单、open orders、trades、balance allowance 的 SDK surface。落地:PM factory 改构造 `py_clob_client_v2.ClobClient`;ExecutionClient import / `get_open_orders` / `cancel_order(OrderPayload)` / `cancel_orders(order_hashes)` / `cancel_market_orders(OrderMarketCancelParams)` / TIF conversion / retry exception 全部切 v2;DataClient / Provider 类型注解同步 v2;`pyproject.toml` polymarket extra 从旧 `py-clob-client` 改 `py-clob-client-v2>=1.0.1,<2.0.0`,并更新 `uv.lock`。验证:PM adapter/config/OE targeted tests 129 passed,6 skipped。详细设计=execution §3.1 + data §2/§3.2 + discovery §3.2;测试 README=PM adapter `pm-adapter-5.1b` + discovery PM CLOB V2 记录。后续:需用户确认后重跑真 PM cancel-only live,目标是先看到 PM `orderID`/`venue_order_id` accepted,再验证下一轮机会 cancel-only。 |
| 2026-06-10 (#96) | **PM cancel-only live probe 暴露 `py_clob_client` 签名兼容仍未完全清理,修复并确认无 PM 残单**。用户授权后跑 NT `launchers/arb_node.py` 真 PM probe:debug OBD mock 只改行情、`skip_execution=false`,强制 mean_rebate 选择 PM 两腿并提交 `BUY 100 @ 0.01`。live 结果:OE prices WS 正常(CDP 与 Playwright 同页均看到 `multiple-market-prices`,并发布 OE OBD),PM strategy action 真实触发,但 PM adapter 没拿到 `venue_order_id`;随后 ExecEngine 对 `ClientOrderId` 查询报 `Cannot generate an order status report for Polymarket without the venue order ID`,订单超过 inflight retries。独立 CLOB 查询确认 `open_order_count=0`,未留下真实 PM 挂单,因此本次 **不能算 cancel-only 验证通过**(没有形成 NT 可追踪 open order)。根因:当前本地 `py_clob_client` 的 `ClobClient.post_order(order, orderType='GTC')` 与 `PostOrdersArgs(order, orderType)` 均不支持 `post_only/postOnly`,但 PM adapter 单笔路径仍传第三个 `post_only`,批量路径仍构造 `postOnly`。修复:`nautilus_trader/adapters/polymarket/execution.py` 去掉两处不兼容参数;补 `test_post_signed_order_uses_current_py_clob_post_order_signature` 锁签名和源码调用形态。验证:`tests/arbitrage/execution/test_polymarket_client.py` + PM/config 相关 56 passed。后续:需重新跑 PM cancel-only live,目标是先看到 PM `OrderAccepted`/`venue_order_id` 落 cache,再验证下一轮机会进入 `Execution session cancel-only` 并撤残单。详细设计=execution §3.1;测试 README=PM adapter `pm-adapter-5.1b`。 |
| 2026-06-10 (#95) | **OE prices WS 间歇捕获问题补断点日志,不恢复 reload/scroll 方案**。背景:live observe 中页面可见赔率但 NT node 长时间未见 `OE price frame routed`;此前为 prices WS readiness 做的自动 reload/DOM ready/scroll 已在 #92 撤回并清理,本次不改页面动作。核查发现 `OrbitExchWebSocketHandler` 自建 stdlib logger,其 `WebSocket connected/type` 等日志不稳定进入 NT node 日志,导致无法区分“prices WS 未被 Playwright 捕获 / 捕获但无下行 / 下行已到但解析或路由未进 DataClient”。落地:handler 支持注入调用方 logger,DataClient/ExecClient 分别传自身 `_log`;handler 低噪声输出 `OE WS connected(type=...)` 与每类 WS 首帧 `OE WS first frame received(type/kind/bytes)`,DataClient 继续输出 competition 页 `ws_count/ws_types`、首个 routed price frame、首个 OBD publish。进一步为 NT-node 真实 DataClient competition page 附加同页 CDP `Network` 旁路探针,输出 `OE CDP WS created(page_key/type/url)` 与 `OE CDP WS first frame(page_key/type/kind/bytes)`,用来和 Playwright `page.on("websocket")` 对照;CDP attach 失败只 warning,不阻断行情页。补 `test_first_frame_logs_ws_type_kind_and_size` / `test_open_page_attaches_cdp_probe_before_navigation`;详细设计=data §3.1 + execution §3.1;测试 README=OE adapter。Discovery 不涉及。 |
| 2026-06-10 (#94) | **`strategy.enabled=false` 语义修复:禁用 Action,保留 OBD 订阅桥**。背景:OE prices WS observe 时临时配置 `strategy.enabled=false`,但 live node 仍触发 `Submit LimitOrder`(SkipExecution mock)。核查代码确认 `StrategySectionConfig.enabled` 仅存在于 schema,`to_strategy_evaluator_config` 只映射 `log_evaluations`,`to_strategy_registry` 和 launcher 都未消费 enabled。设计校准:当前 `StrategyEvaluator` 同时承担 MatchedPair→OBD 订阅桥与策略评估/Action;若 disabled 时直接不装 Actor,会连 PM/OE OBD 订阅也停掉,不利于 observe/smoke。因此落地在 config dispatcher:`strategy.enabled=false` → `to_strategy_registry(cfg)` 返回空 `StrategyRegistry`,Evaluator 仍接收 MatchedPair 并订阅 OBD,但 no_strategy 后 no-op,不会 fire Action/submit。补 `test_strategy_disabled_returns_empty_registry_even_with_bindings`;详细设计=configuration §2/§5/§7.5 + strategy §3.5/§3.7;测试 README=strategy/config。 |
| 2026-06-10 (#93) | **skip_execution 保留 execution session/gate 生命周期,修 NT-node skip smoke 后 `pair_in_flight` 长期占用**。背景:继续 skip-execution observe 验证 #92 后,链路已到 `MatchedPair`、OE competition page `ws_types={'orders':1,'prices':1}`、`Strategy action fired`;PM 两笔 mock submit 出现 `Submit LimitOrder` 且 portfolio position 更新,但后续所有评估都 `reason=pair_in_flight`。根因:#84 per-pair 闸设计中 strategy fire 后不释放,交给 execution `_begin_session` / `_end_session` 的 `exec_started/exec_finished` 清理;而 #40 `SkipExecution{PM,OE}Client._submit_order` 为了跳真 venue,旧实现连 `_begin_session` 也跳过,导致 mock fill 虽然进 NT 事件管道,但 gate 没执行生命周期。落地:`src/arbitrage/debug/execution_clients.py` 新增 `_mock_submit`:skip 下先 `_begin_session(command)`,返回 True 才 `_mock_fill`;full fill 经 session `_send_order_event` 触发 `_end_session`,发布 `execution.finished` 并释放 per-pair gate;返回 False(cancel-only / residual open order)则不 mock fill。仍不碰真 venue IO,不引入 timeline。P11 判定:这是 Debug execution mock 与既有 synchronization 契约的兼容修复,单一自然归属在 `_cross-cutting/debug-injection.md`,不新增同步章节。测试:`test_debug_execution_clients.py` 新增 `_mock_submit` begin/fill 顺序、cancel-only 不 fill、OE skip cancel-only 不 fill;设计=debug-injection + execution 横切咬合;测试 README=debug #93。 |
| 2026-06-10 (#92) | **OE prices WS readiness 诊断候选撤回,保留 #87 可见性修复**。背景:曾根据一次 competition 页打开摘要只有 `orders` 推断 `networkidle` 后可能缺 `prices` WS,并考虑 open 缺 prices 后 reload/等待 readiness。后续核对日志发现该样本在约 21s 后已出现 `OE price frame routed` / `OE OrderBookDeltas published`,且 2026-06-10 零下单 DevTools/CDP probe 证明:同一 competition 首开、无滚动、无 reload 时,CDP 与 Playwright `page.on("websocket")` 都能捕获 `multiple-market-prices` WS 和 price frames,页面 DOM 同时已有赔率。因此撤回“open 缺 prices 立即 reload / DOM ready / market scroll / 固定 5s prices ready warning”方案,不作为设计事实。当前代码只保留 #87 已有证据的可见性处理:`page.bring_to_front()` + BrowserManager 可见态 init script;价格链路验收仍以 DataClient 的 `OE price frame routed` / `OE OrderBookDeltas published` 为准。 |
| 2026-06-10 (#91) | **DebugDataClient 补最小 ODDS→OrderBookDeltas mock builder,用于按既有测试架构驱动 PM/OE 选腿验证**。背景:验证“PM 两单 → 下一轮 cancel-only”前,用户要求先确认项目里是否已有可用 OBD mock;核查结论是 Q11.A 只有 `_DebugDataClientMixin` seam,`debug_config.json` 里旧 `mock_odds_*` 无 NT 新路径消费者。撤回未确认的 `MeanRebateCheck.venue_overrides` 方案,改沿 Q11 既有数据流替换路径落地最小 builder:Debug PM/OE DataClient 在 `_handle_data(OrderBookDeltas)` 前按 `MockCategory.ODDS` + conditions(`instrument_id`/`venue`/`market_type`/`selection_role`) 匹配,将 `data.bid|back` 生成 BUY 档、`data.ask|lay` 生成 SELL 档,输出 snapshot `CLEAR + ADD`。生产 DataClient 不变,debug disabled 不触达;更复杂场景仍可子类化覆盖 `_maybe_substitute`。P11 判定:Debug 注入横切机制已有单一真理源 `_cross-cutting/debug-injection.md`,本次是 Q11.A 既有机制补实现,不新增同步/互斥章节。详细设计=debug-injection §落地状态/§3,测试 README=debug `debug-A.4/A.5`。 |
| 2026-06-10 (#90) | **PM proxy/funder 钱包 `signature_type` 配置接线补全,修 NT 新路径余额误读 0**。背景:准备验证“PM 两单 → 下一轮 cancel-only”时,用户指出 PM 实际有余额,但 NT live 连接日志显示 `0.000000 USDC.e`。只读 CLOB 探针用同一凭证对比确认:`signature_type=0 → 0.000000 USDC.e`,`signature_type=2 → 67.916080 USDC.e`;旧 `odds_client` 也硬编码 `signature_type=2` 初始化 proxy wallet。根因:NT 配置 schema/loader/dispatcher 未暴露 `venues.polymarket.signature_type`,上游 `PolymarketExecClientConfig` 默认 0。落地:① `PolymarketSectionConfig.signature_type:int=0`;② loader 支持 env `POLYMARKET_SIGNATURE_TYPE` 并转 int;③ dispatcher 将其透传给 PM Data/Exec config。补 config loader/dispatcher 单测。P11 判定:配置面→PM adapter 主从接线,有单一自然归属,不新增横切机制。详细设计=configuration §3/§4/§6,测试 README=PM adapter `pm-adapter-2.3c`。 |
| 2026-06-10 (#89) | **OE 登录后弹窗关闭语义校准**。DevTools/Playwright 实测登录后 `postLoginPopup` 容器与 OK 按钮都存在,现有 selector 不是根因;用户明确要求不要靠固定等待,而是等待弹窗出现后点击浏览器主界面任意处,等待超时则继续。落地:`OrbitExchExecutionClient._dismiss_post_login_popup` 从“sleep 2s → 找 OK → click OK”改为“`wait_for` `div[class*="_postLoginPopup_"]` 可见(timeout=7000ms) → `page.mouse.click(24,160)` 点主页面区域”;timeout/无弹窗仍吞掉并继续连接。补 `test_dismiss_post_login_popup_clicks_main_page_when_popup_visible` / `test_dismiss_post_login_popup_timeout_continues_without_click`。P11 判定:OE ExecutionClient 内部登录弹窗处理,单一自然归属明确,不成横切章。详细设计=execution §7 Gap C;测试 README=OE adapter `oe-adapter-5.client.popup.{1,2}`。 |
| 2026-06-10 (#88) | **Strategy `PlaceBetsAction` 增加 venue-keyed 下单价/量覆盖,用于真钱 live 验证 cancel-only 而尽量不成交**。背景:用户要求继续验证“下一轮机会先撤未成交旧单”的 live 行为,但上一轮使用真实 OE best ask 导致一笔真实成交。按 #39 已锁原则,下单价掉包不放 execution(执行层透明传递),而是作为 Strategy Action first-class 参数。落地:`PlaceBetsAction(share, price_overrides=None, qty_overrides=None)`;覆盖只作用于最终 submit spec,不改变 `MeanRebateCheck` 用真实 OBD best ask 计算机会/选腿。live probe 可配置 `price_overrides={"ORBITEXCH": 1000.0}` + `qty_overrides={"ORBITEXCH": 7.0}`,让 OE BACK 单成为高赔率 limit 且 stake 保持最小值,用于制造 unmatched 残单并观察下一轮 cancel-only。补 `test_action_place_bets.py` 覆盖大小写 venue key 正规化与 submit spec 输出。详细设计=strategy §3.8/§3.9,测试 README=strategy Slice 9/10a。 |
| 2026-06-09 (#89) | **健康检查 ⊥ 执行互斥改为 per-venue**(用户指出)。背景:OE 健康检查(DataClient)订 `execution.*` 维护 `_exec_active_count`,**没按 venue 过滤 → 数的是全局**(PM+OE 腿都算),导致 OE 健检会因为「PM 在执行」而多等;而互斥本质是 **venue-local IO 冲突**:OE reload 只跟 OE 下单冲突、PM merge/redeem 只跟 PM 执行冲突。**修**:`data.py` 加 `_is_oe_exec_msg(msg)`(按 `msg["instrument_id"].venue == ORBITEXCH` 过滤),`_on_execution_started/finished` 只数 OE 自己的腿。PM 侧本就只读 `self._execution_active`(自己 session),**已是 per-venue,不动**。**Strategy⊥健康检查方向仍是「任一健检在跑」**(strategy fire 跨双腿)—— 那道(#88 的 `_hc_running` 集合)不变。**测试**:`test_data_client_step2.py::test_exec_active_refcount_only_counts_oe_legs`(OE 腿计入、PM 腿不计入、不负)。**arb 全量 489 passed,0 regression**。设计回写:synchronization.md §1/§2.1/§3/§5/§7.6(「全局互斥」全面改 per-venue)。**「≤1 套利在飞」改由 strategy 全局 `is_execution_active` + per-pair 闸保证,不再是本互斥的连带红利**。 |
| 2026-06-09 (#88) | **per-pair 闸泄漏兜底:健康检查触发 `clear_all` + 补 strategy⊥健康检查互斥**(#84 续）。背景(用户提):#84 的 per-pair 闸若执行链异常(`_end_session` 在 `exec_finished` 前抛 / fire 了但腿全 deny / 超时 alert 丢)会让 `_inflight`/`_exec_count` 卡死、该 pair 被永久挡;原 max-hold 自愈被动且清不到 `_exec_count`。**用户拍板的方案**:① 利用健康检查 ⊥ 执行互斥,strategy 订 `health_check.*`(本就该有、一直没接)——用 **per-venue 在跑 source 集合 `_hc_running`(非 ref-count)**:`started`→add/`finished`→discard,非空即「有健检在跑」;`_route_eval` 加 `if _hc_running: 放弃 fire`(补 §6.10 缺失的 strategy 侧互斥,避免下单撞正 reload 的页);② 收到任一 `finished` 且 `_hc_running` 空 且 `leg_settled.has_any_unsettled()==False`(arb 级判据、和 per-pair 闸同粒度)→ `PairInFlightGate.clear_all()`(清 `_inflight`+`_exec_count`)。**不叠加 `is_execution_active False`**:accept→terminal 窗口清掉无害,全局 is_execution_active 兜新 fire(用户判定)。**接线**:StrategyEvaluator 注入 `leg_settled`。**附带修**:`add_actors` 的 `asyncio.get_event_loop()` 在 Py3.13 无当前 loop 时抛 → 加 try/except 现地建 loop(先前 codex `_run`/`asyncio.run` 测试改造留下的全套件污染,4 个 launcher bootstrap 测试在全跑时挂;**非本功能引入**,deselect 我新测后仍挂可证)。**测试** +5:`test_pair_inflight::clear_all` + `test_evaluator` eval.17-19(健检在跑放弃 / 健检全结束+全 settled→clear / 有腿未结算→不 clear）。**arb 全量 489 passed,0 regression**。设计=synchronization.md §7.6。**未做(留后续)**:OE 健康检查 `is_execution_active` 目前 ref-count 全局 `execution.*`,按 per-venue 应只数 OE 自己的腿(用户指出,不影响本次 clear_all)。 |
| 2026-06-09 (#87) | **OE prices WS 缺失的 DevTools/CDP 复核 + competition 页可见性修复**。用户指出浏览器 DevTools 里能看到 prices WS,但 NT live node 中只稳定收到 `general`/orders WS。直接打开 OE competition 页分析:前台页面资源包含 `/customer/ws/multiple-market-prices/.../websocket`,CDP `Network.webSocketFrameReceived` 能抓到目标 market(`1.258948658`)价格帧;同时 bundle 中 `useDocumentVisible` / `useMarketsPricesVisibleSocketParam` 明确读取 `document.visibilityState` / market 可见性参数。结论:不是 OE 不发 prices,而是 live node 的 competition 页在 data/exec 共享浏览器下可能处于后台或 market 不可见,导致页面未订阅 prices WS。修复范围保持在 OE data/browser 路径:BrowserManager 初始化脚本固定 `document.hidden=false`、`visibilityState='visible'`、`hasFocus=true`,并让 `IntersectionObserver` 视为可见;DataClient 新开或 reload competition 页前 `page.bring_to_front()`。补 `test_data_client_step2.py` 断言新开和 reload 分支都会前置页面;详细设计=data §3.1,测试 README=OE adapter #87。 |
| 2026-06-09 (#86) | **PM `py_clob_client.post_order` 签名兼容修复 + live 暴露的单腿风险收口**。live 重试时确认 discovery/matching 正常(`MatchedPair ATP\|Nick Kyrgios\|Corentin Moutet`),OE prices/OBD 正常流入,但 PM submit 稳定报 `TypeError: ClobClient.post_order() takes from 2 to 3 positional arguments but 4 were given`;同时 OE 腿已真实 accepted,造成单腿活单风险。先用 `scripts/gapc_place_cancel_probe --cleanup-only --confirm` 清理 OE 残单并确认活单数 0。根因:当前本地 `py_clob_client` 签名为 `post_order(order, orderType='GTC')`,`PostOrdersArgs(order, orderType)` 也无 `postOnly`;上游 PM adapter 仍向单笔 `post_order` 传 `post_only`,batch 构造 `PostOrdersArgs(..., postOnly=...)`。修复:`adapters/polymarket/execution.py` 去掉这两处不兼容参数;补 `test_polymarket_client.py::test_post_signed_order_uses_current_py_clob_post_order_signature` 锁当前库签名和源码调用形态。验证:`tests/arbitrage/execution` 34 passed,`git diff --check` passed;修后 live 不再出现 PM TypeError。后续 live 样本触发的是 mean_rebate 当前算法合法形态:每个 outcome 选最低隐含概率腿,当两方向 OE 都更便宜时会只提交 OE 两腿;第二轮机会按现有 cancel-only 识别并撤旧单。收尾 cleanup-only 又发现并取消此前 Ctrl-C 中断留下的旧 OE 活单 `offerId=222050712`,最终活单数 0。详细设计=execution §3.1;测试 README=PM adapter `pm-adapter-5.1b`。 |
| 2026-06-09 (#85) | **OE submit+track timeout 语义 live 校准:venue 回执已到,未成交等待满 30s 是正确行为**。用户确认后用 `launchers/arb_node.py` 跑真钱 OE 路径,PM 因余额为 0 未下单;OE 两笔订单在 `11:03:02` submit,`placeBets` 均返回 `status=OK + offerIds`(`222032569`/`222032570`)。NT `OrderAccepted` 事件本身无独立日志锚点,但成功路径代码会在 result 后调用 `generate_order_accepted`,且下一轮机会进入 cancel-only 时能从 cache open order 取到 venue_order_id 并撤旧单,两笔旧单均 API cancel 成功。30s 后 `Execution session timeout` 不是 venue 回执没到,而是 Q15 设计:OrderAccepted 不重置也不结束 submit+track session,只有 FILLED/CANCELED/REJECTED/EXPIRED terminal 或 timeout 结束。此前“同一 Playwright page 并发 write IO 导致 place 回包不返”的诊断候选被 live 排除;已撤回 `_page_write_lock`、executor request/response logger 注入和对应单测/文档。当前不做 timeout cleanup / recovery,Q15 默认契约保持不变。设计记录见 execution architecture §4.2/§7 + OE README `oe-adapter-5.timeout.6`。 |
| 2026-06-09 (#84) | **per-pair 机会串行闸(修同毫秒并发重复下单)**。#82 实盘暴露:强制机会跑真执行时,一批 OBD 突发让**同一 pair 同毫秒 fire 多笔真单**(4 笔 OE @02:15:37.374)。**根因(经设计逐层核对)**:strategy 评估是 `create_task` **并发**派发,而所有在飞信号(`leg_settled` arm / `execution.started` / settled gate)都在**异步 `_submit_order` 下游**(`_begin_session`)才置位 —— §6.10 §4「单 loop + 置位在 await 前同步」的纪律对 strategy 这边不成立(strategy 无法同步发 execution.started,submit→_begin_session 是 NT 异步)。故全局互斥 + settled gate 能管「健康检查⊥执行」「跨轮重入」,但**结构上拦不住同 pair 同瞬间并发**。**解**(synchronization.md §7):新增**同步 per-pair 闸** `PairInFlightGate`(`src/arbitrage/common/pair_inflight.py`,launcher 经 ArbContext 注入 strategy + execution 共享一份)。strategy `_route_eval` **同步**(`create_task` 前)`try_enter(pair)`,已在飞 → 直接放弃;`_evaluate_and_fire` finally:未 fire → `release_eval`,已 fire → 不释放(交执行)。execution `_begin_session` per-leg `exec_started` / `_end_session` `exec_finished`,per-pair session 计数归 0 → 释放。**交接无空窗**(strategy fire 后持有到执行接手),**防泄漏** max-hold 陈旧自愈。正交于 §1-6 全局互斥(不同 pair 仍可并发)。**测试** +8:`test_pair_inflight`(6:并发放弃/不同 pair 独立/未 fire 释放/fire 交接/max-hold 自愈/负计数防御)+ `test_evaluator` eval.15-16(同 pair 并发只 fire 一次 / 不同 pair 不阻塞)。**arb 全量 458 passed,0 regression**。设计 = synchronization.md §7。 |
| 2026-06-09 (#83) | **MAKER 硬编码评估 = 无害,不改**(收口 #82 留的 MAKER 疑虑)。`_on_current_bets` 无条件 `liquidity_side=MAKER`,#82 曾担心 taker 单不准。**核查**:① OE 是博彩交易所,CURRENT_BETS 帧**无 maker/taker 字段**(`message_parser` 解析的全字段:marketId/selectionId/sizeMatched/averagePrice/side/profitNet/liability)——maker/taker 是 **Polymarket CLOB** 概念(`services/execution/tracker.py:on_polymarket_event` 的 TRADE_CONFIRMED `maker_orders`/`taker_order_id` 是 PM 的);② OE fill `commission=Money(0,GBP)`,无 maker/taker 费;③ 套利 rebate(way_rebate)在 strategy/portfolio 层算,**不读 `liquidity_side`**。故该字段对 OE **纯名义、不驱动任何费用/返水**,留 MAKER 即可,**不改代码**。回写 `_on_current_bets` docstring + execution §4.3 + OE README + gap_c 记忆(从「隐患」降级为「已评估无害」)。无代码逻辑改动(仅注释/文档)。 |
| 2026-06-09 (#82) | **Gap C Tier 2(真成交)live 验完成 + matched 帧抓取 + MAKER 隐患留记**。用户授权并确认后,Claude 直接跑 `scripts.gapc_fill_probe --confirm --size 7`(关沙箱、持久化 profile 真登录):真下市价 **BACK@1.01** 成交单(offerId=222016509,ATP Stuttgart 2026 / BACK Roberto Bautista Agut £7)。**抓到真实 matched 帧**(补上 [[gap_c_oe_exec_live_validated]] 此前缺的 matched 样本):`sizeMatched=7.00`/`averagePrice=2.3`/`sizeRemaining=0.00`/`price=1.01` —— **BACK@1.01 限价在最优 back 赔率 2.3 成交**(价格改善,averagePrice 是真实成交价非限价);`bet_order_progress` 派生 `status=filled,filled_qty=7.0,avg_px=2.3` ✓。**`generate_order_filled` 探针内 0 次触发 = 探针局限非 bug**:探针无 NT `ExecutionEngine` → `OrderAccepted` 未被 apply → cache 无 `venue_order_id` 索引(`Cache.add_venue_order_id`)→ `_on_current_bets` 的 `cache.client_order_id(voi)` 返 None → 跳过成交事件;reconcile 路径靠 market/selection 反查不受影响故正常。**事件路径离线补验**:`test_orbitexch_client.py::test_on_current_bets_matched_fires_generate_order_filled`(用真实 matched 值 + 预置 `add_venue_order_id` → `generate_order_filled` 以 last_qty=7/last_px=2.3/liquidity=MAKER 触发);该文件 9 passed。**⚠️ MAKER 隐患留记**:`_on_current_bets` **无条件硬编码 `liquidity_side=MAKER`** —— 对 mean_rebate 挂单(maker 吃返水)成立,但本真单为 TAKER 成交;系统若将来下 taker 单 liquidity 标记会不准(潜在,未改,留后议)。**至此 Gap C 三档全 live 通过**:connect #67 / Tier 1 place+cancel #78 / Tier 2 fill #82。**注**:该真单留下 BACK £7@2.3 真实持仓(探针按用户选不平不冲),用户手动处理。设计=execution §4.3 Gap C 分档 Tier 2 段。 |
| 2026-06-08 (#81) | **Gap C Tier 2(真成交)探针**。Tier 1(place+cancel 不成交,#76-78)通过后,补验最后一块:真成交后 `CURRENT_BETS` matched 帧填充值(`sizeMatched`/`averagePrice`)+ `current_bets_to_fills` delta + `generate_order_filled`(`last_px=averagePrice`,`liquidity_side=MAKER` 假设)。**建探针** `scripts/gapc_fill_probe.py`(用户授权"市价 BACK 最小注 + 仓手动处理"档):走生产同款发现取真实例、`OrderFactory.limit` 构 **市价 BACK@1.01**(等于以 ≥1.01 任意 back 赔率成交→吃最优 lay 盘立即 TAKER 成交;最大损=注金,该 selection 输)、复用 Tier 1 探针的发现/展示/最小注 helper。**与 Tier 1 关键差异**:会**真成交真持仓**,成交不可撤 → **不撤单/不对冲**(用户选仓手动处理),探针只下单+报告 matched 帧/`generate_order_filled` 实参(last_qty/last_px/liquidity)+ 显著持仓告警(打印 offerId + 未成交余量提示用 `gapc_place_cancel_probe --cleanup-only` 清)。**安全闸**:默认 dry-run(`--confirm` 才真下)。compile/import/`-m --help` 通过,**待用户真跑**(真金成交)。设计=execution §4.3 Gap C 分档 Tier 2 段。 |
| 2026-06-08 (#80) | **Strategy submit 边界修复 + NT-node skip smoke 跑到下单锚点**。用户指出不能在 strategy 下单/撤单未跑时结束。用临时配置(`/tmp/arb_config_force_mean_rebate.json`,仍 `skip_execution=true`)关闭 `pre_match` 并设 `mean_rebate.min_rate=-10.0` 强制机会后,真实 PM/OE 盘口触发 `PlaceBetsAction`,先暴露 bug:`make_submitter` 把 strategy legs 的字符串 `instrument_id` 直接传给 `cache.instrument(...)`,NT cache 期望 `InstrumentId` → `TypeError`。**修复**:`make_submitter` 边界接受 str/`InstrumentId`,统一转 `InstrumentId` 后查 cache/构 `LimitOrder`;补 `test_submitter` 字符串 id 覆盖,并把 submitter/action async 测试改成本工程可执行的 `_run(asyncio.run)` 风格。**验证**:相关离线 20 passed;复跑 NT-node skip smoke 后出现 `Strategy action fired` → `ExecClient-ORBITEXCH: Submit LimitOrder(...)` → SkipExecution mock fill → portfolio position 更新,证明 strategy submit 链路已实跑。**限制**:`skip_execution=true` 会立即 mock 全成,不会留下 open order,所以不能验证真实撤单/cancel-only;当前 cancel-only 主流程由 `tests/arbitrage/e2e/test_mean_rebate_cancel_only.py` 离线覆盖。**新暴露问题**:OBD 高频下同一机会会重复 fire/重复 mock submit,需后续按 strategy 执行保护/节流单独处理,不混入 recovery 状态机。 |
| 2026-06-08 (#79) | **PM exec balance hard-precondition 的 debug-smoke 专用缓解**。背景:[[bug_pm_exec_connect_balance_fatal]] 中 PM 上游 `PolymarketExecutionClient._connect` 会先读余额,transport 级 `PolyApiException(status_code=None,"Request exception!")` 可让 ExecEngine 不 connected → kernel 120s 后不启动 trader,即使 `skip_execution=true` 无真单也会卡住 Data/Matching/Strategy smoke。**决策**:不改生产语义,真下单仍必须读到余额;只在 `SkipExecutionPolymarketClient` 中覆盖 `_connect`:若 `skip_execution` 激活且 super `_connect` 抛 transport 级 `PolyApiException(status_code=None)`,记录显式 warning、启动 PM health loop并返回 connected;API 级错误(如 invalid api key,status_code 非空)仍 re-raise。**理由**:skip 模式只 mock 订单 IO,不会放真 PM 订单;容忍 transient balance read 可避免无真单 smoke 被 PM CLOB 网络抖动阻塞,但不声称 PM 余额路径已验证。**测试**:`test_debug_execution_clients.py` +2(transport 容忍/API 错误仍抛)。详细设计=`architectures/_cross-cutting/debug-injection.md` #66/#79。 |
| 2026-06-08 (#78) | **Gap C Tier 1 修后复跑通过**。用户再次明确确认后复跑 `scripts.gapc_place_cancel_probe --confirm --size 7`:真实 OE 下 `SELL(LAY)@1.01`(同 ATP Stuttgart 2026 / Pierre-Hugues Herbert v Martin Landaluce home,offerId=221973242,估算 liability 0.07 GBP)。**结果三项全过**:`_submit_order`→venue_order_id ✓;CURRENT_BETS working(`remaining=7.00,matched=0.00`) ✓;`generate_order_status_reports` 派生 1 条 ✓;修后的 `_cancel_order` 成功撤掉,脚本判定 `[✓] _cancel_order 撤掉(CURRENT_BETS 该单消失/remaining=0)` 并 exit 0。随后跑 `--cleanup-only` 零下单复查:CURRENT_BETS 保留 offerId=221973242 记录但 `remaining=0.00`,活单数 0。**结论**:Gap C Tier 1(place+cancel 不成交)已 live 通过;仍待 Tier 2 真成交 matched 帧填充值 + fill MAKER 假设。 |
| 2026-06-08 (#77) | **Gap C Tier 1 真单首跑 + 撤单 bug 修复**。用户明确确认后跑 `scripts.gapc_place_cancel_probe --confirm --size 7`:真实 OE 下 `SELL(LAY)@1.01`(event=Pierre-Hugues Herbert v Martin Landaluce,offerId=221972467,估算 liability 0.07 GBP)。**已 live 通过**:`_submit_order`→`venue_order_id` ✓、CURRENT_BETS working(`remaining=7.00,matched=0.00`) ✓、`generate_order_status_reports` 派生 1 条 ✓。**暴露 bug**:`_cancel_order` 构造 legacy Order 只带 `venue_order_id`,而 `executor.cancel_order` 要 `market_id` → 报 `missing market_id`,未撤掉;`_cancel_all_orders` 兜底也未清。**处置**:新增 probe `--cleanup-only`(零下单,打印 CURRENT_BETS,按 `marketId+offerId` 逐单 cancel,再 cancel-all + 复查);成功撤掉 offerId=221972467,最终活单数 0。**代码修复**(`orbitexch/execution.py:_cancel_one`):legacy cancel Order 带 `market_id`/`selection_id`,优先 instrument,缺则 `_current_bets[venue_order_id]` 回填;补 `test_cancel_order_passes_market_id_from_current_bets`。**验证**:OE execution/translation 28 passed;相关大组 111 passed。**状态**:Tier 1 的 submit/CURRENT_BETS/reconcile 已 live 通过;修后完整 `_cancel_order` true place+cancel 仍需用户再次确认后复跑。 |
| 2026-06-08 (#76) | **Gap C 真单 live 验拆两档 + Tier 1 探针**。Gap C(NT `OrbitExchExecutionClient` 下单/撤单/回执)live 验明确分档:**Tier 1**(place+cancel 不成交)验 `_submit_order`→venue_order_id→CURRENT_BETS working 态→`generate_order_status_reports` 派生→`_cancel_order` 撤掉;**Tier 2**(真成交)验 matched 帧填充值 + fill MAKER 假设(风险高,Tier 1 后另议)。**建 Tier 1 探针** `scripts/gapc_place_cancel_probe.py`(用户授权"不成交+完整 `_submit_order`/`_cancel_order`"档):真账户、走生产同款 OE provider 发现取真实例、`OrderFactory.limit` 构 LAY@1.01 + OE 最小 stake 7、SimpleNamespace 当 command 驱动真 `_submit_order`/`_cancel_order`(tracking 超时只结束 session 不补救故安全)。**安全修正**:BACK 方向 odds 越大越好,`BACK@1.01` 是最差价格,不能作为不成交保护;若未来必须验 BACK 方向,应另设高 odds 非市价探针并先检查盘口。**两道安全闸**:默认 dry-run(只打印将下的单,`--confirm` 才真下)+ finally `_cancel_all_orders` 撤单兜底(防 [[bug_compensating_cancel_missing]] 留活单)。compile/import 通过,**待用户真跑**。设计=execution §4.3 Gap C 分档段。 |
| 2026-06-08 (#75) | **OE 健康检查 Phase 2(状态维度 reload)live 验完成 + 安全闸默认开**。#74 解锁可配后,用真账户**零下单探针** `scripts/phase2_exec_reload_probe.py`(arm 未结腿不下单 → 驱动真实 `_run_health_check` → 触发 `_reload_execution_page`)验 Phase 2 reload 已登录 execution 页的两个未知:① 是否重现登录后弹窗(会盖页堵 general WS);② `CURRENT_BETS` 是否重推。**用户 2026-06-08 实测结论**:已登录后 reload **不重现弹窗**(弹窗仅首次登录)+ `CURRENT_BETS` **如期重推** → 安全闸当初默认关的全部理由消除;先前担心的"`_reload_execution_page` reload 后不补 `_dismiss_post_login_popup`"隐患**证伪,无需补**。**改**:`schema.py:OrbitExchSectionConfig.health_check_exec_reload_enabled` 默认 `False→True`(Phase 2 默认开;纯恢复机制,只增可靠性;运营可经 `venues.orbitexch` 显式关回)。adapter 层 `config.py` 默认保留 False(直构 fallback,异于经 dispatcher 的运营默认,同 interval/staleness 的分层)。**测试**:`test_dispatcher` 默认断言改 True + 加"可显式关回"用例(net +1,共 31)。**arb 全量 444 passed,0 regression**。**附**:discovery/matching 示例配置 `Men's Roland Garros 2026`→`ATP Stuttgart 2026`(法网已结束)。**设计**=execution §4.3 Phase 2 段 + `configuration.md §6` 表。 |
| 2026-06-08 (#74) | **OE 健康检查 cadence + Phase 2 安全闸接线补全(解锁 Phase 2 可配)**。背景:着手 Phase 2 实盘验证时发现——OE data client 运行时读 `self._config.{health_interval_secs,staleness_timeout_secs,health_check_exec_reload_enabled}`,但 `to_orbitexch_data_client_config` **三个都没透传** → 全吃 adapter 层硬编码默认(15/30/False)→ **Phase 2 安全闸在生产路径根本打不开、cadence 也改不了**;且 `venues.orbitexch` 已有的 `staleness_timeout_sec=300`/`page_refresh_sec=600` 与新默认冲突,ArbContext 里还有条死接线 `oe_health_interval_secs`(本想对称 PM 但 OE 不走 ctx-kwarg 模式,无人读)。**查证**:`page_refresh_sec` 注释称"10分钟周期刷新"但老栈 `_staleness_monitor_loop` 根本不读它——老栈真实行为纯 staleness 驱动(对齐 NT 新设计);PM `health_interval_secs` 走 ExecClient 构造 kwarg(`ctx.pm_health_interval_secs`,因 PM 宿主=ExecClient、复用上游 config 缺字段),OE 宿主=DataClient、config 自包含,应走 dispatcher 直传。**用户定 cadence**:tick=120s / staleness=300s(老栈值)。**改**:① `schema.py:OrbitExchSectionConfig` 加 `health_interval_sec=120.0` + `health_check_exec_reload_enabled=False`(`staleness_timeout_sec=300` 复用);② `dispatcher.to_orbitexch_data_client_config` 直传三字段进 `OrbitExchDataClientConfig`;③ 删死接线 `oe_health_interval_secs`(dispatcher init kwargs + `bootstrap.py:ArbContext` 字段);adapter 层 config.py 默认(15/30)保留为 fallback(离线测直构 config 不经 dispatcher,不动)。**测试**:`test_dispatcher` +2(三字段映射 / 闸默认关)、修 3 处删字段断言(dispatcher/launcher/risk bootstrap)。**arb 全量 443 passed,0 regression**。**设计**=`configuration.md §6`(新增 OE 健康 cadence 映射表 + PM/OE 接线差异说明)。**仍待**:Phase 2 真实 reload 行为的真账户 live 验 —— 已配**零下单探针** `scripts/phase2_exec_reload_probe.py`(真登录→arm 未结腿不下单→驱动真实 `_run_health_check`→观测登录态/弹窗/CURRENT_BETS 重推)。**弹窗隐患已 live 证伪(2026-06-08 用户实测)**:已登录后 reload 不重现登录弹窗(仅首次登录弹)→ `_reload_execution_page` 无需补 dismiss,reload 对会话安全。**剩余待验**:reload 后 `CURRENT_BETS` 重推(放闸最后一道确认)。详见 execution §4.3 Phase 2 段。 |
| 2026-06-08 (#73) | **PM 文档一致性修复(清 stale「健康检查拉余额」)**。Q17(§5.6/L111)早已锁定 **PM 健康检查不拉余额、余额完全靠事件驱动(连接 + 链上成交确认)**,但三处旧文字仍把「余额」混进健康检查动作、与 Q17 矛盾:① §6.8.4 健康检查伪码注释 `PM: 拉持仓/挂单/余额+对账`;② §7 Q13 索引行 `PM 内含周期拉持仓/挂单/余额`;③ §6.8.x `leg_settled` 状态流转表行 `PM health check 成功拉取 持仓/挂单/余额`。**改**:三处均改为「拉持仓/挂单(不拉余额,Q17)」并补一句余额靠事件。**全文复扫确认**:其余健康检查×余额命中要么已是「不拉余额」,要么是历史修订记录(L1821/1836 记录 Q-L(b)→Q17 翻盘演进,属审计痕迹不改);execution/data 详设文档本就正确。**未动** §6.2「上游 PM ExecutionClient 能力清单」中的「含余额查询(`generate_account_state`)」——那是在列举复用上游适配器的能力(余额查询走事件驱动路径),属实非矛盾。纯文档对齐、无代码改动、不涉组件行为/边界/数据流,故跳过测试 README 同步。 |
| 2026-06-07 (#72) | **OE 健康检查补「连接重试维度」(收尾 OE↔PM 韧性对齐)**。#70 Phase 1 只做了时间维度(已开页赔率冻结→reload),漏了 PM `_delayed_connect` 的真正对等——**初次开页失败的重试**:competition 页 goto 失败 → `_open_or_reload_competition_page` 清理+raise+不入册,而 `_run_health_check` 只遍历 `_comp_pages`(已开页)→ 失败页永不重试,该 competition 一直无赔率(#68 networkidle 超时即此场景)。**改**(`data.py:_run_health_check`):时间维度之后加一段——`set(_market_to_page_key.values()) - set(_comp_pages)`(已订阅未开)→ 本 tick 补开;**每页 try/except 吞掉,补开失败不本轮重试、留下一次健康检查**(复用 loop "异常吞掉、下轮重排"节奏,不打断其它页/Phase 2)。至此 OE 健康检查 = PM"连接失败重试"+"staleness 兜底"两者合一。**测试** +3(`test_data_client_step2.py` data-2.health.11-13:补开 / 失败留下次重试 / 已开不重复)。**arb 全量 441 passed,0 regression**。详细设计 = execution §4.3 Phase 1 / data §3.1;纯 DataClient 内、不碰交易页。 |
| 2026-06-07 (#71) | **健康检查文档样板重构(应用 design-docs 规则 1 共址 / 规则 6 失效就地标记 + 成熟度标记)**。背景:#70 暴露我读设计文档时"凭片段/grep 空下结论"屡错(把已设计+已建的 OE 健康检查说成"既无机制"等),根因之一是文档本身——健康检查详设散在 execution §4.3 + refactor.md §6.8.x、关键归属约束(宿主=DataClient,ExecClient 只 mark)是远处孤行、且 §6.8.3 写于 #68 拆页前未标失效。**改**:① §4.3 加「归属/不变量」共址块(把散落约束提到机制旁),声明 §4.3 为健康检查详设单一真理源;② §4.3 旧表(拆页前"reload→拉持仓/挂单")挂 ⚠️ 失效标记 → 指向 #68 澄清 + Phase 分期;③ refactor.md §6.8.3 加 ⚠️ 失效标记 + 把重复的"刷新动作/数据回写"详设收敛成指向 §4.3 的指针(只留"为什么"类决策);④ §6.8.2 状态流转表 stale 行(`settled=false→刷新 competition 页→同步持仓/挂单/余额`)修正为"reload execution 页(#68)→CURRENT_BETS 重推,不碰余额(Q17)"。**方法论同步**:这两条经验(共址 + 失效/成熟度标记 + 消费侧读法)已沉淀进 `design-docs` skill(Claude+Codex);"断言前核对原文"沉淀进 feedback 记忆。无代码改动。 |
| 2026-06-10 (#99) | **PM cancel REST 成功响应必须生成 `OrderCanceled`(修 cancel-only 只见 venue 清空、不见 cancel event)**。背景:用户要求复验 PM cancel-only:真 PM 路径已看到两笔 `OrderAccepted` + 真实 `0x...` `venue_order_id`,30s 后下一轮进入 `Execution session cancel-only`,最终只读 `open_order_count=0`;但没有 `Cancel confirmed` / `OrderCanceled` 日志。代码核对确认根因:`PolymarketExecutionClient._cancel_order` / deferred cancel / batch cancel / cancel-all-orders 只处理 `response["not_canceled"]`,对 PM CLOB 成功响应 `{"canceled":[order_id],"not_canceled":{}}` 不生成任何事件,导致本地 session/cache 不能从 REST 撤单结果终态化,只能等 USER WS 或后续对账。**修复**:新增成功分支 `_generate_cancel_success_event` → `generate_order_canceled` + 低噪声 `Cancel confirmed ...`;`not_canceled` 仍走 `_generate_cancel_event` / `generate_order_cancel_rejected`,其中 `"already canceled or matched"` 保持抑制语义;REST 与 USER WS cancellation 都按 cache `order.status == CANCELED` 幂等跳过重复终态。覆盖单笔、deferred、batch、cancel-all-orders;global/market cancel 仍 fire-and-forget。复验:PM 两笔真单 accepted(`ARB-4a313236`/`ARB-690db498`),下一轮 `Execution session cancel-only`,两笔均 `Cancel confirmed ...`,preflight `open_order_count=0`;live 同时暴露一笔 `CANCELED -> CANCELED` 重放,已补 REST 成功路径重复保护。测试:`test_polymarket_cancel_order_success_generates_canceled_event` / `test_polymarket_cancel_success_skips_duplicate_canceled_order` / `test_polymarket_cancel_order_reject_generates_cancel_rejected_event`。详细设计=execution §3.1;测试 README=PM adapter `pm-adapter-5.2`。 |
| 2026-06-10 (#98) | **PM CLOB REST 路由 + geoblock/API readiness preflight 修复(已落地,JP 误拦已修正)**。背景:PM cancel-only live 复验中 OE prices WS 与 PM market/user WS 都正常,但 PM v2 REST 下单和 `get_open_orders` 曾连续 `Connection reset by peer` / SSL handshake timeout;进一步核代码发现 PM WS 使用 `venues.polymarket.proxy_url`,但 `py_clob_client_v2` 内部共享 `httpx.Client(http2=True)` 默认读 `HTTP_PROXY/HTTPS_PROXY`,**项目 proxy_url 没接到 CLOB REST**,导致 WS/REST 可能走不同出口。按官方 docs 复核:CLOB L2 post/cancel/open-orders 都走 REST,下单前建议查 `https://polymarket.com/api/geoblock`;同时 docs 明确 `JP` 是 `Frontend UI restricted`(API 本身不限制),`AU/US/...` 才是 API blocked。**决策**:① `get_polymarket_http_client()` 配置 v2 SDK 共享 HTTP transport,显式 `proxy_url` 存在时 `trust_env=False`,保证 PM WS/REST 同路由;② `PolymarketExecutionClient._connect` 真连接前用相同路由做官方 geoblock preflight,但只对 API-blocked / close-only / blocked region fail fast;JP 这类 frontend-only 不误拦;③ launcher 加 `--preflight-polymarket` 只读入口,在 geoblock 后跑 CLOB `get_server_time()` + authenticated `get_open_orders()` + `get_balance_allowance()`,不 build node/不登录 OE/不下单,并提前暴露 proxy wallet `signature_type` 配错导致余额 0 或 SDK transport 超时的问题;④ `skip_execution=true` 仍只 mock 订单 IO,因此 PM debug 子类容忍 preflight `RuntimeError`(同 #79 transport 容错),不把 mock smoke 误当真钱可交易验证。2026-06-10 JP 出口实测 preflight OK:`server_time` 可读、`open_order_count=0`、`balance=67.916080 USDC.e`;AU/NSW 仍按官方 API-blocked fail fast。详细设计见 execution §3.1;data/discovery 只记录 provider/HTTP client 路由约束;debug 见 `_cross-cutting/debug-injection.md`;测试见 PM README `pm-adapter-2.3b` / `5.1c`、launcher README 与 debug README #98。 |
| 2026-06-07 (#68) | **OE 价格订阅改「每 competition 一页 + 新开/刷新统一」(已实现 + live 验证)**。用户核对发现:NT OE data client 退化成"单 `inplay/highlights` 页 + 路由表",**丢了原设计"每 competition 一页 + 新开/刷新走同一套接口"**(老 odds_client `_open_or_reload_page` 的模型)——`inplay/highlights` 只给概览、不含完整盘口,是 OBD delta 稀疏的根因。**决策**:① **每 competition 一页**(key=`{sport_id}_{competition_id}`,instrument 已带 `competition_id`+`event_type_id`=sport_id,订阅时即可定位);② **新开/刷新统一** `_open_or_reload_competition_page`(不存在→create_page+挂监听(#67 先挂后 goto)+goto;已存在→reload,监听跨 reload 自动存活 #67);③ **开页时机 = eager(订阅即开)**。**eager 的依据(用户两问澄清)**:lazy 只是**老代码(迁移前 odds_client)的实现**、**不是 refactor.md 的 NT 设计**(NT 设计在"何时开页"上是空白)→ 故这是新决策,**遵循 NT 架构自身**:`subscribe_order_book_deltas` 语义即"现在要这个 instrument 实时簿"(PM 侧订阅即连 CLOB market WS,对称),且 #61"`MatchedPair`→订阅→策略拿赔率"要求订阅即流;老代码 lazy 是因它无 NT 订阅契约、把开页挂自研健康检查 refresh_page。④ **关页 = 保持打开**(对齐老代码;competition 数有界)。⑤ §4.3 健康检查 reload 将来复用同一方法。**leg_settled 重复刷新疑虑(用户提)→ 定 模型 A**:§4.4 写死"leg_settled entry **首次 execution 才创建**、启动时无 entry",故启动阶段健康检查**两维度都不响**(状态维度无 entry;时间维度刚开页不 stale)→ eager-open(管 odds)与健康检查 leg_settled-refresh(管执行对账)**职责正交、启动不重复刷**;leg_settled 维持纯执行对账语义,不兼任启动初始化。**废止** §902 表"OrbitExchDataClient `data` 单页多市场"→ 改"每 competition 一页"。**详细设计** = data/architecture.md §3.1。**落地**(`data.py`):`__init__` 单页字段(`_page`/`_ws_handler`)→ `_comp_pages`/`_comp_handlers` 注册表;`_connect` 删 highlights 页+单 handler(只 `browser.start()`+发现);`_subscribe_order_book_deltas` 加 `_ensure_competition_page`(eager);新 `_ensure_competition_page`(从 instrument 取 sport_id/competition_id → page_key,去重)+ `_open_or_reload_competition_page`(不存在→create_page+挂监听(#67)+goto;已存在→reload);`_disconnect` 停所有 comp handler。**测试**:`test_data_client_step2.py` +6(订阅即开页 page_key 正确 / 同 competition 去重 / 并发同 competition 去重 / 已存在→reload / goto 失败不缓存脏页 / 不在 cache→不开)`;**arb 全量 427 passed,0 regression**。**live 验证完成**(codex,NT-node skip=true):competition 页开用 `networkidle` + page_timeout 统一 120s(#68 修首版 30s networkidle 超时),PM+OE 双边盘口 OBD 同场到齐并触发 StrategyEvaluator 重评,替代单 highlights 页的稀疏。详见 agent-notes `oe_competition_page_timeout_smoke68`。 |
| 2026-06-07 (#70) | **OE §4.3/§6.8.3 健康检查接线(Phase 1:时间维度赔率防冻)**。背景:补WS/连接韧性 PM/OE 对等——PM 有行情 WS 自动重连(`_delayed_connect`),OE 的对等物按设计是**健康检查页面 reload**(§6.8.3,宿主=DataClient),共享 `HealthCheckLoop` 已建但 OE 侧一直是未接线 seam。**用户澄清关键点**:恢复机制 = **reload 页 → 该页 WS 自然重推帧**(非 DOM 抓;page-level 监听跨 reload 存活,#67)。**#68 拆页澄清**:§6.8.3 原文写于拆页前,#68 后competition 页(DataClient,赔率)与 execution 页(ExecClient,`CURRENT_BETS`=持仓/挂单)分离,两触发维度 reload 落点不同 → 时间维度=competition 页(DataClient 自有)、状态维度=execution 页(需经共享 browser_manager)。**故分期**:**Phase 1(本次)= 时间维度**——`data.py`:`_connect` 挂 `HealthCheckLoop`(interval=`config.health_interval_secs` 默认 15s;Q19 互斥经 DataClient 订 `execution.started/finished` 自维护 ref-count `_is_execution_active`);`_on_price_frame` 每帧写 `_comp_last_update_ns[page_key]`(`_register_instrument_routing` 建 `_market_to_page_key`);`_run_health_check` 扫 competition 页 `now-last_update>config.staleness_timeout_secs`(默认 30s)→ 复用 `_open_or_reload_competition_page` reload 分支。config 加 `health_interval_secs`/`staleness_timeout_secs`。**不碰交易页、不碰 factory**。**Phase 2(待 OE 真单 live 验)= 状态维度**(`leg_settled=false`→reload execution 页),脚手架已铺。**测试**:`test_data_client_step2.py` +5(data-2.health.1-5:stale→reload / fresh 不刷 / 未收帧不刷 / exec ref-count 升降不负 / 收帧写 last_update)。**arb 全量 432 passed,0 regression**。**详细设计**=execution §4.3 落地状态段 + data §3.1;**测试**=OE README #70。**附**:核实发现我先前所述 PM `test_data_client_ws_retry.py` README 漂移**不存在**(README 用'重试/重排'非 'retry/reconnect',grep 假阴性)。 **Phase 2(状态维度,A 方案,用户 2026-06-07 选)**:`_run_health_check` 加状态分支——`leg_settled.has_any_unsettled()`(新增全局方法)真 → `_reload_execution_page()` 经共享 `browser_manager.get_page("execution")` reload ExecClient 的交易页(WS 重推 CURRENT_BETS → leg_settled 标);`leg_settled` 经 DataClient factory 注入。**A vs B**:B=改挂 ExecClient(与 PM 对称),A=守 §6.8.3 单宿主DataClient 经共享 browser_manager 够到 execution 页;用户选 A,不改宿主归属。**安全闸** `config.health_check_exec_reload_enabled` 默认 False(reload 已登录交易页的弹窗/会话未经真单 live 验,验前不自动 reload)。测试 +6(leg_settled has_any_unsettled 1 + data-2.health.6-10 状态维度 5)。**arb 全量 438 passed,0 regression**。Phase 2 真实 reload 行为待 OE 真单 live 验(需用户授权真账户)。 |
| 2026-06-07 (#67) | **OE 连接两 bug 修复(live smoke 抓出)+ 连接路径完整 live 验证**。连接 smoke(`launchers/arb_node.py` + skip=true,真连接零真单)中 OE 余额一直停在 0.00 占位、真 BALANCE 帧不来。查出**两 bug**:**①漏关登录后弹窗** —— `_login` 平移自 `scraper.py` 时漏了 `_handle_post_login_popup`(登录后点 OK);弹层盖住页面 → general WS 不推 BALANCE/CURRENT_BETS。补 `_dismiss_post_login_popup`(等 2s → 点 `//button[="OK"]`,无弹窗/出错都吞掉不致命)。**②WS 监听注册晚于页面建 WS** —— `OrbitExchWebSocketHandler` 用 `page.on('websocket')`,**只捕获注册之后**页面新建的 WS;而 `_connect` 原顺序是先 `goto/_login`(general WS 在登录导航 `/customer/` 期间已建)再 `ws_handler.start()` → 永远错过该 WS。**老 odds_client 注释明示正解**:"必须在 goto 前挂拦截,否则错过 WS 创建"。改:`ws_handler` 构造 + `start()` 提到所有 `goto/_login` 之前(`create_page` 后立即挂监听)。**exec + data client 同 bug 同修**(data 的价格 WS 同理)。**用户追问澄清**:此改与设计 §4.3"页面 reload→重订阅"不冲突——page-level 监听挂在 page 对象上,reload 换 WS 不换 page,监听仍在;而设计的"健康检查周期 reload+重订阅"整体仍是未接线 TODO(独立 slice,本次不做)。**验证**(smoke4):`popup dismissed` ✓ + OE AccountState `0.00 → total=37.49 GBP` 真余额 ✓ + 两腿 Connected + node RUNNING + MatchedPair(Cobolli/Zverev)+ 0 ERROR。**Gap C 连接路径(登录/弹窗/general WS/真 BALANCE 帧→账户状态)至此完整 live 验证**;仍待:OE 下单/撤单/成交回执(需真单)+ §4.3 reload 健康检查 slice。 |
| 2026-06-07 (#66) | **`skip_execution` 语义统一 =「真连接 + mock 订单 IO」,PM/OE 对齐(取代 #51)**。**背景**:准备 live-test Gap C 时发现——`place_and_cancel` scenario 跑老 services 栈、验不到 NT 适配器;而要验 NT 适配器只能跑 `launchers/arb_node.py`,但 OE 的 skip `_connect` 是 no-op(#51 当年因 Gap C `_connect` 还是 NotImplementedError 的权宜之计),导致 skip 下 OE 连都不连、验不了连接路径。**关键不一致**:PM 的 `SkipExecutionPolymarketClient` 从来只覆盖 `_submit_order`、`_connect` 照常真跑(真 CLOB 鉴权+余额),只有 OE 在 skip 下不连。**用户反馈**:别闭门造车、架构问题先设计;并指出 skip 本就该"只跳下单"。**定论**(对话议定后落地):skip 下两 venue 都**真连接**(OE 真登录/page/general WS/账户状态;PM 真 CLOB+user WS),只 mock 订单 IO(`_submit_order` 全成 + `_cancel_*` no-op)。**落地**(`debug/execution_clients.py`):删 `SkipExecutionOrbitExchClient._connect/_disconnect` 覆盖(继承 base 真连接);`_mock_orders()` = skip;PM/OE 都加 `_cancel_*` 在 skip 下 no-op(防拿 MOCK id 真撤)。**撤回**:对话中一度加的 `dry_run_execution` 旗标(多余——skip 本身即安全连接 smoke)。**收益**:`skip_execution=true` 跑 NT node 即「安全验 Gap C 连接路径(登录/WS/账户状态/余额帧/CURRENT_BETS 读侧)而不下真单」。**代价**:skip 下 OE 会真登录账户(与 PM 一致;"完全不碰真账户"模式本不存在)。**arb 全量 396 passed,0 regression**。设计见 `_cross-cutting/debug-injection.md` #66。 |
| 2026-06-06 (#65) | **OE reconcile hardening + schema 纠正(#64 的勘误)**。#64 实写 reports 时我写"bet 无 side、只能反查 NT order 补 order_side/instrument"——**错**:只信了 odds_client 那条**精选 5 字段** debug log。把老 `orchestrator.py:1076`/`tracker.py` 当权威一挖,真实 bet **自带** `side`(BACK/LAY)/`sizePlaced`(原始量)/`placedDate`/`price`。**教训**:bet schema 的权威来源是老代码实读字段,不是某条 curated log。**改正**(`orbitexch/execution.py`):① `bet_order_progress` 直接透出 bet 的 `side`/`market_id`/`selection_id`/`price`,`original_qty` 优先 `sizePlaced`(缺则 matched+remaining);② `_build_order_report` 优先用 bet 自带 `side`(BACK→BUY/LAY→SELL)、`_resolve_oe_instrument(market_id, selection_id)` 反查 instrument —— **NT order 不在 cache(外部/重启单)也能出 OrderStatusReport**;NT order 在则用其更权威 qty/price。**测试** +3(bet 透出 side/ids、原始量优先 sizePlaced、无 sizePlaced 兜底),`test_execution_translation.py` 20 case,**arb 全量 396 passed**。范围:纯 reconcile 派生逻辑,不碰真钱;成交 fill 路径(`current_bets_to_fills`)不变(fill 必须 join 到 NT 已 accepted order,无须改)。 |
| 2026-06-06 (#64) | **OE 订单回执实写(`_on_current_bets`→`generate_order_filled`)+ 一个重要勘误**。**勘误**:`place_and_cancel` scenario(`python3 -m src.arbitrage.testing`)经 `runner.py` 起的是**老 `src/arbitrage/services/` web_gateway 栈**(`WebGatewayService`),**不是 NT `TradingNode`**。2026-06-06 那次真钱 PASS(两腿 placed→cancelled)验的是**老 `OrbitExchExecutor`+`OrbitExchOdds` 路径(Gap C 前就能跑)**,**Gap C 的 NT `OrbitExchExecutionClient` 一行没跑**(日志零 `TradingNode`/`LiveExecutionClient` 痕迹;exec 侧 WS/`_on_current_bets` 无 log)。**教训**:别拿 scenario PASS 推断 NT 适配器可用;Gap C live 验须走 `launchers/arb_node.py`。([[gap_c_oe_exec_live_validated]] 已记)。**回执 schema live 抓帧确认**:那次真单 live 时 odds_client 抓到 populated `CURRENT_BETS` item = `{offerId, selectionId, averagePrice:0, profitNet, liability}`(+派生 marketId/sizeRemaining/sizeMatched)→ **修正旧假设:订单 join key 是 `offerId`(==NT order `venue_order_id`),不是 `marketId`**(注:本条同会话里又写错"bet 无 side",#65 已纠正——bet **自带 `side`**)。**落地**(`orbitexch/execution.py`):① `current_bets_to_fills(bets, prev_matched)` 纯函数 —— 快照**非增量**,按 `offerId` 算 `sizeMatched` delta,`delta>0 且 avgPrice>0` 才产成交意图(单一真理源复用 odds_client 字段语义);② `_on_current_bets` —— 调纯函数 → `offerId==venue_order_id` 反查 NT order(`cache.client_order_id(voi)`)→ `generate_order_filled`(last_qty=delta,last_px=avgPrice,GBP,commission=0,**liquidity=MAKER 假设**);accepted 由 `_submit_order` 同步发、撤单由 `_cancel_*` 发,此处只补成交;leg_settled 经 mixin `_send_order_event` 漏斗自动标记;`self._bet_matched`/`_bet_fill_seq` 维护 per-offer 累积+序号。**测试**:`test_execution_translation.py` +7(空/unmatched→无成交、新成交→full delta、增量→delta、同累积值→无、无价→跳过、缺 offerId→跳过),**arb 全量 388 passed,0 regression**。**(同会话续)`generate_order_status_report(s)`(reconcile)实写**:`_on_current_bets` 缓存 CURRENT_BETS 快照到 `self._current_bets`;`bet_order_progress` 纯函数(sizeRemaining/sizeMatched → accepted/partially_filled/filled/unknown)派生进度,`offerId==venue_order_id` 反查 NT order 补 order_side/instrument_id/price(**bet 无 side** → 查不到 log+skip);`generate_fill_reports`/`generate_position_status_reports` **有意返 []**(OE WS 无逐笔成交回放;持仓由 NT Portfolio 从 fills 自派生 + BALANCE 帧对账 Q17)。**测试** +5(`bet_order_progress`:缺 offerId/仅 remaining→accepted/部分/全成/都 0→unknown),**arb 全量 393 passed**。**真·仍待**:matched 帧填充值 + fill MAKER 假设(**真成交**才能验);健康检查 reconcile 前页面 reload(宿主 DataClient,另落);Gap C 整体 NT-node /live-test。 |
| 2026-06-04 (#63) | **Gap C 起步(安全增量)—— OE exec NT→executor Order 翻译纯函数 + `_place_via_executor` 接线**(真钱下单领域,分阶段:本次只做零风险可单测部分,真登录/下单/撤单走设计先行)。**勘明范围**:OE exec 4 个 `NotImplementedError`(`_connect`/`_place_via_executor`/`_cancel_*`)依赖件均已迁移存在 —— 登录 `scraper.py:login(user,pwd)`、下单 `executor.py:place_order(order,page)`(构 bet_uuid + payload → `page.evaluate` OE 前端 API)、状态 `websocket_handler`(general 频道)。**安全性闸**:`SkipExecutionOrbitExchClient._submit_order` 在 skip 模式 `_mock_fill` **不 super** → 真 `_place_via_executor` **仅非 skip 触达**(default config `skip_execution=true` 不下真单)。**落地**(`orbitexch/execution.py`):① `nt_order_to_legacy_order(nt_order, inst)` 纯映射 —— market_id/selection_id 取自 OE instrument 属性(缺→None)、side NT `BUY`→`BACK`/`SELL`→`LAY`、price=odds、size=qty、type GTC(POC);② `_coerce_handicap` —— `null_handicap`(NT sentinel **-9999999.0**)/NaN/None → 0.0(match-odds 无 handicap;executor `bet_uuid` 用 `int(handicap)`);③ `_place_via_executor` 改用纯函数 + 守卫(instrument 缺 / market_id 缺 / executor 未初始化 → log error 返 None,不崩)。**测试**:`test_execution_translation.py`(5:BUY→BACK/SELL→LAY、null_handicap→0、真 handicap 保留、缺 market/selection→None);**arb 全量 381 passed,0 regression**。**P11**:翻译 + 下单归 OE exec(单一归属 OE 适配器)。**(同会话续接)`_connect` + 撤单结构接通**(用户:mean_rebate 已设计、补偿后做,无设计卡点 → 直接接线):① `_connect` —— `browser_manager.start()`(幂等,#62 共享)+ `create_page("execution")` + `goto base_url` + **持久化 profile 未登录才 `_login`**(`/customer/` 不在 URL → 填 username/password 点 Log In 等 `/customer/`,平移 `scraper.login`)+ 构 `OrbitExchExecutor(ExecutionConfig())`+`set_page` + `OrbitExchWebSocketHandler(page).on_order_update(_on_general_frame)`(general 频道:余额/current_bets)+ 初始 `generate_account_state`(0 占位,真余额由 WS BALANCE 帧更新);② `_disconnect` 停 ws_handler(**不 close 共享 BM**,#62);③ `_cancel_order`→`_cancel_one`(NT cancel → executor.cancel_order(需 venue_order_id)→ `generate_order_canceled`/`_cancel_rejected`)、`_cancel_all_orders`→`executor.cancel_all_unmatched`、`_cancel_residual_one`→`_cancel_one`。**全部仅非 skip 触达**(SkipExecution override);**arb 381 passed 不变**(skip 路径不受影响)。**真·仍待**(需 live,非"设计"):① **/live-test 真验**(真登录 + 真下注/撤单,`skip_execution=false`,最危险,需用户明确确认才跑);② **订单回执 WS reconciliation** —— `_on_general_frame` 的 order 帧 → `generate_order_accepted/filled` 待 **live 抓真 frame schema 补全**(execution.py docstring "item schema 待 populated 抓帧",这是真 blocker、非设计);③ 补偿撤单**触发策略**([[bug_compensating_cancel_missing]]:单腿失败撤另一腿;`_cancel_residual_one` 已能"撤一单",触发逻辑后做)。 |
| 2026-06-04 (#62) | **OE 浏览器:两个可见窗口修复 —— data/exec 共享登录浏览器 + scraper 独立 headless 免登录**(用户报"弹两个浏览器,一个停在主页")。**诊断**(一度误判 data+exec,经用户提醒"scraper 免登录、data/exec 要登录、scraper 定时跑共用会打断"修正):两窗口 = ① OE DataClient 的 `PlaywrightBrowserManager`(`create_page("data")`→highlights,登录可见)+ ② **scraper 自己的 playwright**(`discovery_scraper.start_browser`,每轮 `discover_events` 开,headless 跟随 venue config → 可见、停在抓取页);**OE exec 在 skip 模式不开浏览器**(`OrbitExchExecutionClient._connect` 是 `NotImplementedError`/Gap C 未接,`SkipExecutionOrbitExchClient._connect` no-op,docstring 早已假设"BrowserManager 由 DataClient 起共享 context")。**根因**:§6.2"data+exec+discovery 共享单一 context"未落地 —— data/exec factory **各 `new` 一个 `PlaywrightBrowserManager`**;scraper 又自起一套 playwright,且用 `oe_venue.user_data_dir`(data/exec 的**登录** profile)+ 跟随 headless。**修**:① **data+exec 共享单例**:`bootstrap.ArbContext` 加 `oe_browser_manager`;`orbitexch/factories.py` 加 `_shared_oe_browser_manager(ctx,config)`(复用-或-建并回写),data/exec factory 都用它(NT build data→exec,data 先建 exec 复用)→ 一个登录浏览器。② **scraper 解耦**:`dispatcher.to_oe_scraper_config` 改 `BrowserConfig(headless=True, user_data_dir=None)`(免登录 → 非持久化 context;不抢 data 的 profile、不干扰登录会话、后台隐身)—— **不与 data/exec 共享**(用户明确:免登录 + 定时跑,共享会打断登录会话/抢资源)。**测试**:`test_dispatcher.py::test_arb_context_init_kwargs_includes_oe_scraper_config` 断言改 `headless is True`/`user_data_dir is None`;**arb 全量 376 passed,0 regression**。**Live smoke16**:OE DataClient connected + **`MatchedPair Arnaldi\|Cobolli (pm=2 oe=2)`**(`oe=2` 证 headless 非持久化 scraper 发现仍产 OE instrument)+ 0 Traceback/`user_data_dir` 错误 + graceful stop。**P11**:OE 浏览器拓扑 = data/exec 共享单例(§6.2,登录态)× scraper 独立(免登录)—— 归 OE 适配器(`factories` + `dispatcher`),单一归属,不入横切。**仍待**:Gap C(OE Exec 非 skip 真接线 —— 届时 exec 经共享 BM 取专属 page,不再多窗口)。 |
| 2026-06-04 (#61) | **slice 10e —— OBD per-iid 订阅 + OBD-driven 重评(让策略拿真实赔率算套利)**(接 #52 留的 seam;用户拍"继续")。**问题**:MVP 不预订 OrderBookDeltas → `build_snapshot` 的 `order_book` 恒空 → mean_rebate 算不出机会、永不出单。**落地**(`strategy/actor.py`):① `on_data(MatchedPair)` → `_ensure_obd_subscribed`:两边各腿(`pm_instrument_ids`+`oe_instrument_ids`)首见时 `subscribe_order_book_deltas(InstrumentId.from_str(iid))`,`_obd_subscribed` 去重;② 新 `on_order_book_deltas(deltas)` → `_route_eval`(NT 把订阅的 OBD 投到此回调;经 `instrument_id→PairRegistry→pair_id` 评估 = OBD-driven 重评);③ on_data 评估体抽 `_route_eval` 共用。instrument 已在 cache(slice A),OE/PM data client `_subscribe_order_book_deltas` 接 WS 流。**测试**:`test_evaluator.py` +1 `test_matched_pair_subscribes_obd_deduped`(两边各腿订 + 同 pair 去重);**arb 全量 376 passed,0 regression**。**Live smoke15**:`MatchedPair mensik-zverev` → StrategyEvaluator **4 个 `SubscribeOrderBook`**(2 PM token + 2 OE selection)→ `DataClient-POLYMARKET/ORBITEXCH: Subscribed ... order book deltas` ×4,0 ERROR —— **订阅链路成立**;PM CLOB 市场 WS 实连盘口时 `_delayed_connect WebSocketClientError: Operation timed out`(**PM 端可达性瞬时网络,环境性、非 slice bug**,同 [[bug_pm_exec_connect_balance_fatal]] 类),故本窗口真实盘口未稳定流入。**待验**(需稳定 PM WS + 真实赔率):盘口流入 cache → snapshot 非空 → mean_rebate 算出机会 → skip_execution mock 出单。**P11**:OBD 订阅 / 重评归 StrategyEvaluator(单一活体 Q21,PairRegistry 读者),不引入跨组件机制。**仍待**:Gap C(OE Exec 非 skip)/ sports `signal_collector` 真 collector / PM Exec+CLOB WS 非致命化(本会话 PM 端多次瞬时 outage,见 [[bug_pm_exec_connect_balance_fatal]])。 |
| 2026-06-04 (#60) | **PM Sports 比分信号接入 —— 实时赛事状态进平台,`ended` 驱动准确 eviction(替 #59 不准的 gamma expiration)+ 供 strategy**(实现 §5.9 设计稿;用户判定 gamma `end_date_iso` / Data API `redeemable` 都不准,改接 PM Sports WS)。**NT 零原生**(PM 适配器只连 CLOB market/user WS,无比分数据类型、从不连 sports-api —— 全库 grep 确认)。**外部接口 live 实采**(`wss://sports-api.polymarket.com/ws`,公开/无订阅/无鉴权,事件驱动稀疏):载荷 `sport_result` = `gameId`/`leagueAbbreviation`(实见 nhl/mlb/fif/wnba)/`homeTeam`/`awayTeam`(格式逐 league 异)/`status`/`score`/`period`/`elapsed`/`live`/`ended`/`finished_timestamp`(仅 ended);**无 slug/date**(文档不准);**映射键 = `gameId`**,与 gamma `event["gameId"]` 同值(wnba 13002300 双向对上;ATP series 10365 共 36 events 全有非空 gameId → tennis 映射键就绪)。**决策**(§5.9):D1 独立 `LiveMarketDataClient`(`PMSPORTS`)/ D2 `SportsGameUpdate` 事件 / D3 `gameId` 直配 / D4 **纯 `ended:true` eviction 无兜底**(用户定;`finished_timestamp` 与 ended 绑定不能当 fallback)/ D5 strategy 经 `signal_collector` seam / D6 firehose 消费端过滤。**落地**:① `adapters/polymarket/sports.py`(新):`SportsGameUpdate`(`@customdataclass`)+ `parse_sport_result` 纯映射 + `PolymarketSportsDataClient`(`_connect` 开 WS firehose、ping/pong、断线重连 → **`msgbus.publish` 裸发 `SportsGameUpdate` 到 `data.SportsGameUpdate*`**,消费者 `msgbus.subscribe` 带 #58 `*` 通配)+ `PolymarketSportsDataClientConfig`;② `arb_provider`:发现时抽 `event["gameId"]` → `instrument.info["game_id"]`(新增第 7 key);③ `matching/actor.py`:**删 #59 expiration reaper**(`_is_expired`/`_reap_stale_pairs`),改 `on_data(SportsGameUpdate)` → `ended` → 经 `game_id→pair_id` 索引 `_evict_game`(unregister + 加 `_ended_games`);`_maybe_match` 排除已结束 game 的 PM 腿(不再 re-register);`_emit_pair` 填 `game_id→pair_id`;④ `strategy/actor.py`:订 `data.SportsGameUpdate*` → `on_data` → `signal_collector`(用户域 slice 9;未设时 no-op,`_extract_evaluation_target` 对其返 None 不触发评估);⑤ `arb_factories.py`:`PolymarketSportsLiveDataClientFactory`;⑥ `dispatcher.to_sports_data_client_config` + launcher `data_clients[SPORTS_CLIENT]` + `add_data_client_factory`。**测试**:`test_sports.py`(parse live/ended/缺 gameId、roundtrip,4)、`test_actor.py` 重写 reaper 测试 → `test_sports_ended_evicts_pair` + `test_sports_update_non_ended_ignored`(matching 6)、launcher data-factory 计数 2→3;**arb 全量 375 passed / 33 skipped,0 regression**;PM 适配器 313 passed(唯 1 失败 = 既有无关 `test_data_client` MagicMock-await 坏测试)。**P11**:`SportsGameUpdate` 单一自然归属 = 生产者 sports DataClient(消费者 matching/strategy 只读)→ 挂 data 主方 + 跨引用,不入横切;`game_id→pair_id` 索引 + eviction 归 matching(PairRegistry 写者)。**Live smoke(#60 同会话,smoke12-14)抓出并修 2 个集成 bug**:(a)**client 名 `POLYMARKET-SPORTS`→`PMSPORTS`** —— NT node_builder `key.partition("-")[0]` 前缀路由,带 `-` 的名被错配到 POLYMARKET 主 factory(`AttributeError: no private_key`);(b)**`_handle_data`→`msgbus.publish`** —— DataEngine.process 拒裸自定义 Data(`Cannot handle data: unrecognized type SportsGameUpdate`),改裸 publish 到 `data.SportsGameUpdate*`(msgbus roundtrip 实测 received=1)。**修后验**:3 个 data client 启动(`Building data client for PMSPORTS`→RUNNING)、`Sports WS connected`、PM Loaded、`MatchedPair mensik-zverev`、**0 个 unrecognized-type 错误**、节点稳定 graceful stop;sports 帧实采到(smoke12 `r6siege game_id=1525326`,证 firehose 真推);加一次性"`Sports feed live: first update`"日志(稀疏 feed 运维可观测)。**待验**:WS 真推 **tennis** 比分(实采时巴黎凌晨无网球;gamma ATP 全有 gameId,映射就绪)+ 某 pair 的 game `ended`→真 evict(需赛事在窗口内结束,罕见)。**仍待**:slice 10e(OBD 赔率订阅)/ Gap C(OE Exec 非 skip)/ sports `signal_collector` 真 collector(slice 9 用户域)。 |
| 2026-06-04 (#59) | **slice A 落地 —— InstrumentRefresher 退役,周期发现迁 DataClient 原生 + matching 自 timer + eviction reaper**(实现 #58 §5.2.3 取向;用户拍"为什么不能继续往下做",当场实现)。**落地**:① **PM**(`arb_factories.py`):强制 `instrument_config.load_all=True`(`msgspec.structs.replace`)→ 上游已有的 `_update_instruments`(`initialize(reload=True)`)能调到 override 的 `load_all_async`(修 Gap α:#55/#57 重写时丢了 #53 的 load_all override → `initialize` 加载 0)。② **OE**(`adapters/orbitexch/data.py` + `config.py`):新增 `_send_all_instruments_to_data_engine`(`provider.get_all()`→`_handle_data`→DataEngine→cache+on_instrument)+ `_update_instruments(interval)` task(直调 `load_all_async`,Gap-α-proof)+ `_connect` 首抓 + `_disconnect` cancel;config 加 `update_instruments_interval_mins=60`。③ **MatchingActor**(`matching/actor.py`):删 `InstrumentsRefreshed` 订阅/`on_data`/`_both_recent`;改 `clock` 自重排 timer(`_MATCH_ALERT`)→ `_maybe_match`;latch = 两 venue `cache.instruments(venue)` 非空(替 2×window);加去重 `MatchedPair` INFO 日志(`_emitted_pairs`)。④ **Eviction reaper**(`matching/actor.py`):`_maybe_match` 排除 `_is_expired`(expiration_ns>0 且 <now)instrument + `_reap_stale_pairs`(任一腿过期/缺失 → `unregister_pair` + 清 `_emitted`);触发 = PM binary option `expiration_ns`(OE start_ts=0 无 expiration → 由 PM 腿驱动);**settlement 驱动清理(OE 侧)留后续**。⑤ **launcher**(`arb_node.py`):删 2× InstrumentRefresher 构造 + import + `to_instrument_refresher_configs`/`get_arb_context` 未用引用。**refresher.py/events.py/dispatcher.to_instrument_refresher_configs 留 dead code**(smoke 验后再删)。**测试**:`tests/arbitrage/matching/test_actor.py` 重写(on_data/2×window gate → timer+latch:`test_only_one_venue_in_cache_no_match`/`test_both_venues_in_cache_matches_and_publishes`/`test_on_alert_triggers_match_and_reschedules`/`test_match_unrelated_competition_no_pair`/新 `test_reap_expired_pair_unregisters`);`tests/arbitrage/launchers/test_arb_node.py` refresher 计数测试(4→2)+ 删 3 个 refresher 专属;**arb 全量 385 passed / 33 skipped,0 regression**。**Live smoke(smoke10)**:PM Exec 前 2 次撞 PM CLOB 余额端点持续 outage(`Request exception!`,**与 slice 无关**,[[bug_pm_exec_connect_balance_fatal]];掩盖 bug 修复后是干净 PolyApiException),第 3 次恢复;**全链路点亮**:`ArbPolymarketInstrumentProvider: Loaded 114`(原生 initialize)→ TradingNode RUNNING 无超时 → ~60s 后 matching timer → `MatchedPair ATP\|Jakub Mensik\|Alexander Zverev (conf=0.40, pm=2 oe=2)`,**refresher 全程未参与**(已退役)。**P11**:发现归 DataClient(NT 原生 owner)、matching 触发/eviction 归 MatchingActor(PairRegistry 唯一写者)、settlement-eviction 待接 execution —— 均单一归属,不入横切。**收尾(同 #59 会话已做)**:✅ 删 refresher dead code(`refresher.py`/`events.py`/`discovery.__init__` 导出/`dispatcher.to_instrument_refresher_configs` + 其测试,共 -15 tests → arb **370 passed**;`src/arbitrage/discovery/` 只剩 `__init__.py`+provider);✅ 组件 architecture.md(discovery §1/§3.3、matching §3.1/§3.3/§3.4/§4.4)+ 测试 README(discovery/matching)下沉同步;✅ **PM Exec 余额读取加有界重试**(`execution.py:_update_account_state` 3×/2s,缓解瞬时 blip;PM exec 108 tests passed)。**仍待**:settlement 驱动 eviction(OE 侧)/ slice 10e(OBD per-iid subscribe)/ Gap C(OE Exec 非 skip)。**注**:PM Exec 余额检查仍是 fatal connect 前提 —— read-retry 只挡**瞬时** blip,**持续** outage(2026-06-04 连挂 2 次 launch)仍 120s 超时 + trader 不启动;完整非致命化(connect-then-refresh,按旧 health-loop 设计)仍是 open decision([[bug_pm_exec_connect_balance_fatal]])。 |
| 2026-06-04 (#58) | **#57 后全链路 live smoke — 打通 discovery→cache→matching→MatchedPair→strategy,修 5 个 wiring 回归 + 更正 #52 误诊 + Q4/Q5 简化方向 + refresher 应迁 NT 原生的结论**(用户拍"继续"验 #57→MatchedPair,并提示"参考迁移前代码")。**起因**:#57 修好 PM 发现后跑 smoke,MatchedPair 仍 0 fire。逐层挖出 5 个**阻断整条套利管道**的真 bug(每个都被上游链路的另一个问题掩盖过):① **PM `_connect` 掩盖 bug**(`execution.py:271`):`get_balance_allowance` 网络抖动 → `PolyApiException(status_code=None)` 的 `.error_msg` 是 **str** 而非 dict,`e.error_msg["error"]` 抛 `TypeError(string indices...)` 吞掉真因 → ExecEngine never connected → 节点等满 120s 超时 → **kernel 跳过 `trader.start()`,所有 actor 卡 READY 不 RUNNING**(诊断锚:RUNNING 后 `ExecEngine.check_connected()==False` + actor 停在 READY = exec connect 失败、trader 没起);修:`isinstance(e.error_msg, dict)` 守卫(真因浮出;且该失败是**瞬时网络** blip,重连即恢复)。② **自定义事件订阅缺 `*` 通配**(`matching/actor.py`、`strategy/actor.py` on_start):**更正 #52 决策日志的错误结论** —— #52 称订阅路径是 `msgbus.subscribe(topic=f"data.{TypeName}")`,但 **NT `publish_data` 无 metadata 时 `DataType.topic` 带尾部 `*`**(实测 `data.InstrumentsRefreshed*`),订无星精确串**永不匹配** → `on_data` 从不触发(实证:无星 received=0,带星=1);#52 smoke 当时把 MatchedPair=0 误归因到 PM enricher 占位(Gap B+D 这个**同样真实**的原因),topic bug 被掩盖,#57 修好 PM 6-key 后才浮出;修:订阅串改 `data.{Type}*`。③ **provider→Cache 桥接缺失**(`refresher.py:_tick`):refresher 周期 `provider.load_all_async()` 只灌 **provider 内部 store**(`provider.add()`),从不写 NT `Cache`;而 matching `cache.instruments(venue)`、OE exec `cache.instrument(iid)` 都读 Cache → `pm_events/oe_events=0`;PM data client 连接时虽走原生 `_send_all_instruments_to_data_engine` 但 `initialize()` 此刻加载 0(真发现在 refresher),OE data client `_connect` 干脆无任何 cache 灌入;修:`_tick` 成功后 `for inst in provider.get_all().values(): self.cache.add_instrument(inst)`。④ **snapshot str→InstrumentId**(`snapshot.py`):PairRegistry 存 `str(leg.id)`,`build_snapshot` 拿去 `cache.order_book/instrument` 抛 `TypeError(expected InstrumentId, got str)`;修:cache 边界经 `_as_instrument_id` 转型(公开字段仍保 str 视图),positions 经 `str()` 比对。⑤ **PairRegistry 类型不一致**(`pair_registry.py`):matching `register` 写 str key,而 risk/portfolio/session 用 `InstrumentId` 对象 `get()` → dict 恒 miss(latent,smoke 未 fire 订单故未触发);修:register/get 两侧 `str()` 归一(单点修好所有消费者)。**Live 验证**(smoke6,skip_execution):**全链路点亮** —— 发现 PM=108 instrument(54 events 含主赛事 `mensik-zverev`,#57 目标)→ Cache(pm_events=54/oe_events=4)→ 配对 `results=1`(**Jakub Mensik\|Alexander Zverev** 配上 OE Roland Garros)→ MatchedPair fire → StrategyEvaluator 评估无崩溃;**不出单符合预期**(MVP 未预订 OBD → order_book 空 → mean_rebate 无机会;slice 10e 待接)。**测试**:arbitrage 全量 **197 passed**;全仓 **701 passed**(唯一失败 `test_data_client.py::test_disconnect_...` 经 stash 验证为既有无关坏测试 — MagicMock 不能 await);改 2 个夹具(`test_snapshot.py` fake cache 按 str 归一 + 非法 id `x`→`X.PM`;`test_instrument_refresher.py` stub 用真 `TestInstrumentProvider` instrument 替 `object()`)。**Q4/Q5 修订(用户定 + 讨论收敛)**:核实当前 matching 是**增量**(`PairRegistry.register` 逐对写、从不 `unregister_pair`/清空)+ cache 永远保留 last-good(`add_instrument` upsert,失败发现从不清零,全仓无 instrument 删除)→ "0 结果覆盖旧 pair" 的担忧在当前代码**不存在**;Q5 的 2×interval 新鲜度门控对正确性**不必要**,降为冷启动 latch "两个 venue 各 cache 非空"(原生 `cache.instruments(venue)` 可查),"多 venue 一失败不挡其他"自然成立。**但暴露真缺口:无 eviction** —— 结束的比赛/消失盘口永远留 registry+cache。**NT 无原生 instrument 清理**(Cache purge 只管 orders/positions/accounts,无 `purge_instrument`;NT 假设 instrument 宇宙有界稳定=crypto,而体育是无界 churn)→ 必须自写,触发应是**生命周期结束**(`InstrumentClose` / `expiration_ns`(PM `data.py:228` 已查)/ 现有 settlement 流)而非"发现没返回"(脆弱);清理动作 = `PairRegistry.unregister_pair`(已存在、未接线)。**架构结论(关键)**:`InstrumentRefresher` 实为**从零重造了 NT 原生 `DataClient._update_instruments`**(binance/bybit 范式:`_connect` 起 `create_task(_update_instruments)` → `while: sleep + provider.initialize(reload=True) + _send_all_instruments_to_data_engine()` → `_handle_data`→DataEngine→cache **且** `on_instrument` 通知;`_disconnect` cancel task)—— 本次 3 个 bug(#52 Gap E pending-task、cache 桥接、topic 通配)原生路径**全已正确处理**。**最终方向(初判 B,经上述 eviction / Q4-Q5 讨论收敛到 A + 独立清理)**:① **发现 add/update 走 (A) 全原生** —— 周期循环搬进 PM/OE DataClient(原生 `create_task` + `provider.initialize(reload=True)` + `_send_all_instruments_to_data_engine`→`_handle_data`→DataEngine→cache + `on_instrument`),refresher Actor 退役、手搓任务(Gap E)/cache 桥接/topic 通配代码全消除;matching 触发改用"两 venue cache 非空"latch,自定义 `InstrumentsRefreshed` 不再需要(故**不取 B**)。② **eviction 另起独立机制**:close/settlement 驱动的 reaper(`InstrumentClose` / `expiration_ns` / 现有 settlement 流 → `unregister_pair`),NT 不提供、本领域(无界 churn)特需。代价:OE data client 现**完全无** instrument 灌入需补全(`initialize` + `_send_all` + `_update_instruments` task);refresher 独有的运行时改 interval(msgbus)+ `on_save/on_load` 持久化(Q3/Q6)需评估去留/重接。**作为带决策日志的下个 slice**,本次先保当前可用状态。**P11 判定**:沿用 #52 —— `InstrumentsRefreshed`/`MatchedPair` 各单一自然归属(publisher×subscriber 非对称)→ 挂 matching/strategy 主方,**不入横切章**(更正本次中途一度提的 `_cross-cutting` 设想)。**文档同步**:本条(决策日志单一真理源)记全;`bug_pm_exec_connect_balance_fatal` memory 更新(掩盖 bug 已修)。**组件详细设计(discovery/matching/strategy architecture.md)的 5 修同步暂缓** —— 因 (B) 迁移将替换 refresher cache 桥接 + Actor-to-Actor topic 接法,现写永久设计会与即将到来的 slice 冲突(方法论规则 5 不超前);PairRegistry str 归一 + snapshot 转型是稳定项,随下个 slice 一并回写组件家。**slice 设计验证(已验框架假设,暴露 2 gap)**:**Gap α** —— `InstrumentProvider.initialize(reload=True)` 仅当 config `load_all=True` 才重跑 `load_all_async`,否则 "No loading configured" 直接 return;smoke 实测 **PM `initialize()` 加载 0**(#53 的 `load_all=True` override 在 arb_config 路径未生效,真加载全靠 refresher 直调 `load_all_async`)→ 故 `_update_instruments` 必须**直调 `provider.load_all_async()`**(像现 refresher),不走 initialize 的 config 闸。**Gap β** —— `subscribe_instrument_close` 在 DataEngine 有,但 **PM/OE data client 都不发 `InstrumentClose`** → eviction 触发只能用 `expiration_ns`(PM binary option 有,`data.py:228` 已查;OE `start_ts=0` 无 expiration)+ 现有 settlement 流(OE 侧基本只能靠 settlement)。**2 sub-decision 定**:① matching 触发 = **自带 timer 读 cache + 两 venue 非空 latch**(不订 `on_instrument` 免 108×/轮,不复活 `InstrumentsRefreshed`);② refresher 独有的运行时改 interval(msgbus)+ `on_save/on_load` 持久化(Q3/Q6)**迁移中降级,按需经 DataClient config 恢复**。**仍待**:下个 slice = 发现迁 (A) 原生(refresher 退役)+ Q4/Q5 降为"两 venue cache 非空"latch + matching 触发改造 + **独立 expiration/settlement 驱动 eviction reaper**(接 `unregister_pair`;NT 无 instrument cache 删除 API,只清 registry/活跃集)/ slice 10e(OBD per-iid subscribe,真赔率驱动评估)/ Gap C(slice 10b OE Exec 非 skip)。 |
| 2026-06-02 (#57) | **PM 发现链路修复 — 单次 `series_id` 全量查询(撤 #55 的 `/series/{id}` 截断跳)+ 2-way role 按 competition `ordering` 修反排**(接 #55/#56 series-based 发现;用户拍"先别慌写入,看世界杯行不行"逼出通用性验证)。**根因**:#55 链路 `/sports`→series_id→**`/series/{id}?closed=false`**(只内嵌截断的 ~10 条 events,漏主赛事 `atp-mensik-zverev`)→ per-event `/events?id=`;默认页又被 `limit=20` 截(与页面 react-virtuoso "懒加载"同源 —— 用户回忆"OE 设个参数就不懒加载了"= `limit`)。**公开 gamma API 实测验证**(httpx 只读):① `tag_id=101232`(ATP tag)`/events` 只返 **5 个 outright winners**;match-level H2H 在 **series**(`series_id=10365`)。② **`series_slug` 不通用**:`series_slug=atp`(70)/`world-cup`(0)/`fifa-friendlies`(0)—— 只 atp/wta 的 series slug 恰好 == league slug,足球/棒球查 0;**通用键是 `/sports` 给的 `series` id**,故 `/sports` 必须留。③ **`/events?series_id={id}&closed=false&active=true&limit=500` 一次拉全**(ATP 70 / 足球 bol1 100 / MLB 82),每 event **内嵌 `teams`+`markets`**(含主赛事)→ **省掉 per-event `/events?id=` 二跳**。④ **发现 2-way 队名错位 bug**:`/sports` 每 league 有 **`ordering`**(home/away)字段是 competition 特异属性 —— ATP `ordering=home` outcomes=`[home,away]`(旧固定下标碰巧对);**MLB `ordering=away` outcomes=`[away,home]`**(旧 `("home","away")[idx]` 把客队标成 home)。⑤ 世界杯当前 `/sports` 无 H2H series(只 tag_id=519 的 18 futures),要等赛事 series 上线;soccer 通用性已用 bol1 三向(`slug==ticker-{abbr}` Yes-only)验通。**落地**(`nautilus_trader/adapters/polymarket/arb_provider.py`):① `load_all_async` 读 `comp_info["ordering"]` 透传;② `_load_series(...,ordering)` 改 `/events?series_id=&closed=false&active=true&limit=500`(常量 `_EVENTS_PAGE_LIMIT=500`),iterate 内嵌 events;③ 删 `_load_event` fetch,改 sync `_process_event(event,...,ordering)` 直消费内嵌 dict;④ `_role_for_token` 加 `ordering` 参:`home_first = ordering.lower()!="away"`,2-way/单市场 3-outcome 按 ordering 选 `(home,away)`/`(away,home)`(及 `(home,draw,away)`/`(away,draw,home)`);3-way binary 分支(slug 后缀 abbr,只认 Yes)不变;**队名仍权威源 `event["teams"]`(`_teams_from_event`)+ title fallback,不赌 outcomes 顺序当队名**。**测试**:`tests/arbitrage/adapters/polymarket/test_arb_provider.py` 全 `_role_for_token` 调用补 `ordering=`,新增 `test_2way_ordering_away_reversed`(MLB)+ `test_3way_single_market_ordering_away_reversed`,**27 passed**(原 enricher 测文件 #55 已删);PM provider + discovery **51 passed / 3 skipped,0 regression**。离线又用真 gamma event dict(ATP/MLB/bol1)交叉验 `_role_for_token` 三案全对。**P11 判定**:PM 发现是 PM adapter 内部实现(单一自然归属),挂 `discovery/architecture.md §3.2 blockquote` 主方 + `tests/arbitrage/adapters/polymarket/README.md` 文件分布,**不需横切章**。**文档同步**:`discovery/architecture.md` PM blockquote 整段重写(撤 #53/#54 enricher+event_slug_builder 描述,改 #57 series_id 链路 + ordering role + series_slug 不通用 audit);`tests/arbitrage/adapters/polymarket/README.md` 文件分布删 `test_arb_provider_enricher.py` 行、`test_arb_provider.py` 行改 27-test #55/#57 摘要。**execution/data architecture.md 不涉及**(纯发现改动,exec/data client API 未变)。**仍待**:真 MatchedPair 触发 smoke(PM/OE 同赛事对齐后)/ Gap C(slice 10b OE Exec 非 skip)/ slice 10e(OBD per-iid subscribe)。 |
| 2026-06-01 (#53) | **slice 7B 落地 — PM `enrich_pm_six_key_info` 真写 + `event_slug_builder` `series_slug=atp` filter + 三次 live smoke 验**(#41 设计 slice 7B / Gap B+D 修;接 #52 slice 10d Gap A+E 修):用户拍 "继续",我推 slice 7B PM enricher 真写 + filter 接线。**Audit 关键发现**:① PM gamma `/markets` 单个 `market_info["events"][0]` 已嵌入父 event(含 ticker / slug / startDate)→ **不需另调 `/events/{id}`**;② Ticker 格式 `{competition}-{home_abbr}-{away_abbr}-YYYY-MM-DD`(如 `atp-matisse-thomas-2026-06-01`),末 3 段是日期可作 match-level 判据;③ Main winner market `market_slug == event_ticker`,`outcomes=["Home Full Name", "Away Full Name"]`(全名)— 2-way `[home, away]` / 3-way `[home, draw, away]` 索引推 role;④ Binary sub `{ticker}-{home_abbr}/{away_abbr}/draw` outcomes 是 `["Yes","No"]` 推 role;⑤ Sub-markets(`-first-set-winner-...` / `-match-total-...` / `-set-totals-...`)返空 6-key → matching 自动跳;⑥ Non-match events(`2026-mens-french-open-winner` outright winner ticker 末非日期)同样返空。**关键 audit:PM gamma filter**:`tag_slug=atp` 在 `/events` 错配(返 5 个 outright winners `2026-mens-french-open-winner` 等);`series_slug=atp` 才对(返 100 个 match-level `atp-{home}-{away}-YYYY-MM-DD` events);`event.tags[]` 是 `tennis/sports/games`,`event.series[]` 才有 `slug=atp`。**落地**:① `nautilus_trader/adapters/polymarket/arb_provider.py` 重写 `enrich_pm_six_key_info`:`_infer_selection_role` 内 main winner 按 outcomes 长度分支(2-way `("home","away")[idx]` / 3-way `("home","draw","away")[idx]`),binary sub 按 slug 后缀;`_PREFIX_TO_SPORT` map(`atp/wta→Tennis,epl/ucl/uefa/fifa→Soccer,nba→Basketball,nfl→American Football,nhl→Hockey,mlb→Baseball,mma/ufc→MMA`),unmapped 兜底 `market_info["category"]`;home/away 主 winner 用 outcomes 全名 / binary 用 abbr.title();start_ts 从 `event.startDate` ISO → ns;`_parse_outcomes` 兼容 JSON 字符串 + list;② 同模块加 `build_pm_event_slugs_from_arb_context() -> list[str]` sync callable:读 `ArbContext.pm_event_slug_tags` → `httpx.Client.get(gamma /events, series_slug=tag, closed=false, limit=500)` 返事件 slug 集去重排序;③ `src/arbitrage/bootstrap.py:ArbContext` 加 `pm_event_slug_tags: list = field(default_factory=list)`;④ `src/arbitrage/config/dispatcher.py` 加 `to_pm_event_slug_tags(cfg)` 扁平化 `cfg.discovery.polymarket.sports[].competitions` 去重 + `to_arb_context_init_kwargs(cfg)` 注新字段;⑤ `nautilus_trader/adapters/polymarket/arb_factories.py:ArbPolymarketLiveDataClientFactory.create` 检查 `ctx.pm_event_slug_tags` 非空时 override `instrument_config = PolymarketInstrumentProviderConfig(event_slug_builder="nautilus_trader.adapters.polymarket.arb_provider:build_pm_event_slugs_from_arb_context", load_all=True)`(`__post_init__` 强 load_all=True);⑥ `launchers/arb_node.py:build_trading_node_config` timeout_connection 20→120s(PM 初次 load 100 events ~35s,原 20s 触 NT post-build 卡死)。**测试**:`tests/arbitrage/adapters/polymarket/test_arb_provider_enricher.py` 14 tests:ATP 主 winner home/away、3-way Soccer 含 draw、2-way binary home_yes/away_yes/draw_yes、sub-market 跳过(first-set-winner / match-total / set-totals)、outright winner 不含日期、events 列表缺、start_ts 解析、outcomes JSON 字符串、未知前缀 fallback 走 category。**全 arb 套件 376 passed(+14 enricher,0 regression)** / 33 skipped。**3 次 live smoke**(同 slice 10c/10d 命令):**第一次:无 filter 全量 crawl** 50000+ markets 5+ 分钟未完;停。**第二次:enricher + event_slug_builder + timeout=20s(原)**:PM 35s load 2026 instruments 完成时已超 20s connect timeout → NT post-build 卡死(Refresher 不 start)。**第三次:timeout=120s**:**全干净**(subscribe_data ERROR=0,total ERROR=0,pending warning=0)。**指标**:① PM 初次 load `Loading instruments from 100 event slugs` → `Loaded 2026 instruments from 100 events` 35s;② 4 Actors RUNNING + Portfolio init + TradingNode RUNNING 全部 ~40s 完成;③ PM Refresher tick `POLYMARKET: refresh ok count=2026 (Δ+0)`(2026 个 ATP instruments 含全 6-key);④ OE Refresher tick `ORBITEXCH: refresh ok count=24 (Δ+24)`;⑤ graceful DISPOSED。**MatchedPair 不 fire 是市场逻辑非 bug**:PM `series_slug=atp` 100 events 多为 **Roland Garros JUNIORS / 各地 Tyler / 次级赛**(如 `atp-matisse-thomas-2026-06-01` = "Roland Garros Juniors, Boys: Matisse Martin vs Flynn Thomas"),OE config 锁的"**Men's** Roland Garros 2026" = 主赛大牌 — 不同赛事不可匹。要看 MatchedPair fire + mean_rebate 触发 + PlaceBets log,需:换更宽 sport(EPL 足球 / NBA 篮球 — PM 通常含 match-level + OE 同款覆盖)、或扩 PM filter 到 `tag_slug=tennis`(包含 main 主赛 + outright winners,enricher 自动 skip outright)、或 OE config 同时配多个赛事级别。**P11 判定**:PM enricher 是 PM Provider 内部实现(`ArbPolymarketInstrumentProvider._parse_instrument` 内),**单一自然归属**(PM adapter),挂 `discovery/architecture.md §3.2` 主方 + `tests/arbitrage/adapters/polymarket/README.md` 文件分布,**不需横切章**。**文档同步**:`discovery/architecture.md §3.2` PM 端 6-key 真写大段(ticker 格式约定 + selection_role 推导规则 + sport 推断 map + event_slug_builder 路径 + 性能对比 + timeout 调整);`_cross-cutting/configuration.md §10` slice 7B ✅ 加全详细;`tests/arbitrage/adapters/polymarket/README.md` 文件分布加 `test_arb_provider_enricher.py` + 14 case 摘要;`tests/arbitrage/discovery/README.md` 加 `discovery-7B.1` PM enricher 真写 + `discovery-7B.2` event_slug_builder 路径用例(含 audit 警示 `tag_slug` 错 + `series_slug` 对);launcher `timeout_connection 20→120s` 内嵌注释标 #53 原因。**仍待 #41 slice**:**Gap C**(slice 10b OE Exec 非 skip 模式真接线 — login/page/WS)/ **slice 10e**(StrategyEvaluator OBD-driven 重评 per-iid `subscribe_order_book_deltas`,MatchedPair fire 后调,非 MVP)/ **真 MatchedPair 触发** smoke(用户拍 sport / config 调整后)/ **`/live-test` 项目 skill 适配新 launcher**(slice 后做)。 |
| 2026-05-31 (#52) | **slice 10d 落地 — Gap A + E 修(msgbus 直订 + InstrumentRefresher clean shutdown)+ 二次 live smoke 验**(#41 设计 slice 10 之 d;接 #51 slice 10c 浮上 5 gaps,本 slice 修 A+E,B+C+D 留 7B/10b/7B):**Gap A 根因**:NT `Actor.subscribe_data(data_type, client_id=None, instrument_id=None)` 强制构造 `SubscribeData` cmd 经 DataEngine 路由 → 转发到具体 DataClient;**当 client_id 和 instrument_id 都 None 时直接 `log.error("...need to be specified") + return`**(`actor.pyx:1324`)。**但 `publish_data(data_type, data)` 内部就是 `self._msgbus.publish_c(topic=topic_cache.get_custom_data_topic(data_type), msg=data)` —— `data_topics.pyx:208` 无 instrument_id 时 topic = `f"data.{data_type.topic}"`(对 InstrumentsRefreshed 等 custom Data 类型,`data_type.topic` 默认是 type 名);所以 `publish_data` 是纯 msgbus 路径,但 `subscribe_data` 不是 —— 这是 NT API 的不对称设计**。MarketMatchingActor 收 `InstrumentsRefreshed`、StrategyEvaluator 收 `MatchedPair` 都是 **Actor-to-Actor custom 事件**(无 venue 归属),正确订阅路径是 `self._msgbus.subscribe(topic=f"data.{TypeName}", handler=self.on_data)` 直接走 msgbus broker。**Gap E 根因**:`InstrumentRefresher._on_alert` 是 sync callback,内部 `self._loop.create_task(self._tick())` 创建 task 但不存引用 → on_stop 时 task 未被 cancel 也未被 await → NT TradingNode dispose 时 asyncio 报 "Task was destroyed but it is pending"。**落地**:① `src/arbitrage/matching/actor.py:on_start` 改 `self._msgbus.subscribe(topic=f"data.{InstrumentsRefreshed.__name__}", handler=self.on_data)`;② `src/arbitrage/strategy/actor.py:on_start` 同上改 MatchedPair,**MVP 不预订 OrderBookDeltas**(原 `subscribe_data(DataType(OrderBookDeltas))` 同样报 ERROR,且 OBD 是 venue/instrument-tied,正确接法应 MatchedPair fire 后 per-iid 调 `subscribe_order_book_deltas(iid)` — 延后到 slice 10e 真接,MVP 用 MatchedPair 触发足以验全链路);③ `src/arbitrage/discovery/refresher.py` `__init__` 加 `self._tick_task = None`、`_on_alert` 存 `self._tick_task = self._loop.create_task(self._tick())`、`on_stop` 加 `if self._tick_task is not None and not self._tick_task.done(): self._tick_task.cancel()`。**全 arb 套件 362 passed,0 regression**(matching/strategy 现有 87 tests 不依赖 subscribe_data 实际路由,改成 msgbus.subscribe 表面是 actor 字段修改但对测试 mock 无影响 — 单测层 actor 行为不变;refresher tests 未覆盖 dispose 路径)。**二次 live smoke**(同 slice 10c 命令):**Gap A subscribe_data ERROR 3→0**;**Gap E pending task warning 1→0**;**OE InstrumentRefresher refresh 3 次推进**(28/28/28 instruments,Δ+28 / Δ+0 / Δ+0);剩 1 ERROR 是 `InstrumentRefresher-POLYMARKET: POLYMARKET: refresh failed: PolyApiException[status_code=None, error_message=Request exception!]` —— PM gamma crawl 10000+ markets 中偶发网络异常,**无关本 slice**(PM 上游问题类,类比 [[bug_polymarket_order_version_mismatch]]);`MatchedPair` 仍 0 fire 符合预期(slice 7B PM `enrich_pm_six_key_info` 占位空字符串 → PM 端不参与 matching → `_both_recent()` 闸 Q5 守门 —— OE 单边 refresh 推进时 PM 边 refresh 失败兼缺 6-key,matching `_maybe_match` 早返不出 MatchedPair);全程 graceful `node.dispose()` clean。**P11 判定**:`InstrumentsRefreshed`/`MatchedPair` 两 custom 事件各**单一自然归属**(InstrumentsRefreshed → publisher: InstrumentRefresher × subscriber: MarketMatchingActor 非对称;MatchedPair → publisher: MarketMatchingActor × subscriber: StrategyEvaluator 非对称),挂 matching/strategy 主方 + 跨引用,**不需横切章**。**文档同步**:`matching/architecture.md §3.3` 改 on_start 例码注释 slice 10d 修;`strategy/architecture.md §3.5` 同上 + 加 OBD MVP 不预订注;`discovery/architecture.md §3.3` 加 slice 10d Gap E clean shutdown 段;`_cross-cutting/configuration.md §10` slice 10d ✅ 详细 A+E 修 + smoke 结果;`tests/arbitrage/matching/README.md` 加 "Slice 10d 修(#52)" 段;`tests/arbitrage/strategy/README.md` 加 "Slice 10d(#52)" 段;`tests/arbitrage/discovery/README.md` 加 `discovery-10d.E` 用例(InstrumentRefresher clean shutdown 验收 0 pending warning)。**仍待 #41 slice**:**Gap B+D**(slice 7B PM enricher 真写 + sport filter,让 PM 端参与 matching 出 MatchedPair → mean_rebate 真触发)/ **Gap C**(slice 10b OE Exec 非 skip 模式真接线)/ **slice 10e**(StrategyEvaluator OBD-driven 重评 per-iid subscribe — MatchedPair fire 后 subscribe instruments;非 MVP 必要,有需要再做)。 |
| 2026-05-31 (#51) | **slice 10c 第一次 live smoke — launcher 真起 + OE Tennis × Roland Garros 28 个 instruments 真发现 + 浮上 5 处真接线 gap(就地修)+ 5 处遗留**:用户拍板调 `/live-test` 项目 skill;skill 是为旧 `src/arbitrage/testing/runner.py + web_gateway` 设计的,跟新 NT-native launcher 不通,但 **live-test-base 元流程(Phase 0 预飞行 → 1 启动 → 2 监听 → 3 异常处置 → 4 收尾)通用**,套用启动命令 `python -m launchers.arb_node --config arb_config.example.json`。**Phase 0 通过**:`.env` 7 keys / `arb_config.example.json` schema 合规 / Strategy mean_rebate 装到 competition:ATP / debug.skip_execution=true / OE headless=False(弹真窗口)/ Playwright 装 / launcher import OK。**Phase 1+2 跑出 5 处接线 gap,就地修复**:① `launchers/arb_node.py` 加 `load_dotenv(_PROJECT_ROOT / .env)` —— launcher 进程不自动 load,上游 PM `get_polymarket_api_key()` env fallback 触发 RuntimeError;② `dispatcher:to_instrument_refresher_configs` 加 `ComponentId(f"InstrumentRefresher-{venue}")` —— 两 Actor 默认 ID 重名 NT raise "Already registered actor with ID InstrumentRefresher";③ `nautilus_trader/adapters/orbitexch/browser_manager.py:start()` 幂等(`if self._context is not None: return`)—— OE Data/Exec 共享 BrowserManager,多方 start 不该重复 init Playwright;④ `nautilus_trader/adapters/orbitexch/data.py:OrbitExchDataClient._connect` 自管 `await self._browser_manager.start()` + 改用 `create_page("data")` —— **原 bug**:用 `get_page("data")`(只读 dict lookup)返 None → `await None.goto(...)` AttributeError;**且** factory 层根本没调 `start()`(原设计注释:"start 是 factory 层的事",但 factory 没接);DataClient 自管 + idempotent 更稳;⑤ `src/arbitrage/debug/execution_clients.py:SkipExecutionOrbitExchClient` 加 `_connect`/`_disconnect` skip 模式 no-op —— base `OrbitExchExecutionClient._connect = NotImplementedError("OE _connect: live 接线(browser/executor/general WS),/live-test 验")`,skip_execution=true 下不真出单 → no-op 让 NT ExecClient 状态正常 transition 成 Connected;非 skip 模式仍透传 NotImplementedError 等 slice 10b。**修后 5 项真通**:TradingNode kernel + 4 engines READY / `install_arbitrage_engines` 装 ArbitragePortfolio + `_KernelInjectedDebugEngine` Debug Risk 子类 / 4 factories 注册 / **4 Actors RUNNING**(2 InstrumentRefresher 各 venue distinct ComponentId + Matching + Strategy)/ **PM Data Connected**(Initializing instruments + 真连 CLOB WS)/ **PM Exec Connected**(Account POLYMARKET-001 registered in cache,balance 0.000000 USDC.e — 真账户);**OE Data Connected**(Playwright Chromium 真起 + 真 nav `customer/inplay/highlights` + OrbitExchWebSocketHandler 真起 + `pages=True`)/ **OE Exec Connected**(SkipExecution skip-mode no-op);Portfolio initialized 0 orders / 0 positions;**OE InstrumentRefresher refresh ok count=28**(Δ+28!)—— 真发现 Tennis × Men's Roland Garros 2026 共 **28 个 BettingInstrument**(9-14 场比赛 × home/draw/away),第二轮 refresh Δ+0 稳定;**PM InstrumentRefresher tick 触发** —— PM gamma 真 crawl 10000+ markets(Cursor 'MTAwMDA=' = 10000)。**5 处遗留 gap 留后续 slice**:**Gap A** `MarketMatchingActor` + `StrategyEvaluator` `Actor.subscribe_data` 失败 ERROR("`client_id` or `instrument_id` need to be specified" × 3,actor 仍 RUNNING)—— custom DataType(InstrumentsRefreshed / MatchedPair)NT subscribe_data API 需要额外 routing args,**slice 8A Actor 接线 bug**;routing 可能没真订上;**Gap B** PM `ArbPolymarketInstrumentProvider`("No loading configured: ensure either `load_all=True` or there are `load_ids`")+ 无 sport filter → InstrumentRefresher tick 触发后 PM gamma 全量 crawl 10000+ markets,慢且无意义;**Gap C** OE Exec `_connect` 非 skip 模式仍 NotImplementedError —— 真出单需 OE login/page/WS 一整套(slice 10b);**Gap D** PM `enrich_pm_six_key_info` 占位空字符串 → PM 端不参与 matching → matching 永不出 `MatchedPair` → mean_rebate 永远不触发(slice 7B);**Gap E** InstrumentRefresher `_on_alert → loop.create_task(_tick)` dispose 时未 await pending → "Task was destroyed but it is pending" warning(非 fatal,clean shutdown 缺陷)。**全程 graceful `node.dispose()`**(SIGTERM)。**估值**:**OE 端 connect+discovery 链路真通**(28 instruments 见证),PM 端 connect 真通,**mean_rebate 决策链路 + submitter + SkipExecution + Q11.A 全 wiring 就绪**等 Gap D 后真触发。**文档同步**:`_cross-cutting/configuration.md §10` slice 10c ✅ 详细 5 修 + 5 gap;`data/architecture.md §4 横切咬合` 加 #51 OE DataClient _connect 自管 BrowserManager 行;`_cross-cutting/debug-injection.md` 落地状态加 ✅ #51 SkipExecution OE _connect no-op;`tests/arbitrage/adapters/orbitexch/README.md` 加 "Slice 10c smoke 浮上 #51" 段(三件 OE 接线修);`tests/arbitrage/debug/README.md` 加 "Slice 10c smoke 浮上 #51" 段。**全 arb 套件 362 passed,0 regression**(slice 10c 没加新单测,纯 live 接线修;改的件都不在 unit test 覆盖路径上 — 接 NT runtime / Playwright)。**P11 判定**:OE BrowserManager 由 OE adapter 内 Data/Exec/Discovery scraper 三方共享,**属"组件内部共享件"**(OE adapter 封装),挂 OE adapter README + data/architecture.md 跨引用,**不需独立横切章节**。**推荐下步**:**slice 10d** 修 Gap A + E(NT custom data subscribe 路径正确 + InstrumentRefresher pending task clean shutdown)→ 之后再 smoke 验 `InstrumentsRefreshed` 事件是否真到 MarketMatchingActor;再 **slice 7B** PM enricher → 双边 matching → mean_rebate 真触发。`/live-test` 项目 skill 后续应扩支持新 launcher(slice 后做)。 |
| 2026-05-31 (#50) | **slice 10a 落地 — `EvalContext.submitter` + Action 真出单链路接通**(#41 设计 slice 10 之 a):用户选 slice 10a 推下一步(我推荐;原 plan 候选还有 7B PM enricher 真写,后做)。**问题背景**:Action 是 ABC,`execute(ctx)` 默认无法 submit_order(StrategyEvaluator 是 Actor 非 Strategy,无 `self.submit_order` facade);slice 9 PlaceBetsAction log-only(Q-D1=A)只验决策链路,真出单未通。**落地**(框架小改 + Action 双路径):① `condition.py:EvalContext` 加 `submitter: object | None = None`(运行时类型 `Callable[[dict], Awaitable[None]]`,避循环 import);② `actor.py` 新 module-level `make_submitter(*, cache, msgbus, clock, trader_id, log)` 工厂:返 `async def submit(spec)` callable,内部 `cache.instrument(iid).{size_precision,price_precision}` 拿精度 → 构 NT `LimitOrder`(`OrderSide.BUY/SELL` from spec / `Quantity` / `Price` / `TimeInForce.GTC` / 随机 `ClientOrderId(f"ARB-{UUID4()[:8]}")`)→ 包成 `SubmitOrder` cmd(`StrategyId("ARB-EVAL-001")` fixed literal,StrategyEvaluator 是 Actor 无独立 StrategyId)→ `msgbus.send("ExecEngine.execute", cmd)`;**冷启动安全**:`cache.instrument` 返 None → warning + skip 不 raise;③ `StrategyEvaluator._make_submitter()` thin wrapper delegate to module-level(便于单测 mock 各 dep,避 NT Actor base 依赖);④ `_evaluate_and_fire` 构造 ctx 时 `submitter=self._make_submitter()` 注入;⑤ `PlaceBetsAction.execute` **双路径**:`ctx.submitter` 非 None → `await submitter(spec)` 真出单(log `PlaceBets[submit]`)/ None → log-only fallback(log `PlaceBets[smoke]` + `would submit: ...`);**spec schema** `{instrument_id, side: "BUY"|"SELL", qty: float, price: float}`。**重构动机**:初版 `_make_submitter` 是 method,但 method 内访问 `self.cache` / `self.msgbus`,这些 NT Actor 经 register_base 才有,单测 mock 复杂 → 抽 module-level 函数 + thin method delegate,测 module 函数即可(`make_submitter(cache=Mock, msgbus=Mock, ...)`)。**配合 Q11.3 SkipExecutionClient**:`debug.skip_execution=true` 时 SkipExecutionClient 拦 ExecClient `_submit_order` mock 全成 — **Action→submitter→真 SubmitOrder→SkipExecution 兜底**完整链路安全可跑(不上链)。**测试**(+6):`test_submitter.py`(3:build LimitOrder + send SubmitOrder cmd 到 "ExecEngine.execute" 端点 / SELL 侧 / cache.instrument None skip 不 raise — 用 InstrumentId+Symbol+Venue + size_precision=3 避 5.625 精度被 round 到 5.63);`test_action_place_bets.py` +2(submitter 注入 2 leg spec 正确 + log "[submit]" 无 "would submit" / submitter=None 显式 fallback 不 raise);`test_evaluator.py` +1(Action 拿到的 ctx.submitter 是 callable not None,经 _harness + _RecordingAction 捕 ctx)。**全 arb 套件 362 passed(+6,0 regression)** / 33 skipped。**P11 判定**:submitter 单一自然归属(strategy 域→ExecEngine 单向 send,生产者-消费者非对称),挂 `strategy/architecture.md §3.9`(新增段)+ 不需横切章。**文档同步**:`strategy/architecture.md §3.9` 新增 "slice 10a 落地" 段(submitter 接口 + make_submitter 流程 + PlaceBetsAction 双路径 + SkipExecutionClient 配合);`_cross-cutting/configuration.md §10` slice 10a ✅;`tests/arbitrage/strategy/README.md` 加 slice 10a 段。**Q21 设计取舍**:**StrategyEvaluator 没改为 Strategy 子类**(NT Strategy 有 submit_order facade 但是为单一策略设计;Q21 evaluator 跑多 strategy,语义不重合 → 保持 Actor + submitter callable 注入更干净)。**仍待 #41 slice**:7B/C(PM enricher 真写 → 让真 matching 跑;之前 user 说"先放放,我们先把套利逻辑跑通,测试时撤未成交订单也顺便测撤单")/ 8B(Settlement)/ 10c(跑真 launcher /live-test;现已可,user 触发)。 |
| 2026-05-31 (#49) | **slice 9 落地 — `mean_rebate` 测试策略 + 框架小改(per-pair 隔离 / per-eval scratch / snapshot 加 in_play)**(#41 设计 slice 9/10 之 9):用户决定先落"mean_rebate 简单策略"打通测试链路。**关键设计对话**:用户先建议 SignalStore 双状态存 in_play(我前面 Q-D2),audit 后发现**SignalStore 是 evaluator 单例,跨 pair 共享会乱**(pair A 写 pre_match 污染 pair B);用户进一步反问"既然每次赔率都带 inplay,无非多一套赋值操作,也没必要持久态存储",再深入到**PM-only 赔率事件触发评估时无 inplay 字段的问题**。最终落地:**inplay 不进 SignalStore,走 instrument.info(cache-resident mutable)+ snapshot 派生**;持久态/瞬态机制保留供未来真持久信号用(如 user_pause 等外部输入)。**关键工程对话**:Action 怎么 submit?**Q-D1 先 A(log-only smoke)后 B(`EvalContext.submitter` 接 SkipExecutionClient)**;legs 传递方式?**Y `EvalContext.scratch: dict`**(per-eval 自动隔离);per-pair 持久态隔离?**P3 `SignalStore.view(pair_id)` 子视图**(保留供未来用,本 slice 不动用);pre_match/live 信号建模?**(ii) 单 in_play key + BoolExpr NOT 表达式**(但本 slice 实际改走 PreMatchCheck 在 checktion 列表里短路 AND,因 inplay 不进 SignalStore)。**落地清单**(12 件):① **utils** `polymarket_price_to_probability(price)` + `orbitexch_odds_to_probability(odds)`(数值处理段,clamp / 防脏数据)+ utils_api.md 同步;② **`EvalContext.scratch: dict = field(default_factory=dict)`** per-eval Check→Action 传值;③ **`OpportunitySnapshot.in_play: bool`** + **`OpportunitySnapshot.instrument_info: dict`**(浅拷贝冻 cache.instrument.info 给 Check decouple from cache);④ **`build_snapshot` 派生** in_play + instrument_info(一次遍历两件);⑤ **`SignalStore.view(pair_id) → _PairScopedStoreView`**(thin wrapper,内部 key `f"{key}.{pair_id}"`,P3 per-pair 隔离);⑥ **`StrategyEvaluator._evaluate_and_fire`** 构造 ctx 时 `store=self._signal_store.view(pair_id)`(scratch 由 default_factory 自动);⑦ **OE `data.py`** `_on_price_frame` 透 `marketDefinition.inPlay` 调 module 级 helper `write_inplay_to_instrument_info(cache, iid, in_play)`(从 `_on_price_frame` 提出方便单测,因 NT `_cache` cdef readonly Mock 困难);⑧ **`PreMatchCheck()`**(`src/arbitrage/strategy/checks/pre_match.py`,无参,读 `snapshot.in_play` 取反);⑨ **`MeanRebateCheck(min_rate)`**(`src/arbitrage/strategy/checks/mean_rebate.py`):按 `snapshot.instrument_info[iid].selection_role` 分组 / 经 `snapshot.order_books[iid].best_ask_price()` 取价 + `_to_prob(venue, price)` 转概率 / 每方向取 min(prob)PM 优先 tiebreak / sum → `rate = 1 - sum` / `>= min_rate` 写 `ctx.scratch["legs"]` + return True;**支持 2-way / 3-way**(roles 必须是 `["away","home"]` 或 `["away","draw","home"]`);⑩ **`PlaceBetsAction(share)`**(`src/arbitrage/strategy/actions/place_bets.py`):consume `ctx.scratch["legs"]` / `_compute_size(venue, share, price)` PM=share / OE=share/price / **log-only smoke**(`_LOG.info` 每 leg "would submit");⑪ **launcher** `register_builtin_checks_and_actions()` 注 3 类,`main` 顶部调一次;⑫ **`arb_config.example.json`** strategy 段:checktion 短路 AND `[pre_match, mean_rebate]` + action place_bets share=22.5 + binding `competition:ATP → mean_rebate`。**重要修正**:`MeanRebateCheck` 初版试图 `getattr(ctx, "cache")` fallback 不干净 → 改 snapshot 加 `instrument_info` 冻结副本,Check decouple from cache(slice 5 #44 设计精神延续:Check 是纯函数,只读 snapshot)。**`_FakeCache` test fixture 扩**:`set_instrument(iid, info)` + `instrument(iid)`,5 个 evaluator 测试改用 `store.view(pair_id).set_persistent(...)` symmetric with view idiom。**测试**(+32 净增):utils prob 7 / snapshot in_play 派生 3 + 现有 5 改 view / SignalStore view 6 / pre_match 3 / mean_rebate 4 / place_bets 5 / OE inplay helper 4。**全 arb 套件 353 passed(+32,0 regression)** / 33 skipped。**冷启动假阳性不存在**(用户洞察):OE 赔率是 MeanRebateCheck 的硬前置(没 OE best_ask 算不出 prob),OE 赔率帧本身就携带 inplay → 赔率到 = in_play 到。**P11 判定**:slice 9 三件(scratch / view / in_play 派生)各有单一自然归属(scratch+view 在 strategy 域内 Check→Action 传值机制;in_play 写入是 data→strategy 生产者-消费者非对称),挂主方足够,**不需横切章**;均挂 `strategy/architecture.md §3.8` + `data/architecture.md §4` 跨引用。**文档同步**:`strategy/architecture.md §3.8` 新增 ~50 行 "slice 9 落地" 段(三件隔离机制 + snapshot 新字段 + view + scratch + 3 个用户域 Check/Action + OE 透 inplay + 冷启动论证);`data/architecture.md §4` 加 #49 行;`utils_api.md` 加 PM/OE prob 函数段;`tests/arbitrage/strategy/README.md` 加 slice 9 段(框架改 + 用户域子类 + OE inplay);`tests/arbitrage/adapters/orbitexch/README.md` 加 `test_data_client_inplay_writeback.py` 行;`_cross-cutting/configuration.md §10` slice 9 ✅(超出原计划 4 件补充);**`arb_config.example.json`** 全段更新 example 配置。**仍待 #41 slice**:7B/C(PM enricher 真写 + PM filter,enables 真 matching)/ 8B(Settlement + positions_fetcher)/ 10 /live-test smoke。**Q-D1=B 真下单(`EvalContext.submitter`)留 slice 10 一起做**。 |
| 2026-05-30 (#48) | **slice 8A 修正 — Q19 `is_execution_active` 真接(撤"TODO"占位)**:**用户指正**:"merge 和 redeem 不是放在健康检查中吗,健康检查不是和 execution 有同步机制吗?" — 一语戳穿我前一 slice 的盲点。**事实**:Q19 机制早就存在(`_cross-cutting/synchronization.md` + `src/arbitrage/execution/session.py:58` `ArbExecutionSessionMixin._execution_active` ref-count `len(_active_sessions) > 0` + `health_check.py:77` 已用同一 callable 跳过 tick);**StrategyEvaluator 的 `is_execution_active` 跟健康检查的 `is_execution_active` 本就是同一语义**(查 PM/OE 哪边有 session 在飞)。我在 slice 8A 落 `lambda: False` 写 "TODO slice 8B/9" 是漏看了既有机制,不是新需求。**修正落地**:① `launchers/arb_node.py` 加 `_make_is_execution_active(node)` 工厂:遍历 `node.kernel.exec_engine._clients.values()`,`getattr(client, "_execution_active", False)` 兜底(无 mixin 的 client 默认 False,不 raise);任一 True → 聚合返 True;② `add_actors` 内 StrategyEvaluator deps 改用 `is_execution_active=_make_is_execution_active(node)`(撤 `lambda: False`),docstring 移除 TODO 标注、加 Q19 桥接说明。**测试**(+4):无 session 在飞 → False / 任一 client True → 聚合 True 且 callable 反映最新切换 / 无 `_execution_active` 属性的 client `getattr` 默认 False 不 raise / `add_actors` 装 StrategyEvaluator 时 `_is_execution_active is _make_is_execution_active(node)` 返值(不是 stub)。**全 arb 套件 321 passed(+4,0 regression)** / 33 skipped。**文档**:`_cross-cutting/configuration.md §10` slice 8A 行加 #48 修正说明 + slice 8B 移除 `is_execution_active`(只剩 PolymarketSettlement / positions_fetcher);`tests/arbitrage/launchers/README.md` 加 "Slice 8A 修正 #48" 段。**P11 判定**:Q19 机制本身已在 `_cross-cutting/synchronization.md` 单独成横切章(execution session / healthcheck / StrategyEvaluator 三方对等消费同一不变量),保持不动;本 slice 只补 launcher 端**接线**(把现有机制的抽象 callable 桥到具体 exec client),无新机制,不动横切归类。**经验教训(写入)**:做 launcher 接线前 grep 现有 callable / property 的真实形态,不是看到接口形态相同就当成新 TODO 写 stub;`_cross-cutting/*` 章不仅给真理源也给"这件事我做没做过"的索引。 |
| 2026-05-29 (#47) | **slice 8A 落地 — Actors 接线(4 个 Actor + Provider 共享机制)**(#41 设计 slice 8/8 之 A;Settlement 留 8B):**关键约束发现**:`InstrumentRefresher._RuntimeDeps.provider` 必须跟 DataClient 用**同一实例**(否则 add 的 instrument 双方各持一份,cache 视图分裂);data factory 在 `node.build()` 期间构造 Provider → 必须**回写 ArbContext** 让 launcher post-build 拿到。**落地**:① `src/arbitrage/bootstrap.py:ArbContext` 加 `pm_instrument_provider: object | None = None` + `oe_instrument_provider: object | None = None`(运行时类型避循环 import);② `nautilus_trader/adapters/polymarket/arb_factories.py:ArbPolymarketLiveDataClientFactory.create` 构造 `ArbPolymarketInstrumentProvider` 后 `ctx.pm_instrument_provider = provider` 回写;③ `nautilus_trader/adapters/orbitexch/factories.py:OrbitExchLiveDataClientFactory.create` 构造 `OrbitExchInstrumentProvider`(slice 7A 真接 scraper)或占位 `InstrumentProvider()` 后 `ctx.oe_instrument_provider = provider` 回写;④ `launchers/arb_node.py` 新函数 `add_actors(node, cfg, *, pair_registry)`(**必须 node.build 后调用**):`asyncio.get_event_loop()` 拿 loop;`to_instrument_refresher_configs(cfg) → (pm_ref_cfg, oe_ref_cfg)` 配对;`ctx.pm_instrument_provider is not None` → 装 `InstrumentRefresher(config=pm_ref_cfg, deps=RefresherDeps(provider=ctx.pm_instrument_provider, loop=loop))`(OE 同构,跳过 None);**MarketMatchingActor**(`deps=MatchingDeps(pair_registry=pair_registry)`);**StrategyEvaluator**(`deps=StrategyDeps(pair_registry, strategy_registry=to_strategy_registry(cfg), portfolio=node.kernel.portfolio, signal_store=SignalStore(), is_execution_active=lambda: False, loop=loop, signal_collector=None)`);全部 `node.trader.add_actor(actor)`。⑤ `bootstrap_and_build` 在 `wire_arbitrage_runtime` 之后调 `add_actors`(顺序:install → TradingNode → prepare_context → factories → build → wire → **add_actors** → return)。**TODO 残留**:`is_execution_active = lambda: False` 永不阻塞,slice 8B/9 真接(查 leg_settled / cache.orders_open()),pre-check 跳过在飞订单(Q19);`signal_collector=None`(slice 9 用户域)。**Settlement / positions_fetcher 留 slice 8B**(用户决定推迟,需 PolymarketContractService + private_key + RPC 大件;skip_execution smoke 不需要)。**测试**(+6 net):`test_arb_node.py` 加 6 个:both providers → 4 actors / PM 缺 → 3 actors(跳 PM Refresher)/ 两缺 → 2 actors / StrategyEvaluator `_portfolio is node.kernel.portfolio`(sentinel verified)/ PM Refresher `_provider is ctx.pm_instrument_provider`(同一实例)/ `bootstrap_and_build` 调 `add_actors`(patch.object 隔离)。`asyncio.get_event_loop` monkeypatch 成 MagicMock fake_loop 避免 deprecation warning。**全 arb 套件 317 passed(+6,0 regression)** / 33 skipped。**文档同步**:`discovery/architecture.md §3.3` 加 "slice 8A Provider 共享机制" 段(P11 判据:生产者-消费者非对称,挂主方 InstrumentRefresher 子节,不需横切章);`tests/arbitrage/discovery/README.md` 加 discovery-2.A.1 用例(Provider 共享验收);`tests/arbitrage/launchers/README.md` slice 6 待办勾 7A/8A ✅ + 加 slice 8A 落地段;`_cross-cutting/configuration.md §10` slice 8A ✅ + slice 8B 待办标记。**仍待 #41 slice**:8B(Settlement + is_execution_active 真接)/ 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-29 (#46) | **slice 7A 落地 — OE data factory 真接 scraper + Provider aliases 注入**(#41 设计 slice 7/8 之 A;7B/7C 留):**audit 发现**:① OE factory 装 `InstrumentProvider()` 占位,scraper 完全不接;② OE `OrbitExchInstrumentProvider._build_legs` 写 `info["sport"]/["competition"]` 不查 aliases,跟 normalizer 注释"Provider 填 info 时已 alias"假设脱节;③ PM `ArbPolymarketInstrumentProvider.enrich_pm_six_key_info` 是占位(sport/competition/home/away/start_ts/selection_role 大都 `""`)→ **PM 端目前不参与 matching**;④ PM 侧 sport 过滤需查上游 filter 语义。**slice 7A 范围决策**:做 ① + Provider aliases 注入;**不做** PM enricher 实写 + PM filter(独立大件,留 7B/7C 或 slice 9 用户落策略时一并)。**架构 known divergence**:`OrbitExchScraper` **自管 Playwright lifecycle**(独立 browser 进程,无登录共享),与 OE Data/Exec 走 `BrowserManager.get_page("data"/"exec")`(Q2 / §6.2)的共享模型不一致。**判定 OK**:Q2 原意只覆盖 Data+Exec,discovery 是第三方;OE 看公开 competition list 不需登录,unauthenticated browser 够用。若后续 discovery 需登录态(私有赛事 / 用户偏好),拆 slice 7C refactor scraper 接 `BrowserManager.get_page("discovery")`。**落地**:① `src/arbitrage/bootstrap.py:ArbContext` 加 `oe_scraper_config: object | None`(运行时类型避循环 import)+ `oe_sport_aliases: dict` + `oe_competition_aliases: dict`(`field(default_factory=dict)`);② `src/arbitrage/config/dispatcher.py:to_oe_scraper_config(cfg) → OrbitExchVenueConfig | None`(`cfg.discovery.orbitexch.enabled=False` → None;否则按 `OrbitExchVenueConfig(enabled, browser=BrowserConfig(headless, user_data_dir, timeout_ms=page_load_timeout_sec*1000), sports=[SportConfig(...)])` 装配);`to_arb_context_init_kwargs(cfg)` 扩展返 `oe_scraper_config` + `oe_sport_aliases` + `oe_competition_aliases`(`dict(cfg.matching.sport_aliases)` 拷贝 frozen msgspec 解结构);③ `nautilus_trader/adapters/orbitexch/providers.py:OrbitExchInstrumentProvider.__init__` 加 kw-only `sport_aliases` / `competition_aliases`(默认 None → 空 dict),`_build_legs` 写 `info["sport"]=self._sport_aliases.get(event.sport, event.sport)`(competition 同理);④ `nautilus_trader/adapters/orbitexch/factories.py:OrbitExchLiveDataClientFactory.create` 分支:`ctx.oe_scraper_config is None` → 装 `InstrumentProvider()` 占位(`enabled=False` 路径);else `OrbitExchScraper(config=ctx.oe_scraper_config)` + `OrbitExchInstrumentProvider(scraper=, sport_aliases=, competition_aliases=)`;⑤ `launchers/arb_node.py` 无逻辑改动(经 `**to_arb_context_init_kwargs(cfg)` 自动解包新字段),只加注释标 slice 7A。**测试**(+9 net):`test_orbitexch_provider.py` +4(sport alias 命中 / competition alias 命中 / alias miss 透传 / 默认 None 透传)+ `test_dispatcher.py` +3(scraper_config 含 sports + browser timeout_ms 单位换算 / disabled → None / aliases 经 init_kwargs 透传)+ 新 `tests/arbitrage/adapters/orbitexch/test_data_factory_provider_wiring.py` +2(无 scraper_config → 装非 OrbitExchInstrumentProvider 占位 / 有 → scraper_class 调 once + Provider kwargs 含 aliases)。**全 arb 套件 311 passed(+9,0 regression)** / 33 skipped。**文档同步**:`discovery/architecture.md §3.2` Provider 构造签名 + 6-key 映射表 sport/competition 行加 aliases 规范化说明 + #46 落实"normalizer Provider 填 info 时已 alias"假设;`tests/arbitrage/discovery/README.md` 加 discovery-1.4.g/h 用例;`tests/arbitrage/adapters/orbitexch/README.md` 新增 `test_data_factory_provider_wiring.py` 行 + scraper 浏览器自管 known divergence 段;`_cross-cutting/configuration.md §10` slice 7A ✅。**仍待 #41 slice**:7B/C(PM enricher 真写 + PM filter)/ 8 Actors 接线 + Settlement / 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-28 (#45) | **slice 6 落地 — launcher 骨架(`launchers/arb_node.py`)**(#41 设计 slice 6/8):**落地**:① 新建 `launchers/` 顶层目录;`launchers/arb_node.py`(~120 行)5 个函数 + main CLI:`build_trading_node_config(cfg)` 纯函数 ArbConfig → `TradingNodeConfig`(PM+OE × data+exec 4 client config + TraderId("ARBITRAGE-001") + LoggingConfig INFO + LiveExecEngineConfig reconciliation=False + 默认 timeouts 20/10/1);`prepare_runtime_state(cfg) → (LegSettledRegistry, PairRegistry, DebugConfig | None)`(进程级单例,经 ArbContext 传给 factory + 后续 Actors);`register_factories(node)` 注 4 个(PM `Arb*Live{Data,Exec}ClientFactory` + OE `OrbitExchLiveDataClientFactory` / `ArbOrbitExchLiveExecClientFactory`);`bootstrap_and_build(cfg, node_factory=TradingNode) → (node, leg, pair)` 主 orchestrator:**严格顺序 install_arbitrage_engines(debug_config=) → TradingNode(config) → prepare_arb_context(leg/pair/debug/timeouts/settlement=None/fetcher=None) → register_factories → node.build() → wire_arbitrage_runtime(params=, leg_settled=)**;`main(argv) → int` CLI:argparse `--config` path → load_arb_config → bootstrap_and_build → `try: node.run() finally: node.dispose()`。**节制范围**:slice 6 不接 Actors(InstrumentRefresher × 2 + MarketMatchingActor + StrategyEvaluator,留 slice 8);`pm_settlement` / `pm_positions_fetcher` 暂 `None`(PolymarketSettlement 需 contract_service+RPC 太重,留 slice 8);默认空 StrategyRegistry(launcher 仍能起 connect/discovery/matching,StrategyEvaluator no-op evaluate)。② `node_factory` 参数注入便于 test mock。**测试**(11):build_trading_node_config 字段映射(含 trader_id "ARBITRAGE-001" 校验)+ 凭证 from cfg;prepare_runtime_state(无 debug→None / enabled→DebugConfig);register_factories 4 次调用 + venue 字符串校验;bootstrap_and_build 完整调用链(install + factories + build + wire,patch.object 隔离 install_engines/wire);ArbContext 填全(leg_settled/pair_registry/timeouts/settlement=None/fetcher=None);install 传 enabled DebugConfig vs None;main CLI(正常路径 + 异常路径仍 dispose)。**坑 + fix**:`tests/arbitrage/launchers/__init__.py` 让 pytest 把测试目录当 package "launchers" → 与顶层 `launchers/` 冲突 import `from launchers import arb_node` 失败 → **删 `tests/arbitrage/launchers/__init__.py`**(pytest 不要求 test dir 是 package)。**全 arb 套件 302 passed(+11,0 regression)** / 33 skipped。**文档同步**:`_cross-cutting/configuration.md §10` 落地清单勾选 slice 2-6 ✅ + 各 slice 测试数标注;新建 `tests/arbitrage/launchers/README.md`。**仍待 #41 slice**:7 OE data factory 真接 scraper + aliases→Provider / 8 Actors 接线 + Settlement / 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-28 (#44) | **slice 5 落地 — Strategy JSON parser + Check/Action registry**(#41 设计 slice 5/8):**落地**:① `src/arbitrage/strategy/check_action_registry.py`(45 行):两个 module-level dict(`_CHECK_REGISTRY` / `_ACTION_REGISTRY`)+ `register_check/_action(name, cls)`(同名同类幂等,同名异类 raise)+ `build_check/_action(spec) → cls(**params)`(未知 type / 缺 type → `StrategyConfigError`);**框架不预注册任何具体类**(#41 撤回旧策略平移),用户在 launcher main 顶部装入;`_reset_for_tests()` 测试隔离。② `src/arbitrage/strategy/json_loader.py`(120 行)3 个递归 + 1 个 registry builder:`bool_expr_from_json(spec)` 5 形态(`{"signal": str}` / `{"AND": [...]}` / `{"OR": [...]}` / `{"NOT": <sub>}` / None→`AndExpr()` 真值兜底)+ 多 key/未知 key/non-str signal raise;`condition_from_json(spec)` 递归 sub_conditions + checktion `build_check` + action `build_action`,**`self_hits` 缺/None → `AndExpr()`**(空 AND vacuous truth,Q21 必填字段兜底);`strategy_from_json(id, spec, scope_key)` 接受 dict 或 `StrategyJsonConfig` msgspec,**`compensation_tree` 缺 → `_NOOP_COMPENSATION_TREE = Condition(self_hits=OrExpr())`**(空 OR 永 False,Q21 必填字段兜底),metadata 含 id+description;**arbitrage_tree 缺 → raise**(强制必填);`build_strategy_registry(strategy_section)` 解析 bindings: `pair:<id>` (别名 `pair_id:<id>`) / `competition:<name>` / `sport:<name>` 三 kind 分派到 `register_*`,unknown strategy_id / 错 kind / 错格式全 raise。③ `config/dispatcher.py` 增 `to_strategy_registry(cfg)`(纯包装 `build_strategy_registry(cfg.strategy)`,空 bindings → 空 registry,launcher 仍能起 connect/discovery/matching)。**测试**:`test_check_action_registry.py`(8) + `test_json_loader.py`(26) 共 34;BoolExpr 嵌套(`AND([live, OR([home, NOT draw])])` 三层)+ Condition self_hits 短路 + checktion 短路 + 递归 sub_conditions 互斥(命中即停)+ Strategy 缺 compensation 永 False + StrategyRegistry 挂载锁定(pair > comp > sport 不降级)。**全 arb 套件 291 passed(+34,0 regression)** / 33 skipped。**文档同步**:`strategy/architecture.md §3.7` 新增"Check/Action 类型注册 + JSON loader" 节,标注 #44 + 各 API 签名 + 缺值兜底规则 + scope 格式;strategy/config README 各加 slice 5 状态。**仍待 #41 slice**:6 launcher 骨架 / 7 OE factory 真接 scraper + aliases→Provider / 8 Actors 接线 / 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-28 (#43) | **slice 4 落地 — ArbConfig dispatcher 纯函数**(#41 设计的代码 slice 4/8):`src/arbitrage/config/dispatcher.py` 11 个 `to_*` 纯函数:① 4 个 ClientConfig(PM/OE × Data/Exec)凭证字段→NT-native config 映射;PM 凭证 None passthrough、**OE Config 要求 `username/password: str` 非 Optional → dispatcher 回退空串**(让下游 BrowserManager/login 触发明确错误,loader 不预判);② `to_instrument_refresher_configs(cfg) → (PM, OE)` 共享 `cfg.discovery.refresh_interval_secs`;③ `to_market_matching_actor_config`(注意 **`sport_aliases`/`competition_aliases` 不在 MarketMatchingConfig** — normalizer 注释"Provider 填 info 时已 alias",aliases→Provider 接线留 slice 7;空 dict→None 兜底);④ `to_strategy_evaluator_config`(只 `log_evaluations`,其余 deps launcher 装);⑤ `to_arb_risk_params` ArbRiskParams 字段映射;⑥ `to_arb_context_init_kwargs(cfg) → dict`(execution.tracking_timeout_sec/health_check_interval_sec → PM+OE session/health 四字段;`leg_settled/pair_registry/pm_settlement/pm_positions_fetcher` 由 launcher 运行时补);⑦ `to_debug_config` — debug 段缺 / enabled=False 返 None;enabled 时构造 DebugConfig 并展开 overrides + mock_data(category 字符串→MockCategory enum)。**测试**(17:PM/OE × Data/Exec config 凭证映射 / PM 凭证 None / OE 空串 fallback / IR pair venue+interval / MatchingActor min_similarity+competition_max_matches+空 dict→None / StrategyEvaluator 默认 / ArbRiskParams 全字段 / ArbContext init kwargs / Debug None when missing+disabled / Debug enabled overrides+mock_data / mock conditions 不匹配返 None / 纯函数不 mutate cfg)。**全 arb 套件 257 passed(+17,0 regression)** / 33 skipped。**仍待 #41 slice**:5 Strategy JSON parser / 6 launcher / 7 OE factory 真接 scraper + aliases→Provider / 8 Actors 接线 / 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-28 (#42) | **slice 3 落地 — ArbConfig schema + JSON loader + env 凭证注入**(#41 设计的代码 slice 1/8):**落地**:① `src/arbitrage/config/schema.py`(11 个 msgspec Struct,frozen+kw_only;list/dict 默认走 `msgspec.field(default_factory=...)` 避共享对象;顶层 `ArbConfig` 7 段 + 凭证字段全 `str | None = None`);② `src/arbitrage/config/loader.py`:`load_arb_config(path)` 顺序 = JSON 解析 → `_warn_credentials_in_json`(JSON 含凭证字段非空发 `ConfigWarning`)→ `_inject_env_credentials`(就地覆盖 `raw["venues"][...]`,env 缺失不覆盖;`POLYMARKET_USER_ADDRESS` fallback `POLYMARKET_ADDRESS`)→ `msgspec.convert` 校验+冻结;`ConfigError` 包 `FileNotFoundError` / `json.JSONDecodeError` / `msgspec.ValidationError`;**env 缺失不 raise**(交给下游 client 构造);③ `src/arbitrage/config/__init__.py` 延迟解析 `load_arb_config` 避循环 import。**测试**(15:default empty / full json / PM env 注入 7 字段 / OE env 注入 / env 缺失保 None / `POLYMARKET_ADDRESS` 别名 fallback / 主 env 胜 alias / env 胜 JSON / 凭证-JSON ConfigWarning / 干净 JSON 无 warning / 文件不存在 / 无效 JSON / schema 不匹配 / `venues` 段缺 setdefault 兜底 / root 非 object)+ test fixtures `_clean_env` autouse 防宿主机 `.env` 污染。**全 arb 套件 240 passed(+15,0 regression)** / 33 skipped。**仍待 #41 slice**:4 dispatcher / 5 Strategy JSON parser / 6 launcher / 7 OE factory 真接 scraper / 8 Actors 接线 / 9 用户落测试 Strategy / 10 /live-test smoke。 |
| 2026-05-28 (#41) | **配置面架构锁定(Q22-Q27)+ 凭证泄漏发现 / .gitignore 收紧 + configuration 横切详设 + 修订 launcher 路径**:用户问"launcher 全链路接线 → /live-test 能做吗?"我先架空答"web_gate 是 UI polish 可后做",用户纠"discovery 配置需要提前配置,你不走市场发现/匹配怎么跑实盘?"我撤回并 audit,发现真实差距 6 条:① OE InstrumentProvider 工厂未真接 scraper(占位 `InstrumentProvider()`);② OE/PM scraper 抓取目标无注入;③ InstrumentRefresher / MatchingActor 未 add_actor;④ MatchingActor aliases / max_matches 无来源;⑤ 配置面整体缺(旧 `default_config.json` 7 个 section 集中存,无 NT-native loader);⑥ 具体 Strategy 实例是用户域。**根因 = #5 配置面**(其余 4 个皆其衍生)→ 决定先做 web_gateway 配置面(无 UI),launcher 后做。**审计触发严重安全发现**:`src/arbitrage/services/web_gateway/default_config.json` 含 PM `clob_api_key` / `clob_api_secret` / `clob_passphrase` + OE `username` / `password` + PM `private_key` 字段(空但暴露架构)等明文凭证;**git ls-files 确认文件已 tracked,git log -S 确认凭证字符串永久在 git 历史**(`4018e8796a refactor(adapters)...` 等多次 commit)— `.gitignore` 对已跟踪文件无效。**slice 1 安全收尾**:`.gitignore` 加凭证保护段(`.env` / `.env.*` / `.credentials.json` / `*.credentials.json` / `settings.local.json` / `src/arbitrage/services/web_gateway/default_config.json`);旧 state.py line 408-454 已有 env override 机制,沿用其变量名(`ORBITEXCH_USERNAME` / `_PASSWORD` / `POLYMARKET_CLOB_API_KEY` / `_SECRET`(注意旧码用 `_SECRET` 非 `_API_SECRET`)/ `_PASSPHRASE` / `_PRIVATE_KEY` / `FUNDER` / `USER_ADDRESS`(or alias `POLYMARKET_ADDRESS`)/ `EOA_ADDRESS` / 可选 builder `POLYMARKET_API_KEY`/`_SECRET`/`_PASSPHRASE`),用户的 `.env` 不用改。**用户操作待办**:① 轮换 PM/OE 凭证(永久泄漏挽救:这步不做新设计也救不回历史);② `git rm --cached default_config.json` + commit 停止跟踪(destructive,用户做)。**Q22-Q27 锁定**(用户拍板,Q25 反我推荐):**Q22 ✅ B 分组件**(顶层 `ArbConfig` 组合 `discovery/matching/venues/strategy/risk/execution/debug`,跟 architecture 分件保持 P8 一致);**Q23 ✅ C env 优先 + 文件 fallback**(沿用旧码模式);**Q24 ✅ JSON**(沿用旧 schema 字段名,降迁移成本,msgspec 原生);**Q25 ❌ 改 A 配置驱动**(用户要 `"strategy_1": {"condition": {...}, "sub_conditions": [...], "checktion": [...], "action": ...}` 嵌套递归 JSON,直接对应 Q21 框架的 `Condition` / `BoolExpr` 树 — 这是 framework 已支持的递归结构,但 **JSON → object loader 是新工作**;**且依赖 Check/Action 具体实现已落** — Q21 框架只有 ABC,旧 `services/strategy/signals/` 的 rebate/pre-match/live/mean_rebate/multi-way 没平移到 Q21 接口);**Q26 ✅ A `launchers/arb_node.py`**(NT 惯例,跟 `examples/live/` 风格一致);**Q27 ✅ A 1:1 兼容旧 JSON 字段名**(降迁移成本)。**修订计划**:旧策略不平移(用户后落一个测试 Strategy),Slice 5 框架层只做 JSON parser + Check/Action 空 registry + `register_check`/`register_action` API(用户自己 register);launcher slice 6 默认空 registry 仍能起 connect/discovery/matching 链路(/live-test 部分 smoke)。**slice 2 落地**:**新建 `docs/arbitrage/architectures/_cross-cutting/configuration.md`**(配置面无单一自然归属,7 个组件全消费,按 P11 横切判据成横切章);9 节:总览数据流 + ArbConfig schema(msgspec)+ JSON 用户视角 + env 变量约定 + Loader 接口 + Dispatcher 接口(纯函数 `to_*_config(cfg)`)+ Strategy JSON parser(`bool_expr_from_json` / `condition_from_json` 递归 / `Check`-`Action` registry + `build_check`/`build_action` / `strategy_from_json`)+ 与各组件咬合表 + 凭证安全段 + 落地清单 + P7 不做(热重载 / Web UI / TOML / 版本号 migration)。**slice 3-10 计划**:3 schema+loader,4 dispatcher,5 Strategy JSON parser,6 launcher 骨架,7 OE data factory 真接 scraper,8 Actors 接线,9 用户落测试 Strategy,10 /live-test smoke(skip_execution=true)。**安全提醒永久写入新 design 第 9 节**:凭证只走 env,不进任何 commit-able 文件;loader 检测 JSON 中含凭证字段时发 ConfigWarning。 |
| 2026-05-26 (#40) | **Q11.3 SkipExecution{PM,OE}Client 落地(跳真执行 + mock 全成交)+ 顺补 `arb_factories.py` latent NameError**:Q11 框架最后一块。**落地**:① `src/arbitrage/debug/execution_clients.py`:`_mock_fill(client, command, quote_currency)` 共享纯函数(`generate_order_accepted` venue_order_id=`MOCK-{cid}` → `generate_order_filled` last_qty=order.quantity / last_px=order.price 或 `0.5` 兜底 / commission=0 / liquidity=TAKER / trade_id=`MOCK-{cid}-1`);② `SkipExecutionPolymarketClient(ArbPolymarketExecutionClient)` + `SkipExecutionOrbitExchClient(OrbitExchExecutionClient)`,各自 `_submit_order` 检查 `is_override_active("skip_execution")` —— 真则 `_mock_fill(self, command, USDC_POS/GBP)` 短路(跳 `_begin_session` + 真 venue 上送),假则透传 super;③ PM `arb_factories.ArbPolymarketLiveExecClientFactory` + OE `factories.ArbOrbitExchLiveExecClientFactory` 读 `ArbContext.debug_config` 分支,`enabled` → 装 Skip 子类传 `debug=cfg`,否则装生产(重构 `common_kwargs = dict(...)` 共享构造参数避免重复)。**当前是"立即全成"**,不实现 Q11.4 时序(timeline.py 仅在真需要部分填 / 拒单 / 撤单 lifecycle 模拟时才做)。**测试**:`test_debug_execution_clients.py`(8:`_mock_fill` 顺序 + PM USDC commission=0 + OE GBP + market 兜底 0.5 + limit 用 order.price;`_submit_order` 三分支 — skip 未激活走 super / skip 激活短路 / debug.enabled=False 走 super)+ `test_debug_exec_factories.py`(4:PM/OE × 无 debug / debug enabled 装 Skip + 传 debug=cfg,monkeypatch 替 NT 客户端类 + heavy deps 为 sentinel)。**全 arb 套件 225 passed(+12 Q11.3,0 regression)** / 33 skipped。**顺补 latent bug**:`nautilus_trader/adapters/polymarket/arb_factories.py` 引用 `get_polymarket_instrument_provider` 但**未 import**(Step 6 PM exec factory 未真接 live,NameError 没暴露);本回合写 exec factory 测试时 monkeypatch 报 AttributeError 抓出,补 `from nautilus_trader.adapters.polymarket.factories import get_polymarket_instrument_provider`。**文档**:`_cross-cutting/debug-injection.md` 落地状态加 ✅ #40 + Q11.3 行为详述;`execution/architecture.md §5 横切咬合` Q11.3 行从⬜→✅ + 详述`(不再"待"`;debug README 加 39 passed 累计 + #40 详述 + 顺补 bug 注;`timeline.py` 唯一仍待项。**Q11 Debug 框架四件落齐**:`DebugConfig` ✅ #38 + `DebugArbitrageLiveRiskEngine.skip_check_size` ✅ #38 + `Debug{PM,OE}DataClient` ✅ #39 + `SkipExecution{PM,OE}Client` ✅ #40;`DebugArbitrageStrategy` ❌ #39 撤(配置 vs 配置代替);`timeline.py` ⬜ 按需。**下一站候选**:launcher 全链路接线(把已落各件接起来跑 /live-test)/ web_gateway + 旧 services 清理 / live-only 项(PM 6-key extraction / OE start_ts / CURRENT_BETS schema)。 |
| 2026-05-26 (#39) | **Q11.A DebugDataClient 落地 + 撤回 `DebugArbitrageStrategy` 整条 + 锁"下单价掉包不放 execution"**:用户问"strategy 需要做掉包吗?strategy 里的参数本身就是可设置的"—— 框架重大澄清。**落地**:① `src/arbitrage/debug/data_clients.py`:`_DebugDataClientMixin` 拦 `_handle_data` + `_maybe_substitute(data) → data|None` 钩子(默认 passthrough,具体替换算法 user 子类化覆盖,**框架不预设 mock_data schema 因为 user 场景特定**);`DebugPolymarketDataClient(PolymarketDataClient)` / `DebugOrbitExchDataClient(OrbitExchDataClient)` 两子类除继承外完全对称;② PM `arb_factories.ArbPolymarketLiveDataClientFactory.create` + OE `factories.OrbitExchLiveDataClientFactory.create` 读 `ArbContext.debug_config`,`enabled` → 装 Debug 子类并传 `debug=cfg`,否则装生产。**测试**:`test_debug_data_clients.py`(5:默认 passthrough / 子类覆盖 hook 替换 / hook 返 None 退化 passthrough / 子类经 self._debug 读 mock_data 决定替换 / debug_config 访问器)+ `test_debug_data_factories.py`(5:PM 无 debug / PM disabled / PM enabled → Debug 子类 + 传 debug=cfg / OE 无 debug / OE enabled → Debug 子类 + 传 debug=cfg;monkeypatch 替 NT 客户端类 + heavy deps 为 sentinel,聚焦验 factory 挑哪个类构造)。**全 arb 套件 213 passed(+10 Q11.A,0 regression)** / 33 skipped。**重大撤回(整条 strategy 层 Debug 子类机制砍掉)**:用户审"strategy 参数本身就是可设置的,如果想更换参数直接在策略里配置不就行了?"—— 完全对。**Q21 框架下 strategy 参数(min_rebate / price / size / share_scaler)是具体 `Check`/`Action` 的构造参数**,first-class config,要 debug 时**配置一份 debug 版 Strategy 实例**(同 scope,Check/Action 用极端参数)即可,经 `StrategyRegistry.register_*` 装载。**原候选 (a) `EvalContext.debug_overrides` 注入 + (b) Debug Check/Action 子类替换都取消**:(a) 违反 P10(生产 Check/Action 带 `if ctx.debug_overrides` 分支);(b) 工程量大但实际需求(参数 override)已被 Q21 参数化吸收。**老 hook 机制(`_get_min_rebate_rate` / `_get_pm_price` / `_get_oe_size`)是 pre-Q21 单体 Strategy 类的产物**,Q21 拆出 Check/Action 后冗余。**第二澄清:"下单价掉包"不放 execution**:用户问"下单价格的掉包之前 execution 规划过吗?"—— 答没有。execution 一直规划为透明传递层(只决定要不要执行 / 怎么报告,不改 order content);破开会让"size 也改不改 / venue_id 也改不改"一路滑下去,失去 audit clarity。下单价的极端 override 由 Strategy 层 Action 参数化处理(如 `PMSubmitAction(price_override=0.01)`)。**文档**:`_cross-cutting/debug-injection.md` 落地状态段加 ✅ #39 + ❌ 撤回 strategy 子类 + ❌ 下单价不放 execution;`data/architecture.md §4 横切咬合` 加 Q11.A 行;`tests/arbitrage/strategy/README.md` 替换"Strategy hook 点契约"老表为 Q21+#39 的"配置 vs 配置"模型 + Debug 相关章节改写为"无 strategy 层 Debug 子类";debug README 加 27 passed 状态(17 base + 10 #39)+ 撤回决策。**仍待**:`SkipExecution{PM,OE}ExecutionClient`(Q11.3);`timeline.py`(Q11.4 NT Clock 状态机,只在 SkipExecution 真要 mock 订单 lifecycle 时才需要)。 |
| 2026-05-24 (#38) | **Q11 Debug 框架基础落地(Slice A+B;skip_check_size 全链路)+ 撤旧 `DebugManager` 单例 + 浮 strategy hook 设计冲突**:用户选 Q11 接 strategy 之后。**slice A**:① **`src/arbitrage/debug/config.py:DebugConfig`** 重写自 `services/debug/config.py` —— 保留配置 schema(enabled / overrides / mock_data,用户 `debug_config.json` 不动)+ DebugOverride/MockDataItem dataclass + JSON load/from_dict/to_dict;**砍 `DebugManager` 单例**(Q11.5 反单例,改 DI 经 `ArbContext.debug_config` 传),`is_override_active` 双闸(总开关 + 该项 enabled),`get_mock` 按 category+conditions+priority。② **`debug/risk.py:DebugArbitrageLiveRiskEngine`** —— `skip_check_size` 激活时跳 `super()._check_order`(NT price/qty/GTD),只跑应用层 `_check_balance` + `_check_rebate_gates`。~10 行,前一轮"粒度"伪问题已撤;**就是"跳过"**。**slice B**:③ `ArbContext.debug_config` 字段新增(运行时类型 `DebugConfig | None`,避 bootstrap 循环 import);④ `install_arbitrage_engines(debug_config=...)` 扩展:`enabled=True` 时**装薄包装类 `_KernelInjectedDebugEngine(DebugArbitrageLiveRiskEngine)`**(kernel 按 LiveRiskEngine 实参表构造、不会传 `debug=`,从**闭包**注入 cfg);`enabled=False` / None → 装生产 `ArbitrageLiveRiskEngine`;Portfolio 不分 debug(本轮只覆盖 Risk)。⑤ `wire_arbitrage_runtime` 的 `isinstance(risk_engine, ArbitrageLiveRiskEngine)` 检查对 debug 透明(包装类继承链)。**测试**:`test_debug_config.py`(7)+ `test_debug_risk_engine.py`(5,用 `unittest.mock.patch.object(ArbitrageLiveRiskEngine, "_check_order")` 验 super 是否被调)+ `test_bootstrap_integration.py`(5:无 debug / disabled / enabled / 双 cfg 双包装类闭包独立 / `ArbContext.debug_config` 字段)。**全 arb 套件 203 passed(+17 debug,0 regression)** / 27 skipped。**捅出的设计冲突(下个 slice 必须先讨论)**:Q11 旧 `DebugArbitrageStrategy` 的 `_get_min_rebate_rate` / `_get_pm_price` / `_get_oe_size` 等 hook **预设单体 Strategy 类**,跟 **#37 Q21 框架(`StrategyEvaluator(Actor)` + `Condition` tree + `Check`/`Action`)不兼容** —— Q21 下 min_rebate / price / size 是具体 `Check`/`Action` 的参数。**候选重设**:(a) `EvalContext.debug_overrides` 注入,具体 Check/Action 读;(b) Debug Check/Action 子类替换在 Strategy 注册时切。**待 slice C 讨论**。**仍待**(独立小 slice):`SkipExecution{PM,OE}ExecutionClient`(Q11.3,需 generate_order_filled mock + 可能 timeline);`DebugDataClient` mock 数据帧(Q11.A);`timeline.py`(Q11.4 NT Clock 状态机)。文档:`debug-injection.md` 加落地状态段 + Q21 冲突段;debug README 加 17 passed 状态 + 仍待;risk arch §7 `skip_check_size` ⬜→✅。 |
| 2026-05-24 (#37) | **Step 4 Strategy 框架代码落地(Q21 设计 → 全 5 slice 单测;不迁旧具体策略)**:用户决定旧 `services/strategy/` 3000 行不迁(架构变了,旧码能用的不多),只做框架。**按 architecture §7 顺序 5 slice**:① **`SignalStore`**(双状态:persistent 写后保留 / transient 用后即清;`peek` 不消费供 BoolExpr 求值,`get` 消费供 Action 决策)— 6 tests;② **`BoolExpr` + `SignalRef`/`AndExpr`/`OrExpr`/`NotExpr`**(Q10b 表达式树,叶子 SignalRef 经 `store.peek` 读)— 9 tests(含 vacuous truth + 嵌套 + 求值不消费 transient 关键不变量);③ **`Condition` / `Check` / `Action` / `EvalResult` + `evaluate_tree` 纯函数**(算法从 Actor method 提到 condition.py 模块级,纯逻辑可全单测;Actor 只做 orchestration)— 9 tests(self_hits False / sub互斥命中即停 / 全 miss / 叶子 checktion AND / 空 list 默认通过 / action=None 仍 hit / **evaluate 求值不消费 transient + 不 fire action**);④ **`Strategy` / `StrategyRegistry`**(三层 `register_pair/_competition/_sport`,`get_for` 按优先级挂载锁定不降级 Q21-a)— 6 tests;⑤ **`OpportunitySnapshot` + `build_snapshot`**(Q20,per-evaluation 容器,builder 从 cache/portfolio/pair_registry 一次性聚)— 5 tests(快照后 cache 变动不扰动容器内容、无长存全局 dict、未知 pair 空 snapshot 不抛);⑥ **`StrategyEvaluator(Actor)`**(NT Actor,`on_data` 提取 `(pair_id, sport, competition)` → MatchedPair 直读 / OrderBookDeltas 经 PairRegistry+info 反查;`_evaluate_and_fire` Q19 让路 + Q20 snapshot + `asyncio.gather` 并行 + 套利优先 fire-and-forget)— 8 tests(MatchedPair 直接路由 / 无挂载 no-op / Q19 跳过 / Q21 套利优先 / 补救兜底 / 双 miss 不 fire / SignalCollector 接入 / 非目标数据安全 skip)。**抓出/避免的设计陷阱**:`SignalStore.peek` vs `get` 严格分(`peek` 给 BoolExpr 用、`get` 给 Action 用,避免 stale + 求值消费的二次错乱);`evaluate_tree` 提纯函数(decouple from Actor;tests 直接 unit test 无需 Actor 全栈);`_aevaluate` async wrapper(sync `evaluate_tree` 包成 coroutine 供 `asyncio.gather`,Check 演进到 async I/O 时本层零改);build_snapshot 不向全局 dict 注册(per-evaluation GC,Q20 #21b)。**全 arb 套件 186 passed(+43 strategy framework,0 regression)** / 27 skipped(均为旧 placeholder 或需 live)。strategy arch §3.5 对齐(_evaluate_tree → 模块级 evaluate_tree + _aevaluate 包装)。**仍待**:Step 4 后续 — 具体 Strategy 实例(用户自写,本框架 Check/Action 抽象类填充);SignalCollector 跟外部信号源(比分 / 比赛开始 等)的接入;launcher 接 StrategyEvaluator + 注入 PairRegistry/StrategyRegistry/SignalStore(整合 /live-test 时)。**Step 1-6 全 NT-native 闭环**;Step 7 web + 旧 services 清理 / Q11 Debug 框架 / live-only 项(PM extraction / OE start_ts / CURRENT_BETS item)仍按之前计划独立 slice 推进。 |
| 2026-05-24 (#36) | **Q21 strategy 框架锁定(用户拍板,详细设计落地 — 进 Step 4 前最大块决策)**:之前 strategy 是搁置的最大缺口(refactor.md §5.4 已锁的全是约束,核心信号流水线/决策框架未设计)。用户本轮提出完整框架:① **scope-priority**(pair_id > comp > sport)+ **挂载存在锁定**(具体比赛挂了即使没命中也不下放到 comp/sport;Q6a);② **每 scope 单策略**(收回"同 scope 并行多策略",简化避免多策略冲突);③ **策略 = 套利树 + 补救树**(asyncio.gather 并行评估;套利 hit 阻断补救 fire — 关键洞察:**evaluate 必须无副作用**返 EvalResult,fire 由 evaluator 顶层做,补救才能等套利 evaluate 结果);④ **Condition 嵌套树**:`{self_hits: BoolExpr, sub_conditions: list, checktion: list[Check] AND, action: 1个}`;sub_conditions 命中即停(互斥);叶子 checktion 全过 → action 待 fire(空 list 默认通过);action=None 默认 no-op pass;⑤ **SignalStore 双状态**:persistent(写后保留,如 `live=True`)/ transient(用后即清,如 `rebate` — `peek` 不消费供 BoolExpr 求值,`get` 消费供 Action 决策用);⑥ **BoolExpr AND/OR/NOT 表达式树** + `SignalRef` 叶子(Q10b 灵活而非 list AND);⑦ Q19 互斥 + Q20 快照都包(evaluate 开跑前 `execution_active` 跳过、整轮快照 + safety gate 走 live);⑧ **NT 无原生策略优先级**(msgbus handler priority 语义不同),scope-priority 自建于本组件,**不超前抽象**为通用件(P7)。**审问触发的设计修正**:用户对"Debug 粒度"措辞反问 → 撤回 — `skip_check_size` 就是普通 skip(`super()._check_order` 不调),"粒度"是我的伪问题,~10 行随 Q11 Debug slice 落;用户对"size 替换 vs skip"独立性反问 → 撤回我"bump-to-min 取代 skip"的越权推论,两者是独立 Debug 选项,Q11 时按各自语义实现。**落地**:`architectures/strategy/architecture.md`(占位"搁置" → 标准 7 节 ~300 行)+ §7 Q21 状态行 + strategy README 加 Q21 框架锁定 header + 新 `strategy-4.framework.x` 用例集(store / expr / cond / reg / eval / snap 共 ~26 个);risk arch §3.2 主文本同步 #34 pair_registry 改正 + `_leg_from_position` 加 `selection_role`(Q9)/ `market_type` fallback 测试(2 个 — 143 passed);risk §7 `skip_check_size` 措辞去"粒度"伪问题。**待**:Step 4 框架代码落地(SignalStore → BoolExpr → Condition → StrategyRegistry → StrategyEvaluator Actor → OpportunitySnapshot,渐进单测);旧 `services/strategy/` 3000 行作为具体 Check/Action 类填充内容平移(框架结构按本文)。 |
| 2026-05-24 (#35) | **Step 2 完成 + PM info 6-key seam(用户要求 strategy 前清完遗留)**:用户问"市场数据订阅迁了没?",自查发现 Step 2 整体未动 + PM info 6-key 接线缺。**落地**:① **OE DataClient 整体重写**(`nautilus_trader/adapters/orbitexch/data.py`):旧版基类用错(`LiveDataClient` 应 `LiveMarketDataClient`)、出 `QuoteTick` 应出 `OrderBookDelta`、自己起浏览器(违反 Q2 共享单例)、订阅 API 通用(应 type-specific `_subscribe_order_book_deltas`);现版本 `LiveMarketDataClient` 子类 + `_connect` 只 `get_page("data")`(不启停 browser)+ `_on_price_frame` 经新模块函数 `oe_runner_to_book_deltas`(snapshot CLEAR + N×BACK ADD(BUY) + M×LAY ADD(SELL))包 `OrderBookDeltas` 入 `_handle_data` + routing 表 `dict[market_id_str, dict[selection_id_str, InstrumentId]]`(订阅时从 BettingInstrument 读 market_id/selection_id 建)+ 未订阅市场静默丢弃。`OrbitExchLiveDataClientFactory` 同目录 factories.py 加,构造 `PlaywrightBrowserManager`(单例 §6.2)+ data client。② **PM `ArbPolymarketInstrumentProvider`**(`nautilus_trader/adapters/polymarket/arb_provider.py`):覆盖 `_parse_instrument` super 后 `instrument.info.update(enrich_pm_six_key_info(market_info, outcome))` —— matching `events_from_instruments` 必需 Q9 6-key,上游 `info=market_info`(gamma dict)没 6-key。`enrich_pm_six_key_info` 当前是 **best-effort seam**:`sport ← market_info["category"]`(能拿就拿),其它 5-key 空字符串/0 占位,**TODO 实写**需调 PM gamma `/events/{event_id}` + ticker 拆解(参旧 `odds_client.py:255+` 的"epl-not-lee-..."拆法)。`ArbPolymarketLiveDataClientFactory`(`arb_factories.py`)用此 provider + 上游 `PolymarketDataClient` 不变(P1 复用)。**结构完整**:matching 实写 extraction 时下游 0 行修改;现状下 PM 侧暂不参与匹配(`events_from_instruments` 见空 4-key 跳过)。**测试**:`test_data_client_step2.py`(10 passed:snapshot 转换/单边/空跳过/客户端构造/路由读 instrument/未订阅丢弃/full message 路由)+ `test_arb_provider.py`(4 passed:6-key 必含/category→sport/缺字段空占位/null 安全)。全 arb 套件 **141 passed,27 skipped**。data architecture.md 占位 → 标准 7 节;OE adapter README oe-adapter-2.x 标 ✅;PM adapter README pm-adapter-1.2 标 ✅ + extraction TODO;discovery README 补 #35 注。**剩余阻塞**:PM 旧代码删除(odds_client/executor/scraper)被 `services/execution`+`services/odds_subscription`+`services/web_gateway` 链阻塞;OE scraper DOM 抽 start_ts 需 live DOM 探索。两者均待 web_gateway 处理后 / live 阶段。 |
| 2026-05-24 (#34) | **Step 3 Matching 落地 + pair_id 来源校准(用户审计 #33 风格的反查触发)**:写 matching 算法时反查发现之前埋下的 bug —— `info["competition"]` 被当 pair_id 用(`_resolve_pair_id` 等),但 `competition` 是**联赛名**(EPL/NFL,多场比赛),真实数据下两场同 league 比赛会被合成同一 pair → way_rebate 跨场混算。**真实 pair_id 是 matching 算出的**(老 `MatchedPair.pair_id` 基于 PM event_id),应通过共享 `PairRegistry` 暴露(同 leg_settled P11 模式,但有单一自然归属:matching 唯一写者 + 其它只读 → 放主方小节,不单独成横切章节)。**落地**:`src/arbitrage/common/pair_registry.py`、`src/arbitrage/matching/{events,normalizer,engine,actor}.py` + 配套测试 26 passed;`bootstrap.ArbContext` 加 `pair_registry`,launcher 经 wire 注入;`ArbitragePortfolio.configure_arb` 增 `pair_registry` 参,`_resolve_pair_id` / engine `_pair_id_for_order` / session `_pair_id_for` 全改读 registry;PM/OE ExecClient 子类 + factory 链 pass `pair_registry`。**附带修两个 bug**:① `MarketMatchingActor._both_recent` 用 `.get(v, 0)` 在 TestClock t=0 时假阳性放行 → 改"两 venue 都必须有过 refresh 事件"(测抓);② NT `Actor` 属性是 `self.cache`/`self.clock`(public readonly cdef,非 `_cache`/`_clock`)(测抓);③ `_leg_from_position` `info` 读 key 校正:Q9 标准是 `selection_role`,旧 `market_type` 作 fallback 兼容。matching 详细设计(占位 → 标准 7 节)+ discovery doc 注释更正 + risk doc + risk/matching README 全部回写。**127 passed**(从 101 增 26 matching/registry tests + 0 regression)。**教训重申**:#33 之后我开始按用户审计风格反查自己的代码,这次 #34 就是这个习惯结的果。 |
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
