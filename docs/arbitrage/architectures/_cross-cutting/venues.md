# 横切:Venue Registry / Capability 详细设计

> **定位**:详细设计。本文是第二阶段 venue 插拔化的单一真理源(Q28)。
> **成熟度**:第二阶段静态 venue capability 已落地(2026-07-08)。静态 registry、launcher enablement、factory 注册、ArbContext keyed map、PMSPORTS data source、matching tradable venue 派生、strategy/risk/web 公式与展示主路径已切到 capability/keyed map;完整 venue graph / 动态插件不在本阶段。
> **归属判据**:venue 插拔同时约束 config、launcher、matching、strategy、risk、web 与 adapter factory,
> 无单一自然归属,按 P11 放在横切章节。

---

## 1. 目标与非目标

第二阶段目标不是把 PM/OE/SE 合成一个抽象交易所,而是把“同类规则”从散落硬编码收敛到
venue capability。

**目标**:

- `venues.*.enabled` 决定 runtime 注册哪些 venue。
- launcher / dispatcher 不再重复写 `if POLYMARKET / ORBITEXCH / SHARPEXCH`。
- strategy / risk 不再散落 `venue in {ORBITEXCH, SHARPEXCH}` 这类类别判断。
- 真实 venue identity 保留:instrument id、account id、adapter、factory、日志和持仓归属仍使用真实 venue。
- PMSPORTS anchor 数据源已从 `POLYMARKET` venue descriptor 剥离到独立
  `DATA_SOURCE_REGISTRY["sports_status"]`;matching anchor 是 `PMSPORTS`
  non-tradable event instruments,不是 PM 可交易 instruments。

**非目标(本阶段不做)**:

- 不做第三方动态插件加载 / pip entrypoint / 配置 import class。
- 不做 OE↔SE 或任意 venue pairwise matching;当前仍通过 PMSPORTS event anchor 聚合 enabled tradable venues。
- 不泛化 PM settlement;merge/redeem 仍是 PM 专属 capability。
- 不抽象 OE/SE browser/login/page 内部细节;adapter 子树继续各自实现。
- 不把 SE 成交路径或完整套利 E2E 作为本设计阶段的前置。

---

## 2. 核心原则

| 原则 | 说明 |
|---|---|
| 保留 identity | `POLYMARKET` / `ORBITEXCH` / `SHARPEXCH` 仍是不同 NT `Venue`,不能替成一个统一实例 |
| 抽能力不抽差异 | 概率模型、size/share 公式、金额口径这类同类规则查 capability;登录、下单 payload、settlement 仍留各 adapter |
| 静态 registry 先行 | 第一版是代码内静态表,不是动态插件系统 |
| PMSPORTS 来源显式化 | PMSPORTS anchor/lifecycle 是 data source,不属于任何 trading venue;通过 `data_sources.sports_status.enabled` 开关 |
| 单一事实源 | venue 的 display group/odds model/stake 口径/factory 等只在 registry 定义,其它组件引用 |

---

## 3. VenueDescriptor

代码侧新增普通 Python registry,建议放 `src/arbitrage/common/venues.py`:

```python
@dataclass(frozen=True)
class VenueDescriptor:
    venue_id: str                         # "POLYMARKET" / "ORBITEXCH" / "SHARPEXCH"
    config_key: str                       # "polymarket" / "orbitexch" / "sharpexch"
    instrument_model: Literal["binary_option", "betting"]
    odds_model: Literal["probability", "decimal"]
    amounts_normalized_to_usd: bool
    stake_currency: str                   # adapter 入站后 cache 展示/风控口径

    data_config_builder: str
    exec_config_builder: str | None
    discovery_config_builder: str | None
    data_factory: str | None
    exec_factory: str | None

    settlement_kind: Literal["none", "polymarket_ctf"] = "none"
```

## 3.1 DataSourceDescriptor

PMSPORTS 不再混入 `VENUE_REGISTRY`。代码侧另有 data source registry:

