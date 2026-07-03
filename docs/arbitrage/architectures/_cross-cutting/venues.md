# 横切:Venue Registry / Capability 详细设计

> **定位**:详细设计。本文是第二阶段 venue 插拔化的单一真理源(Q28)。
> **成熟度**:落地中(2026-07-02)。静态 registry、launcher enablement、matching tradable venue 派生、strategy/risk 公式类 helper 已落地;完整 venue graph / 动态插件不在本阶段。
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
    display_group: Literal["primary", "external"]
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

| venue | display_group | instrument_model | odds_model | 金额口径 | 专属能力 |
|---|---|---|---|---|---|
| `POLYMARKET` | `primary` | `binary_option` | `probability` | USD/USDC.e | PM CTF settlement |
| `ORBITEXCH` | `external` | `betting` | `decimal` | adapter 外 USD 归一 | Playwright BIAB/OE adapter |
| `SHARPEXCH` | `external` | `betting` | `decimal` | USD 原生 | Playwright BIAB/SE adapter |

`display_group` 只服务 Web 旧 `pm_*` / `external_*` 展示字段,不表达 matching anchor。当前 matching anchor 是
`DATA_SOURCE_REGISTRY["sports_status"]` 产出的 `.PMSPORTS` non-tradable instruments。

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
def probability_from_price(venue: str, price: float) -> float: ...
def qty_from_share(venue: str, share: float, price: float) -> float: ...
```

约束:

- `probability_from_price("POLYMARKET", price)` 使用 PM 概率价格公式。
- `probability_from_price(decimal venue, odds)` 使用 decimal odds 概率公式。
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

1. 新增 registry + helper + 单元测试,不接业务调用。
2. 替换 launcher enablement / factory registration / liveness 初始化。
3. 替换 dispatcher tradable venues 派生。
4. 替换 strategy 中 decimal odds 集合判断。
5. 替换 risk/portfolio 中公式类 venue 判断。
6. 更新 web/config 展示中的 enabled venue 派生。
7. 将 PMSPORTS 从 venue descriptor 剥离成 `DATA_SOURCE_REGISTRY`。
8. 跑 PM+OE、PM+SE、OE+SE、PM+OE+SE 离线测试与 `skip_execution=true` smoke。

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

live / smoke 验收(本设计落地后再做):

- `skip_execution=true` PM+SE smoke:discovery/matching/strategy/risk submit intent 可达。
- 真 SE 成交路径与完整套利 E2E 不作为 registry 设计阶段验收前置。
