"""
ArbConfig → 各组件 config 的纯函数派发(slice 4)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §6`。

**纯函数**:无副作用、无 I/O,易测;每个 `to_*` 接 `ArbConfig`,返对应组件配置 / 参数。

不在本模块的事:
- 凭证检查(loader 阶段 ConfigWarning 已处理;下游 client 构造时按需 raise)
- provider 内部 alias 应用逻辑(本模块只把 aliases 派发进 ArbContext keyed map)
- Strategy JSON → Condition 树解析细节(`to_strategy_registry` 只调用 loader)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClientConfig
from nautilus_trader.adapters.sharpexch.config import SharpExchDataClientConfig
from nautilus_trader.adapters.sharpexch.config import SharpExchExecClientConfig
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import SHARPEXCH
from src.arbitrage.common.venues import SPORTS_CLIENT
from src.arbitrage.common.venues import descriptor_for
from src.arbitrage.common.venues import enabled_tradable_venue_ids
from src.arbitrage.common.venues import enabled_tradable_venues
from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.config.schema import ConfigError
from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.matching.actor import MarketMatchingConfig
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.strategy.actor import StrategyEvaluatorConfig
from src.arbitrage.strategy.json_loader import build_strategy_registry
from src.arbitrage.strategy.registry import StrategyRegistry
from src.arbitrage.web.actor import WebGatewayConfig


if TYPE_CHECKING:
    pass


# ─── Venues ───────────────────────────────────────────────────────────────


def _polymarket_ws_base_url(url: str | None) -> str | None:
    """输出 NT 上游 PolymarketWebSocketClient 期望的 base URL。"""
    if url is None:
        return None
    normalized = url.rstrip("/")
    for suffix in ("/market", "/user"):
        if normalized.endswith(suffix):
            raise ConfigError(
                "venues.polymarket.ws_url must be the base websocket URL ending in /ws, "
                f"not a channel endpoint ending in {suffix}",
            )
    return normalized.rstrip("/") + "/"


def to_polymarket_data_client_config(cfg: ArbConfig) -> PolymarketDataClientConfig:
    pm = cfg.venues.polymarket
    return PolymarketDataClientConfig(
        private_key=pm.private_key,
        signature_type=pm.signature_type,
        funder=pm.funder,
        api_key=pm.clob_api_key,
        api_secret=pm.clob_api_secret,
        passphrase=pm.clob_passphrase,
        base_url_http=pm.clob_url,
        base_url_ws=_polymarket_ws_base_url(pm.ws_url),
        proxy_url=pm.proxy_url,
    )


def to_sports_data_client_config(cfg: ArbConfig) -> PolymarketSportsDataClientConfig:
    """#60:PM Sports 比分 firehose client config（公开 WS,无凭证;端点用默认)。"""
    sports = cfg.data_sources.sports_status
    return PolymarketSportsDataClientConfig(
        sports_ws_url=sports.ws_url,
        proxy_url=cfg.venues.polymarket.proxy_url,
    )


def to_polymarket_exec_client_config(cfg: ArbConfig) -> PolymarketExecClientConfig:
    pm = cfg.venues.polymarket
    return PolymarketExecClientConfig(
        private_key=pm.private_key,
        signature_type=pm.signature_type,
        funder=pm.funder,
        api_key=pm.clob_api_key,
        api_secret=pm.clob_api_secret,
        passphrase=pm.clob_passphrase,
        base_url_http=pm.clob_url,
        base_url_ws=_polymarket_ws_base_url(pm.ws_url),
        proxy_url=pm.proxy_url,
        max_retries=pm.max_retries,
        retry_delay_initial_ms=pm.retry_delay_initial_ms,
        retry_delay_max_ms=pm.retry_delay_max_ms,
    )