```python
@dataclass(frozen=True)
class DataSourceDescriptor:
    source_id: str                         # "sports_status"
    config_key: str                        # "sports_status"
    client_id: str                         # "PMSPORTS"
    provider: str                          # "polymarket_sports"
    data_config_builder: Callable[[ArbConfig], object]
    data_factory: type
```

Venue 静态表:

| data source | client_id | provider | 职责 |
|---|---|---|---|
| `sports_status` | `PMSPORTS` | `polymarket_sports` | `.PMSPORTS` synthetic event anchors + SportsGameUpdate lifecycle |

第一版静态表:

| venue | instrument_model | odds_model | 金额口径 | 专属能力 |
|---|---|---|---|---|
| `POLYMARKET` | `binary_option` | `probability` | USD/USDC.e | PM CTF settlement |
| `ORBITEXCH` | `betting` | `decimal` | adapter 外 USD 归一 | Playwright BIAB/OE adapter |
| `SHARPEXCH` | `betting` | `decimal` | USD 原生 | Playwright BIAB/SE adapter |

Matching anchor 是 `DATA_SOURCE_REGISTRY["sports_status"]` 产出的 `.PMSPORTS`
non-tradable instruments;不由 venue descriptor 的展示字段表达。

`VENUE_REGISTRY` 只表达系统已支持的 venue;用户配置中的 `enabled=false` 不删除 descriptor,
只让该 venue 不进入 runtime。

---

## 4. Helper API

建议提供以下纯函数,供 launcher/dispatcher/strategy/risk/web 使用:

```python
def all_venues() -> tuple[VenueDescriptor, ...]: ...
def is_venue_enabled(cfg: ArbConfig, venue: str) -> bool: ...
def enabled_venues(cfg: ArbConfig) -> tuple[VenueDescriptor, ...]: ...
def enabled_venue_ids(cfg: ArbConfig) -> tuple[str, ...]: ...
def enabled_tradable_venues(cfg: ArbConfig) -> tuple[VenueDescriptor, ...]: ...
def enabled_tradable_venue_ids(cfg: ArbConfig) -> tuple[str, ...]: ...
def enabled_data_sources(cfg: ArbConfig) -> tuple[DataSourceDescriptor, ...]: ...
def enabled_data_source_client_ids(cfg: ArbConfig) -> tuple[str, ...]: ...
def enabled_sports_client_ids(cfg: ArbConfig) -> tuple[str, ...]: ...

def descriptor_for(venue: str) -> VenueDescriptor: ...
def is_decimal_odds_venue(venue: str) -> bool: ...
def probability_from_price(venue: str, price: float, claim: str = "yes") -> float: ...
def order_exposure_probability(venue: str, price: float, side: str) -> float: ...
def qty_from_share(venue: str, share: float, price: float) -> float: ...
def outcome_for_position(
    venue: str,
    outcomes: Collection[str],
    *,
    selection_role: str | None,
    claim: str | None,
    position_side: str,
) -> str | None: ...
def leg_economics(venue: str, price: float, size: float, *, is_lay: bool = False) -> LegEconomics: ...
def order_liability(venue: str, quantity: float, price: float, *, is_lay: bool = False) -> float: ...
def order_required_balance(venue: str, quantity: float, price: float, side: str) -> float: ...
```

### 4.1 净 Position → outcome 经济腿(#230)

> **成熟度**:已落地(2026-07-15)。

`outcome_for_position` 与 `leg_economics` 共同构成**单腿持仓经济量唯一的家**。
`ArbitragePortfolio`、Strategy `mean_rebate_recovery` 和间接消费 Portfolio 的 `share_limit`
必须委托这两个 helper,禁止各自维护 claim/side/赔率公式。

`outcome_for_position` 先决定 NT 净 Position 经济上属于 pair 的哪个互斥 outcome:

| Position | outcome 归属 |
|---|---|
| LONG | `claim or selection_role`,且必须存在于 `outcomes`;Portfolio 调用方只允许显式 `claim=yes/no` |
| decimal SHORT(LAY) | 上述 base outcome 在二元 `outcomes` 中的**另一个 outcome** |
| probability SHORT | 违反执行/对账不变量,抛 `PositionOutcomeInvariantError`,调用方 fail-closed |
| FLAT / 未知 side / 非二元 complement | 返回 `None`,不猜测 |

