"""
ArbConfig 顶层 msgspec schema(Q22 B 分组件 / Q24 JSON)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §2`。

- **凭证字段**(`venues.polymarket.{clob_api_key,...}` / `venues.orbitexch.{username,password}`)
  在 JSON 中应留空 / 省略,由 `loader.py` 从 env 注入(Q23 C)。
- 全部 `frozen=True, kw_only=True`(msgspec idiom)。
- list/dict 默认值用 `msgspec.field(default_factory=...)` 避免共享对象。
"""

from __future__ import annotations

import msgspec


class ConfigError(Exception):
    """配置加载 / 校验失败。"""


class ConfigStruct(msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True):
    """配置 schema 基类:所有未知字段统一作为配置错误处理。"""


# ─── Discovery ────────────────────────────────────────────────────────────


class SportFilter(ConfigStruct):
    """{sport: "Tennis", competitions: ["atp", ...]}。"""

    sport: str
    competitions: list[str] = msgspec.field(default_factory=list)


class VenueDiscoveryConfig(ConfigStruct):
    enabled: bool = True
    sports: list[SportFilter] = msgspec.field(default_factory=list)


class DiscoveryConfig(ConfigStruct):
    refresh_interval_secs: float = 10.0     # #59:MarketMatchingActor timer 轮询间隔(锁 10s;发现间隔在 data client update_instruments_interval_mins,与此解耦)
    polymarket: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)
    orbitexch: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)
    sharpexch: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)


# ─── Data Sources ─────────────────────────────────────────────────────────


class SportsStatusDataSourceConfig(ConfigStruct):
    """PMSPORTS event anchor / lifecycle data source。"""

    enabled: bool = True
    provider: str = "polymarket_sports"
    ws_url: str | None = None
    sports: list[SportFilter] = msgspec.field(default_factory=list)


class DataSourcesConfig(ConfigStruct):
    sports_status: SportsStatusDataSourceConfig = msgspec.field(
        default_factory=SportsStatusDataSourceConfig,
    )


# ─── Matching ─────────────────────────────────────────────────────────────


class MatchingConfig(ConfigStruct):
    sport_aliases: dict[str, str] = msgspec.field(default_factory=dict)
    competition_aliases: dict[str, str] = msgspec.field(default_factory=dict)
    competition_max_matches: dict[str, int] = msgspec.field(default_factory=dict)


# ─── Venues ───────────────────────────────────────────────────────────────


class PolymarketSectionConfig(ConfigStruct):
    """Polymarket 接入参数 + 凭证(凭证字段由 env 注入,JSON 中应留 None)。"""

    enabled: bool = True
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    relayer_url: str = "https://relayer-v2.polymarket.com/"
    polygon_rpc_url: str = "https://polygon-rpc.com/"
    proxy_url: str | None = None
    signature_type: int = 0
    # 透传 NT 上游 PolymarketExecClientConfig;默认 None = 上游默认(当前等价不重试)。
    # 若启用会同时影响 PM CLOB submit/cancel/report,真钱前需显式评估。
    max_retries: int | None = None
    retry_delay_initial_ms: int | None = None
    retry_delay_max_ms: int | None = None
    # 凭证(env-only,见 loader §4)
    clob_api_key: str | None = None
    clob_api_secret: str | None = None
    clob_passphrase: str | None = None
    private_key: str | None = None
    funder: str | None = None
    # builder relayer(可选)
    builder_api_key: str | None = None
    builder_api_secret: str | None = None
    builder_passphrase: str | None = None


class OrbitExchSectionConfig(ConfigStruct):
    enabled: bool = True
    base_url: str = "https://www.orbitexch.com"
    page_load_timeout_sec: float = 120.0
    # #109:competition 页 WS handler 内部 liveness timeout(超此无任何帧含心跳 → reload)。
    # 旧 `health_interval_sec`(HealthCheckLoop tick)随 #109 退役删除。
    staleness_timeout_sec: int = 300
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: str | None = None
    # 凭证(env-only)
    username: str | None = None
    password: str | None = None


class SharpExchSectionConfig(ConfigStruct):
    """SharpExch 接入参数 + 凭证(凭证字段由 env 注入,JSON 中应留 None)。"""

    enabled: bool = False
    base_url: str = "https://portal.sharpxch.com"
    login_url: str = "https://sharpxch.com/player/"
    page_load_timeout_sec: float = 120.0
    cloudflare_timeout_sec: float = 120.0
    staleness_timeout_sec: int = 300
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: str | None = None
    # 凭证(env-only)
    username: str | None = None
    password: str | None = None