def to_orbitexch_data_client_config(cfg: ArbConfig) -> OrbitExchDataClientConfig:
    oe = cfg.venues.orbitexch
    # OE Config 把 username/password 标为 required str(非 Optional);env 缺失时转为空串
    # 让下游 BrowserManager / login 流程触发明确错误(loader 不预判)
    return OrbitExchDataClientConfig(
        username=oe.username or "",
        password=oe.password or "",
        base_url=oe.base_url,
        headless=oe.headless,
        browser_type=oe.browser_type,
        user_data_dir=oe.user_data_dir,
        proxy_url=oe.proxy_url,
        page_timeout=int(oe.page_load_timeout_sec * 1000),
        # #109:OE 无 HealthCheckLoop;`staleness_timeout_secs` 改作 WS handler 内部 liveness timeout。
        staleness_timeout_secs=oe.staleness_timeout_sec,
    )


def to_orbitexch_exec_client_config(cfg: ArbConfig) -> OrbitExchExecClientConfig:
    oe = cfg.venues.orbitexch
    return OrbitExchExecClientConfig(
        username=oe.username or "",
        password=oe.password or "",
        base_url=oe.base_url,
        headless=oe.headless,
        browser_type=oe.browser_type,
        user_data_dir=oe.user_data_dir,
        proxy_url=oe.proxy_url,
        page_timeout=int(oe.page_load_timeout_sec * 1000),
    )


def to_sharpexch_data_client_config(cfg: ArbConfig) -> SharpExchDataClientConfig:
    se = cfg.venues.sharpexch
    return SharpExchDataClientConfig(
        username=se.username or "",
        password=se.password or "",
        base_url=se.base_url,
        login_url=se.login_url,
        headless=se.headless,
        browser_type=se.browser_type,
        user_data_dir=se.user_data_dir,
        proxy_url=se.proxy_url,
        page_timeout=int(se.page_load_timeout_sec * 1000),
        staleness_timeout_secs=se.staleness_timeout_sec,
    )


def to_sharpexch_exec_client_config(cfg: ArbConfig) -> SharpExchExecClientConfig:
    se = cfg.venues.sharpexch
    return SharpExchExecClientConfig(
        username=se.username or "",
        password=se.password or "",
        base_url=se.base_url,
        login_url=se.login_url,
        headless=se.headless,
        browser_type=se.browser_type,
        user_data_dir=se.user_data_dir,
        proxy_url=se.proxy_url,
        page_timeout=int(se.page_load_timeout_sec * 1000),
        cloudflare_timeout=int(se.cloudflare_timeout_sec * 1000),
    )


# ─── Actors ───────────────────────────────────────────────────────────────


# #59(slice A):to_instrument_refresher_configs 删除 —— InstrumentRefresher 退役,
# 周期发现迁 DataClient 原生 `_update_instruments`(见 refactor.md §5.2.3/#59)。


def to_market_matching_actor_config(cfg: ArbConfig) -> MarketMatchingConfig:
    """注意:`sport_aliases` / `competition_aliases` 不在 MarketMatchingConfig。

    aliases 经 `to_arb_context_init_kwargs` 派发给 provider/discovery keyed map,matching actor
    只消费 provider 写入 instrument.info 后的标准化字段。
    """
    m = cfg.matching
    return MarketMatchingConfig(
        anchor_venue=SPORTS_CLIENT,
        tradable_venues=enabled_tradable_venue_ids(cfg),
        refresh_interval_secs=cfg.discovery.refresh_interval_secs,
        competition_max_matches=m.competition_max_matches or None,
    )


def to_strategy_evaluator_config(cfg: ArbConfig) -> StrategyEvaluatorConfig:
    """StrategyEvaluatorConfig 只有 `log_evaluations`;其余(registries / store / portfolio /
    is_execution_active / loop)经 `_RuntimeDeps` 注入,launcher 装配。"""
    return StrategyEvaluatorConfig(log_evaluations=cfg.strategy.log_evaluations)


def to_web_gateway_config(cfg: ArbConfig) -> WebGatewayConfig:
    """WebGatewayConfig 只有 enabled/host/port;portfolio / loop 经 `WebGatewayDeps` 注入,launcher 装配。"""
    w = cfg.web
    return WebGatewayConfig(enabled=w.enabled, host=w.host, port=w.port)