这同时覆盖两类二元 pair:

- 3-way 拆分 pair:`outcomes=[yes,no]`;OE/SE no 腿真单重定向到 yes instrument 后形成
  `SHORT`,因此 `yes → no`。PM NO token 是独立 instrument 的 `LONG claim=no`,直接归 `no`。
- 2-way 与 3-way 拆分 pair 均为 `outcomes=[yes,no]`;`selection_role` 只用于匹配和展示,
  Portfolio 不再用它兜底经济 outcome。decimal SHORT 映射到其 instrument claim 的互补 outcome。

`leg_economics` 只计算已经完成 outcome 归属后的金额:

| 腿型 | share_if_wins | profit_if_wins | loss_if_loses |
|---|---|---|---|
| probability(PM,LONG) | `size` | `size × (1−price)` | `size × price` |
| decimal back(LONG) | `size × price` | `size × (price−1)` | `size` |
| decimal lay(`is_lay=True`;size=lay size,price=lay odds) | `size × price` | `size` | `size × (price−1)` |

lay 行推导:lay size q 押 liability `q(L−1)`;互补 outcome 赢时收回 liability 并赢得 q →
回收 `qL`,与 back 的 share_if_wins 同形(这也是 `qty_from_share(venue, share, lay)` = `share/lay`
对 no 腿直接成立的原因)。

`order_liability` 是不含订单方向约束的底层经济函数。实际订单统一调用
`order_required_balance(venue, quantity, price, side)`:probability SELL 减仓返回 0；其余订单按
`order_liability` 计算。Strategy 的机会级资金汇总、Risk 下单前余额门控与 Execution
`OrderAccepted` 后本地预扣都必须委托该 helper,禁止各自重写公式。

`order_exposure_probability(venue, price, side)` 是订单概率门控的唯一入口。BUY 取报价对应的
yes 概率；SELL 取其补集。因此 PM SELL 减仓与 decimal LAY 都按订单实际获得的互补敞口校验,
不受概率上下界是否对称影响。

输入边界是 **NT 当前净 Position**(`side/quantity/avg_px_open`),不是历史 bet 明细。OE/SE
reconciliation 已把同 selection 的 BACK/LAY 聚合为一个 LONG/SHORT 净 Position;本模型正确计算
该净 Position 的未实现 outcome 敞口,但不尝试从净额还原已经平仓部分的历史锁利或 realized PnL。

约束:

- `probability_from_price("POLYMARKET", price)` 使用 PM 概率价格公式。
- `probability_from_price(decimal venue, odds)` 使用 decimal odds 概率公式。
- `claim` 是经济 outcome,不再兼任行情类型。只有合成 no instrument 带 `quote_claim="no"`；
  真实 decimal NO runner 的 `quote_claim` 默认 yes,因此仍按自身 BACK 赔率 `1/price` 解释。
- `probability_from_price(decimal venue, price, claim="no")`
  返回 `1 − 1/price`——decimal venue 的合成 no 腿 book 存的是 **lay 列原值**(cache 只存 venue
  原始价、下单价零换算的不变量,见 data 架构"OE/SE 3-way 腿模型"),其隐含概率是补集。
  probability venue 与 `claim="yes"`(默认)行为不变。本函数是全系统唯一的 claim 概率换算家:
  strategy checks(strategy §3.7)、matching 概率校验门控(matching §4.2.1)、risk 概率门控
  (risk §4.1b)一律经它,禁止各自换算。
- `qty_from_share("POLYMARKET", share, price)` 返回 `share`。
- `qty_from_share(decimal venue, share, odds)` 返回 `share / odds`。
- helper 内部通过 `odds_model` 分支,禁止调用方自己维护 `{"ORBITEXCH","SHARPEXCH"}` 集合。
- enabled 判断通过 descriptor 的 `config_key` 读取 `venues.<key>.enabled`;dispatcher/launcher
  不直接写 `cfg.venues.polymarket/orbitexch/sharpexch.enabled` 路径,除非正在构造该 venue 的专属 config。