class VenuesConfig(ConfigStruct):
    polymarket: PolymarketSectionConfig = msgspec.field(default_factory=PolymarketSectionConfig)
    orbitexch: OrbitExchSectionConfig = msgspec.field(default_factory=OrbitExchSectionConfig)
    sharpexch: SharpExchSectionConfig = msgspec.field(default_factory=SharpExchSectionConfig)


# ─── Strategy(JSON 驱动嵌套 Condition 树,Q25)──────────────────────────


class StrategyJsonConfig(ConfigStruct):
    """单条 strategy 的 JSON 配置(Condition 树 + 可选补救树)。

    `arbitrage_tree` / `compensation_tree` 用 `dict | None`(loader 递归解析,见 slice 5)。
    """

    arbitrage_tree: dict | None = None
    compensation_tree: dict | None = None
    description: str = ""


class StrategyBindingConfig(ConfigStruct):
    """`scope` 格式:`pair:<pair_id>` / `competition:<name>` / `sport:<name>`。"""

    scope: str
    strategy_id: str


class StrategySectionConfig(ConfigStruct):
    enabled: bool = True
    log_evaluations: bool = False
    strategies: dict[str, StrategyJsonConfig] = msgspec.field(default_factory=dict)
    bindings: list[StrategyBindingConfig] = msgspec.field(default_factory=list)


# ─── Arbitrage / Risk / Execution / Debug ─────────────────────────────────


class ArbitrageSectionConfig(ConfigStruct):
    """套利运行默认值。strategy params 显式配置时覆盖这些默认值。"""

    share: float = 22.5
    max_leg_share: float | None = None
    fx: float = 1.33


class RiskSectionConfig(ConfigStruct):
    match_tp: float = 0.05
    match_sl: float = -0.05
    min_probability: float = 0.03
    max_probability: float = 0.97


class ExecutionSectionConfig(ConfigStruct):
    tracking_timeout_sec: float = 30.0
    # 注(#110):PM merge/redeem 改 NT 连续 position 对账驱动(无 HealthCheckLoop)→ 旧
    # `health_check_interval_sec`(PM tick 间隔)已删;对账节奏在 launcher `position_check_interval_secs`。
    cleanup_enabled: bool = True
    cleanup_merge_enabled: bool = True
    cleanup_claim_enabled: bool = True
    # NT 连续对账周期(原硬编码在 launcher,#121 提出来可配):open=order/挂单对账(#111),
    # position=持仓对账(#110,驱动 PM merge/redeem)。改后需重启(LiveExecEngineConfig build 时定)。
    open_check_interval_secs: float = 300.0
    position_check_interval_secs: float = 300.0
    # decimal venue(OE/SE)市价单开关(#256 续):打开后 place_bets 用书内最差价下单,
    # 保证成交而非最优价。改后需重启(ArbContext 构造时定,见 bootstrap.py)。
    market_order_enabled: bool = False


class DebugSectionConfig(ConfigStruct):
    """与 `src/arbitrage/debug/config.py:DebugConfig` schema 对齐;dispatcher 转换。"""

    enabled: bool = False
    overrides: dict = msgspec.field(default_factory=dict)
    mock_data: dict = msgspec.field(default_factory=dict)


class WebSectionConfig(ConfigStruct):
    """Step 7 只读监控 WebGatewayActor(详细设计 architectures/web/architecture.md)。
    默认关闭;`host` 默认只绑本机,暴露公网需用户显式改。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    start_halted: bool = True  # #119:web 开启时 boot 即 HALTED,操作员点 Start 才放行(真金安全默认)


# ─── 顶层 ArbConfig ───────────────────────────────────────────────────────


class ArbConfig(ConfigStruct):
    arbitrage: ArbitrageSectionConfig = msgspec.field(default_factory=ArbitrageSectionConfig)
    discovery: DiscoveryConfig = msgspec.field(default_factory=DiscoveryConfig)
    data_sources: DataSourcesConfig = msgspec.field(default_factory=DataSourcesConfig)
    matching: MatchingConfig = msgspec.field(default_factory=MatchingConfig)
    venues: VenuesConfig = msgspec.field(default_factory=VenuesConfig)
    strategy: StrategySectionConfig = msgspec.field(default_factory=StrategySectionConfig)
    risk: RiskSectionConfig = msgspec.field(default_factory=RiskSectionConfig)
    execution: ExecutionSectionConfig = msgspec.field(default_factory=ExecutionSectionConfig)
    web: WebSectionConfig = msgspec.field(default_factory=WebSectionConfig)
    debug: DebugSectionConfig | None = None
