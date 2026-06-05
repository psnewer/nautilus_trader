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

    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    relayer_url: str = "https://relayer-v2.polymarket.com/"
    polygon_rpc_url: str = "https://polygon-rpc.com/"
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
    base_url: str = "https://www.orbitexch.com"
    api_url: str = "https://www.orbitexch.com/customer/api"
    zoom_level: float = 0.8
    page_load_timeout_sec: float = 60.0
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
    signals: dict[str, SignalDefConfig] = msgspec.field(default_factory=dict)
    strategies: dict[str, StrategyJsonConfig] = msgspec.field(default_factory=dict)
    bindings: list[StrategyBindingConfig] = msgspec.field(default_factory=list)


# ─── Risk / Execution / Debug ─────────────────────────────────────────────


class RiskSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    execution_enabled: bool = True
    share: float = 22.5
    fx: float = 1.33
    match_tp: float = 0.05
    match_sl: float = -0.05
    global_sl: float = -0.10
    health_check_interval_sec: float = 120.0
    match_overrides: dict = msgspec.field(default_factory=dict)


class ExecutionSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    enabled: bool = True
    tracking_timeout_sec: float = 30.0
    tracking_check_interval_sec: float = 5.0
    max_failure_retries: int = 5
    health_check_interval_sec: float = 120.0
    cleanup_enabled: bool = True
    cleanup_merge_enabled: bool = True
    cleanup_claim_enabled: bool = True
    staleness_timeout_sec: int = 300


class DebugSectionConfig(msgspec.Struct, frozen=True, kw_only=True):
    """与 `src/arbitrage/debug/config.py:DebugConfig` schema 对齐;dispatcher 转换。"""

    enabled: bool = False
    overrides: dict = msgspec.field(default_factory=dict)
    mock_data: dict = msgspec.field(default_factory=dict)


# ─── 顶层 ArbConfig ───────────────────────────────────────────────────────


class ArbConfig(msgspec.Struct, frozen=True, kw_only=True):
    discovery: DiscoveryConfig = msgspec.field(default_factory=DiscoveryConfig)
    matching: MatchingConfig = msgspec.field(default_factory=MatchingConfig)
    venues: VenuesConfig = msgspec.field(default_factory=VenuesConfig)
    strategy: StrategySectionConfig = msgspec.field(default_factory=StrategySectionConfig)
    risk: RiskSectionConfig = msgspec.field(default_factory=RiskSectionConfig)
    execution: ExecutionSectionConfig = msgspec.field(default_factory=ExecutionSectionConfig)
    debug: DebugSectionConfig | None = None