- discovery context 通过 descriptor 的 `discovery_config_builder` 派生 `discovery_config_by_venue`;
  新增 tradable venue 时在 descriptor 声明 builder,dispatcher 不再维护 OE/SE venue 列表。

---

## 5. 组件接线

### 5.1 launcher

`launchers/arb_node.py` 的以下函数改由 registry 驱动:

- `enabled_runtime_venues(cfg)` → `enabled_venue_ids(cfg)`
- `validate_venue_enablement(cfg)`
- `build_trading_node_config(cfg)`
- `prepare_runtime_state(cfg)`
- `register_factories(node,cfg)`

PM 专属逻辑不消失,但应由 descriptor 显式触发:

- enabled `DATA_SOURCE_REGISTRY["sports_status"]` → 额外注册 `SPORTS_CLIENT`
- `enabled_settlement_venues(cfg, "polymarket_ctf")` 非空 → `_make_pm_settlement`

### 5.2 configuration / dispatcher

`ArbConfig` schema 保留显式 `venues.polymarket/orbitexch/sharpexch` 字段,并新增可选
`data_sources.sports_status` 控制 PMSPORTS。常规配置可以不写该段,由 schema 默认值注册
PMSPORTS,并让目标 competitions 默认继承 `discovery.polymarket.sports`;只有需要和 PM sports
分离时才显式覆盖:

```json
{
  "data_sources": {
    "sports_status": {
      "enabled": true,
      "provider": "polymarket_sports",
      "ws_url": null,
      "sports": [
        {"sport": "Tennis", "competitions": ["atp"]}
      ]
    }
  }
}
```

原因:trading venue 的启停与 data-only sports anchor 的启停不是同一件事。PMSPORTS target
competitions 优先读 `data_sources.sports_status.sports`;为空时继承
`discovery.polymarket.sports`,避免常规配置重复写同一份 sports。

dispatcher 变化:

- `to_market_matching_actor_config(cfg)` 从 `enabled_tradable_venue_ids(cfg)` 派生
  `tradable_venues`;不再生成 legacy `external_venues`。
- discovery context 的启用判断走 descriptor `config_key` 对应的 runtime venue enabled + `discovery.<venue>.enabled`;
  专属 config 构造仍读取 `cfg.venues.<venue>` 的登录/浏览器字段。
- `to_arb_context_init_kwargs(cfg)` 遍历 `enabled_tradable_venues(cfg)`,按 descriptor
  `discovery_config_builder` 生成 `discovery_config_by_venue`;enabled 判断从 registry helper 读取。

### 5.3 matching

当前阶段已经是:

```text
PMSPORTS event anchor × enabled tradable venues
```

`MarketMatchingActor` 只接受 `anchor_venue` / `tradable_venues`;dispatcher 显式设置
`anchor_venue="PMSPORTS"`、`tradable_venues=enabled_tradable_venue_ids(cfg)`。PM/OE/SE
都是 tradable venues,匹配到同一个 non-tradable PMSPORTS event anchor。

当前 validation:

- runtime enabled venues 至少 2 个。
- `data_sources.sports_status.enabled=true`,使 PMSPORTS anchor 数据源注册进 TradingNode。
- enabled tradable venue 可为 PM/OE/SE 的任意已启用子集,但必须满足上面的 sports anchor 数据源约束。
  因此 OE+SE-only 已可通过 launcher validation,matching 仍经 PMSPORTS anchor 聚合,不是 OE↔SE pairwise。

### 5.4 strategy

策略算法保留真实 venue identity,但把类别判断改查 capability:

- `mean_rebate`:价格转概率走 `probability_from_price`。
- `one_side_rebate`:所有 enabled/匹配进来的 venue 都作为候选;同 outcome 多 venue 均保留。
- `mean_rebate_recovery`:补缺口时用 capability 计算目标 qty。
- `share_limit` / `place_bets`:size/share 推导走 `qty_from_share`。

禁止在 strategy 新增 `if venue == "SHARPEXCH"` 或 `venue in {"ORBITEXCH","SHARPEXCH"}`。

### 5.5 risk / portfolio