def to_strategy_registry(cfg: ArbConfig) -> StrategyRegistry:
    """JSON `strategies` + `bindings` → `StrategyRegistry`。

    **前置**:用户必须先调 `register_check(name, cls)` / `register_action(name, cls)` 注册
    具体 Check/Action 类(slice 9 用户域)。未注册的 type 名 → `StrategyConfigError`。

    `strategy.enabled=false` → 空 registry:保留 StrategyEvaluator 作为 MatchedPair→OBD 订阅桥,
    但不挂任何策略,因此不评估、不触发 Action。

    空 bindings → 空 registry(launcher 仍能起 connect/discovery/matching,
    `StrategyEvaluator` no-op evaluate)。
    """
    if not cfg.strategy.enabled:
        return StrategyRegistry()
    return build_strategy_registry(cfg.strategy)


# ─── ArbContext / Risk / Debug ────────────────────────────────────────────


def to_arb_risk_params(cfg: ArbConfig) -> ArbRiskParams:
    r = cfg.risk
    return ArbRiskParams(
        match_tp=r.match_tp,
        match_sl=r.match_sl,
        min_probability=r.min_probability,
        max_probability=r.max_probability,
    )


def to_arbitrage_params(cfg: ArbConfig) -> ArbitrageParams:
    a = cfg.arbitrage
    return ArbitrageParams(
        share=a.share,
        max_leg_share=a.max_leg_share,
        fx=a.fx,
    )


def to_arb_context_init_kwargs(cfg: ArbConfig) -> dict:
    """`prepare_arb_context(**dict)` 用;launcher 后续补 `venue_liveness` / `pair_registry` /
    `pm_settlement` 等运行时件。

    只返回 venue/data-source keyed map;旧 `pm_*` / `oe_*` / `se_*` 投影已删除。
    """
    tracking_timeout = cfg.execution.tracking_timeout_sec
    tradable_venues = enabled_tradable_venue_ids(cfg)
    sport_aliases = dict(cfg.matching.sport_aliases)
    competition_aliases = dict(cfg.matching.competition_aliases)
    discovery_by_venue = _discovery_config_by_enabled_venue(cfg)
    data_source_competitions = {SPORTS_CLIENT: to_sports_status_target_competitions(cfg)}
    data_source_comp_to_sport = {SPORTS_CLIENT: to_sports_status_competition_to_sport(cfg)}
    return {
        "session_timeout_secs_by_venue": {
            venue: tracking_timeout
            for venue in tradable_venues
        },
        "discovery_config_by_venue": discovery_by_venue,
        "sport_aliases_by_venue": {
            venue: sport_aliases
            for venue in tradable_venues
        },
        "competition_aliases_by_venue": {
            venue: competition_aliases
            for venue in tradable_venues
        },
        "target_competitions_by_data_source": data_source_competitions,
        "competition_to_sport_by_data_source": data_source_comp_to_sport,
    }


def _discovery_config_by_enabled_venue(cfg: ArbConfig) -> dict:
    """从 Venue Registry 的 discovery builder 派生 runtime discovery keyed map。"""
    discovery_by_venue = {}
    for descriptor in enabled_tradable_venues(cfg):
        builder_name = descriptor.discovery_config_builder
        if builder_name is None:
            continue
        builder = globals()[builder_name]
        discovery = builder(cfg)
        if discovery is not None:
            discovery_by_venue[descriptor.venue_id] = discovery
    return discovery_by_venue


def to_sports_status_target_competitions(cfg: ArbConfig) -> list:
    """PMSPORTS 目标 competitions 扁平化。

    `data_sources.sports_status.sports` 显式配置时优先;常规省略时按当前约定继承
    `discovery.polymarket.sports`,让 PMSPORTS discovery 与 PM sports 配置保持一致。
    """
    filters = _sports_status_filters(cfg)
    if not filters:
        return []
    tags = []
    for sf in filters:
        for comp in sf.competitions:
            if comp and comp not in tags:
                tags.append(comp)
    return tags


