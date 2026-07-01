"""
ArbConfig 顶层 msgspec schema(Q22 B 分组件 / Q24 JSON 1:1 兼容旧 `default_config.json`)。

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


# ─── Discovery ────────────────────────────────────────────────────────────


class SportFilter(msgspec.Struct, frozen=True, kw_only=True):
    """{sport: "Tennis", competitions: ["atp", ...]}。"""

    sport: str
    competitions: list[str] = msgspec.field(default_factory=list)


class VenueDiscoveryConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    sports: list[SportFilter] = msgspec.field(default_factory=list)


class DiscoveryConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    refresh_interval_secs: float = 10.0     # #59:MarketMatchingActor timer 轮询间隔(锁 10s;发现间隔在 data client update_instruments_interval_mins,与此解耦)
    polymarket: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)
    orbitexch: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)
    sharpexch: VenueDiscoveryConfig = msgspec.field(default_factory=VenueDiscoveryConfig)


# ─── Matching ─────────────────────────────────────────────────────────────


class MatchingConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    min_similarity: float = 1.0
    sport_aliases: dict[str, str] = msgspec.field(default_factory=dict)
    competition_aliases: dict[str, str] = msgspec.field(default_factory=dict)
    competition_max_matches: dict[str, int] = msgspec.field(default_factory=dict)


# ─── Venues ───────────────────────────────────────────────────────────────


class PolymarketSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
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
    user_address: str | None = None
    eoa_address: str | None = None
    # builder relayer(可选)
    builder_api_key: str | None = None
    builder_api_secret: str | None = None
    builder_passphrase: str | None = None


class OrbitExchSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    base_url: str = "https://www.orbitexch.com"
    api_url: str = "https://www.orbitexch.com/customer/api"
    zoom_level: float = 0.8
    page_load_timeout_sec: float = 120.0
    page_refresh_sec: int = 600
    # #109:competition 页 WS handler 内部 liveness timeout(超此无任何帧含心跳 → reload)。
    # 旧 `health_interval_sec`(HealthCheckLoop tick)随 #109 退役删除。
    staleness_timeout_sec: int = 300
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: str | None = None
    cdp_url: str | None = None
    # 凭证(env-only)
    username: str | None = None
    password: str | None = None
    # 下单
    default_persistence: str = "LAPSE"
    default_order_type: str = "GTC"
    discount: float = 1.0
    take_off: float = 0.0
    market_order_enabled: bool = False
    supported_market_types: list[str] = msgspec.field(
        default_factory=lambda: ["home", "draw", "away"],
    )


class SharpExchSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """SharpExch 接入参数 + 凭证(凭证字段由 env 注入,JSON 中应留 None)。"""

    enabled: bool = False
    base_url: str = "https://portal.sharpxch.com"
    login_url: str = "https://sharpxch.com/player/"
    api_url: str = "https://portal.sharpxch.com/customer/api"
    zoom_level: float = 0.8
    page_load_timeout_sec: float = 120.0
    page_refresh_sec: int = 600
    staleness_timeout_sec: int = 300
    headless: bool = True
    browser_type: str = "chromium"
    user_data_dir: str | None = None
    cdp_url: str | None = None
    # 凭证(env-only)
    username: str | None = None
    password: str | None = None
    # 下单
    default_persistence: str = "LAPSE"
    default_order_type: str = "GTC"
    discount: float = 1.0
    take_off: float = 0.0
    market_order_enabled: bool = False
    supported_market_types: list[str] = msgspec.field(
        default_factory=lambda: ["home", "draw", "away"],
    )


class VenuesConfig(msgspec.Struct, frozen=True, kw_only=True):
    polymarket: PolymarketSectionConfig = msgspec.field(default_factory=PolymarketSectionConfig)
    orbitexch: OrbitExchSectionConfig = msgspec.field(default_factory=OrbitExchSectionConfig)
    sharpexch: SharpExchSectionConfig = msgspec.field(default_factory=SharpExchSectionConfig)


# ─── Strategy(JSON 驱动嵌套 Condition 树,Q25)──────────────────────────


class SignalDefConfig(msgspec.Struct, frozen=True, kw_only=True):
    """用户定义信号:`{params: {...}, description: ""}`。"""

    params: dict = msgspec.field(default_factory=dict)
    description: str = ""


class StrategyJsonConfig(msgspec.Struct, frozen=True, kw_only=True):
    """单条 strategy 的 JSON 配置(Condition 树 + 可选补救树)。

    `arbitrage_tree` / `compensation_tree` 用 `dict | None`(loader 递归解析,见 slice 5)。
    """

    arbitrage_tree: dict | None = None
    compensation_tree: dict | None = None
    description: str = ""


class StrategyBindingConfig(msgspec.Struct, frozen=True, kw_only=True):
    """`scope` 格式:`pair:<pair_id>` / `competition:<name>` / `sport:<name>`。"""

    scope: str
    strategy_id: str


class StrategySectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    log_evaluations: bool = False
    signals: dict[str, SignalDefConfig] = msgspec.field(default_factory=dict)
    strategies: dict[str, StrategyJsonConfig] = msgspec.field(default_factory=dict)
    bindings: list[StrategyBindingConfig] = msgspec.field(default_factory=list)


# ─── Arbitrage / Risk / Execution / Debug ─────────────────────────────────


class ArbitrageSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """套利运行默认值。strategy params 显式配置时覆盖这些默认值。"""

    share: float = 22.5
    max_leg_share: float | None = None
    fx: float = 1.33


class RiskSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    execution_enabled: bool = True
    share: float | None = None  # legacy fallback;新配置用顶层 arbitrage.share
    max_leg_share: float | None = None  # legacy fallback;新配置用顶层 arbitrage.max_leg_share
    fx: float | None = None  # legacy fallback;新配置用顶层 arbitrage.fx
    match_tp: float = 0.05
    match_sl: float = -0.05
    global_sl: float = -0.10
    min_probability: float = 0.03
    max_probability: float = 0.97
    health_check_interval_sec: float = 120.0
    match_overrides: dict = msgspec.field(default_factory=dict)


class ExecutionSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    tracking_timeout_sec: float = 30.0
    tracking_check_interval_sec: float = 5.0
    max_failure_retries: int = 5
    # 注(#110):PM merge/redeem 改 NT 连续 position 对账驱动(无 HealthCheckLoop)→ 旧
    # `health_check_interval_sec`(PM tick 间隔)已删;对账节奏在 launcher `position_check_interval_secs`。
    cleanup_enabled: bool = True
    cleanup_merge_enabled: bool = True
    cleanup_claim_enabled: bool = True
    staleness_timeout_sec: int = 300
    # NT 连续对账周期(原硬编码在 launcher,#121 提出来可配):open=order/挂单对账(#111),
    # position=持仓对账(#110,驱动 PM merge/redeem)。改后需重启(LiveExecEngineConfig build 时定)。
    open_check_interval_secs: float = 300.0
    position_check_interval_secs: float = 300.0


class DebugSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """与 `src/arbitrage/debug/config.py:DebugConfig` schema 对齐;dispatcher 转换。"""

    enabled: bool = False
    overrides: dict = msgspec.field(default_factory=dict)
    mock_data: dict = msgspec.field(default_factory=dict)


class WebSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """Step 7 只读监控 WebGatewayActor(详细设计 architectures/web/architecture.md)。
    默认关闭;`host` 默认只绑本机,暴露公网需用户显式改。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    start_halted: bool = True  # #119:web 开启时 boot 即 HALTED,操作员点 Start 才放行(真金安全默认)


# ─── 顶层 ArbConfig ───────────────────────────────────────────────────────


class ArbConfig(msgspec.Struct, frozen=True, kw_only=True):
    arbitrage: ArbitrageSectionConfig = msgspec.field(default_factory=ArbitrageSectionConfig)
    discovery: DiscoveryConfig = msgspec.field(default_factory=DiscoveryConfig)
    matching: MatchingConfig = msgspec.field(default_factory=MatchingConfig)
    venues: VenuesConfig = msgspec.field(default_factory=VenuesConfig)
    strategy: StrategySectionConfig = msgspec.field(default_factory=StrategySectionConfig)
    risk: RiskSectionConfig = msgspec.field(default_factory=RiskSectionConfig)
    execution: ExecutionSectionConfig = msgspec.field(default_factory=ExecutionSectionConfig)
    web: WebSectionConfig = msgspec.field(default_factory=WebSectionConfig)
    debug: DebugSectionConfig | None = None