Risk 保留逐真实 venue 的门控和账户状态读取,但公式类判断走 capability:

- 概率门控:使用 `probability_from_price`。
- 余额门控:先保留现有 PM vs decimal venue 口径差异,但判断依据改为 descriptor。
- 最小下单金额:继续从 instrument metadata / NT 父类检查读取,不在 Risk 放 venue 常量。

Portfolio 必须继续保留真实 venue identity,不能把 SE 持仓归入 OE。

### 5.6 web

Web 可从 `enabled_venue_ids(cfg)` / cache account states 展示 enabled venues。

配置 UI 仍按显式 JSON 字段编辑,不要求第一版动态渲染任意 venue schema。

---

## 6. 迁移顺序

1. [x] 新增 registry + helper + 单元测试。
2. [x] 替换 launcher enablement / factory registration / liveness 初始化。
3. [x] 替换 dispatcher tradable venues 派生。
4. [x] 替换 strategy 中 decimal odds 集合判断。
5. [x] 替换 risk/portfolio 中公式类 venue 判断。
6. [x] 更新 web/config 展示中的 enabled venue 派生。
7. [x] 将 PMSPORTS 从 venue descriptor 剥离成 `DATA_SOURCE_REGISTRY`。
8. [ ] PM+OE、PM+SE、OE+SE、PM+OE+SE `skip_execution=true` smoke 逐组合验收。

## 6.1 当前审计结论(2026-07-08)

全局搜索当前 NT 主路径后,第二阶段剩余工作按类别归为:

| 类别 | 当前状态 | 后续动作 |
|---|---|---|
| 运行主路径旧字段 | `pm_instrument_ids` / `oe_instrument_ids` / `external_*` 已从 `MatchedPair` 与 Web 输出删除;剩余引用主要是拒绝旧字段的测试与历史说明 | 不再保留兼容兜底;新增字段必须走 `venue_instrument_ids` / `tradable_instrument_ids` |
| Enablement / factory | launcher 和 dispatcher 已从 registry 派生 data source / venue / factory / liveness;adapter 专属 config builder 仍保留在 adapter 边界 | 新 venue 接入时新增 descriptor + 专属 builder,不改 launcher 主流程 |
| PMSPORTS anchor | 已独立为 data source,不依赖 PM trading venue enablement;matching 默认走 PMSPORTS anchor 聚合 enabled tradable venues | 仍需按组合跑 skip smoke,验证 `.PMSPORTS` 不进入 Strategy/Risk/Execution |
| Web 配置 UI | 页面仍显式展示 Polymarket / OrbitExch / SharpExch 标签,因为 schema 当前就是显式字段;监控展示已按实际 venue 动态渲染 | 动态 schema-driven 配置 UI 属后续增强,不是第二阶段阻塞 |
| PM settlement | 仍是 PM 专属 capability,由 `enabled_settlement_venues(..., "polymarket_ctf")` 触发 | 不在第二阶段抽象 settlement;只保证 PM disabled 时不构造 |

---

## 7. 验收

离线验收:

- PM+OE 配置只注册 PM/PMSPORTS/OE,不注册 SE。
- PM+SE 配置只注册 PM/PMSPORTS/SE,不注册 OE。
- OE+SE 配置只注册 PMSPORTS/OE/SE,不注册 PM。
- PM+OE+SE 配置注册 PM/PMSPORTS/OE/SE。
- 少于两个 enabled venue 报 `ConfigError`。
- `data_sources.sports_status.enabled=false` 时配置校验拒绝。
- strategy helper 对 OE/SE 使用相同 decimal odds 概率与 qty 公式。
- Risk 概率门控对 PM/OE/SE 均经同一 helper。

live / smoke 验收:

- [ ] `skip_execution=true` PM+OE smoke。
- [x] `skip_execution=true` PM+SE smoke 已有 SE 侧验证记录(见 SharpExch 设计与测试 README)。
- [ ] `skip_execution=true` OE+SE-only smoke。
- [ ] `skip_execution=true` PM+OE+SE smoke。
- 真 SE 成交路径已由独立 probe 验证;完整套利 E2E 仍放在组合 smoke 后单独授权执行。