def to_sports_status_competition_to_sport(cfg: ArbConfig) -> dict:
    """PMSPORTS competition slug → sport 名,如 {"atp": "Tennis"};provider 写 `info["sport"]`。"""
    filters = _sports_status_filters(cfg)
    if not filters:
        return {}
    mapping = {}
    for sf in filters:
        for comp in sf.competitions:
            if comp:
                mapping[comp.lower()] = sf.sport
    return mapping


def _sports_status_filters(cfg: ArbConfig):
    """PMSPORTS discovery targets:显式 data source 配置优先,默认继承 PM sports 配置。"""
    sports = cfg.data_sources.sports_status
    if not sports.enabled:
        return []
    if sports.sports:
        return sports.sports
    if not cfg.discovery.polymarket.enabled:
        return []
    return cfg.discovery.polymarket.sports


def _enabled_venue_discovery_sections(cfg: ArbConfig, venue: str):
    """按 Venue Registry descriptor 读取 runtime venue + discovery section。"""
    descriptor = descriptor_for(venue)
    venue_section = getattr(cfg.venues, descriptor.config_key)
    discovery_section = getattr(cfg.discovery, descriptor.config_key)
    if not venue_section.enabled or not discovery_section.enabled:
        return None
    return venue_section, discovery_section


def _browser_venue_discovery_config(cfg: ArbConfig, venue: str, config_cls):
    """OE 型 browser discovery config:免登录、后台 headless、按 discovery.sports 过滤。"""
    from src.arbitrage.common.venue_configs import BrowserConfig
    from src.arbitrage.common.venue_configs import SportConfig

    sections = _enabled_venue_discovery_sections(cfg, venue)
    if sections is None:
        return None
    venue_section, discovery_section = sections
    return config_cls(
        enabled=True,
        # discovery/scraper 独立浏览器,免登录、定时后台跑:强制 headless 且不用登录 profile,
        # 避免和 data/exec 的登录会话抢同一 persistent 目录。
        browser=BrowserConfig(
            headless=True,
            user_data_dir=None,
            timeout_ms=int(venue_section.page_load_timeout_sec * 1000),
        ),
        sports=[SportConfig(sport=s.sport, competitions=list(s.competitions)) for s in discovery_section.sports],
    )


def to_oe_scraper_config(cfg: ArbConfig):
    """`ArbConfig.discovery.orbitexch.sports` + `cfg.venues.orbitexch.{headless,user_data_dir,page_load_timeout_sec}`
    → `OrbitExchVenueConfig`(scraper 用)。

    返 `OrbitExchVenueConfig | None`;`enabled=False` → None(让 factory 装空 Provider)。
    """
    from src.arbitrage.common.venue_configs import OrbitExchVenueConfig

    return _browser_venue_discovery_config(cfg, ORBITEXCH, OrbitExchVenueConfig)


def to_se_discovery_config(cfg: ArbConfig):
    """`ArbConfig.discovery.sharpexch.sports` + `cfg.venues.sharpexch.*`
    → `SharpExchVenueConfig | None`。

    供 SE provider/factory 构造 discovery client;`enabled=False` 时返回 None。
    """
    from src.arbitrage.common.venue_configs import SharpExchVenueConfig

    return _browser_venue_discovery_config(cfg, SHARPEXCH, SharpExchVenueConfig)


def to_debug_config(cfg: ArbConfig) -> DebugConfig | None:
    """`debug` 段缺 / `enabled=False` → None(launcher 装生产链路)。"""
    if cfg.debug is None or not cfg.debug.enabled:
        return None
    dbg = DebugConfig(enabled=True)
    for name, raw in (cfg.debug.overrides or {}).items():
        dbg.overrides[name] = DebugOverride(
            name=name,
            enabled=bool(raw.get("enabled", False)),
            value=raw.get("value"),
            description=raw.get("description", ""),
        )
    for mid, raw in (cfg.debug.mock_data or {}).items():
        dbg.mock_data[mid] = MockDataItem(
            id=mid,
            category=MockCategory(raw["category"]),
            name=raw.get("name", ""),
            enabled=bool(raw.get("enabled", False)),
            data=raw.get("data"),
            conditions=raw.get("conditions") or {},
            priority=int(raw.get("priority", 0)),
        )
    return dbg
