"""
ArbConfig → 各组件 config 的纯函数派发(slice 4)。

设计见 `docs/arbitrage/architectures/_cross-cutting/configuration.md §6`。

**纯函数**:无副作用、无 I/O,易测;每个 `to_*` 接 `ArbConfig`,返对应组件配置 / 参数。

不在本模块的事:
- 凭证检查(loader 阶段 ConfigWarning 已处理;下游 client 构造时按需 raise)
- aliases → Provider 层接线(待 slice 7;normalizer 注释:Provider 填 info 时已 alias)
- Strategy JSON → Condition 树解析(待 slice 5;`to_strategy_registry` 暂留 stub)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsDataClientConfig

from src.arbitrage.config.schema import ArbConfig
from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.matching.actor import MarketMatchingConfig
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.strategy.actor import StrategyEvaluatorConfig
from src.arbitrage.strategy.json_loader import build_strategy_registry
from src.arbitrage.strategy.registry import StrategyRegistry

if TYPE_CHECKING:
    pass


# ─── Venues ───────────────────────────────────────────────────────────────


def _polymarket_ws_base_url(url: str | None) -> str | None:
    """兼容旧配置的 full endpoint,输出 NT 上游 PolymarketWebSocketClient 期望的 base URL。"""
    if url is None:
        return None
    normalized = url.rstrip("/")
    for suffix in ("/market", "/user"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized.rstrip("/") + "/"


def to_polymarket_data_client_config(cfg: ArbConfig) -> PolymarketDataClientConfig:
    pm = cfg.venues.polymarket
    return PolymarketDataClientConfig(
        private_key=pm.private_key,
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
    return PolymarketSportsDataClientConfig()


def to_polymarket_exec_client_config(cfg: ArbConfig) -> PolymarketExecClientConfig:
    pm = cfg.venues.polymarket
    return PolymarketExecClientConfig(
        private_key=pm.private_key,
        funder=pm.funder,
        api_key=pm.clob_api_key,
        api_secret=pm.clob_api_secret,
        passphrase=pm.clob_passphrase,
        base_url_http=pm.clob_url,
        base_url_ws=_polymarket_ws_base_url(pm.ws_url),
        proxy_url=pm.proxy_url,
    )


def to_orbitexch_data_client_config(cfg: ArbConfig) -> OrbitExchDataClientConfig:
    oe = cfg.venues.orbitexch
    # OE Config 把 username/password 标为 required str(非 Optional);env 缺失时回退空串
    # 让下游 BrowserManager / login 流程触发明确错误(loader 不预判)
    return OrbitExchDataClientConfig(
        username=oe.username or "",
        password=oe.password or "",
        base_url=oe.base_url,
        headless=oe.headless,
        browser_type=oe.browser_type,
        user_data_dir=oe.user_data_dir,
        page_timeout=int(oe.page_load_timeout_sec * 1000),
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
        page_timeout=int(oe.page_load_timeout_sec * 1000),
    )


# ─── Actors ───────────────────────────────────────────────────────────────


# #59(slice A):to_instrument_refresher_configs 删除 —— InstrumentRefresher 退役,
# 周期发现迁 DataClient 原生 `_update_instruments`(见 refactor.md §5.2.3/#59)。


def to_market_matching_actor_config(cfg: ArbConfig) -> MarketMatchingConfig:
    """注意:`sport_aliases` / `competition_aliases` 不在 MarketMatchingConfig
    (normalizer 注释:Provider 填 info 时已 alias);aliases → Provider 的接线见 slice 7。"""
    m = cfg.matching
    return MarketMatchingConfig(
        refresh_interval_secs=cfg.discovery.refresh_interval_secs,
        min_similarity=int(m.min_similarity),
        competition_max_matches=m.competition_max_matches or None,
    )


def to_strategy_evaluator_config(cfg: ArbConfig) -> StrategyEvaluatorConfig:
    """StrategyEvaluatorConfig 只有 `log_evaluations`;其余(registries / store / portfolio /
    is_execution_active / loop / signal_collector)经 `_RuntimeDeps` 注入,launcher 装配。"""
    return StrategyEvaluatorConfig(log_evaluations=cfg.strategy.log_evaluations)


def to_strategy_registry(cfg: ArbConfig) -> StrategyRegistry:
    """JSON `strategies` + `bindings` → `StrategyRegistry`。

    **前置**:用户必须先调 `register_check(name, cls)` / `register_action(name, cls)` 注册
    具体 Check/Action 类(slice 9 用户域)。未注册的 type 名 → `StrategyConfigError`。

    空 bindings → 空 registry(launcher 仍能起 connect/discovery/matching,
    `StrategyEvaluator` no-op evaluate)。
    """
    return build_strategy_registry(cfg.strategy)


# ─── ArbContext / Risk / Debug ────────────────────────────────────────────


def to_arb_risk_params(cfg: ArbConfig) -> ArbRiskParams:
    r = cfg.risk
    return ArbRiskParams(
        share=r.share,
        fx=r.fx,
        match_tp=r.match_tp,
        match_sl=r.match_sl,
        global_sl=r.global_sl,
    )


def to_arb_context_init_kwargs(cfg: ArbConfig) -> dict:
    """`prepare_arb_context(**dict)` 用;launcher 后续补 `leg_settled` / `pair_registry` /
    `pm_settlement` / `pm_positions_fetcher` 等运行时件。

    slice 7A:加 `oe_scraper_config` / `oe_sport_aliases` / `oe_competition_aliases`(供
    OE data factory 构造真 scraper + Provider 写 info 时查表)。
    #55:`pm_event_slug_tags`(目标 competition 列表,如 ["atp"];PM `/sports` 的 `sport` 字段比对)
    + `pm_competition_to_sport`(competition→sport map,如 {"atp": "Tennis"};provider 写 info["sport"])。
    """
    return {
        "pm_session_timeout_secs": cfg.execution.tracking_timeout_sec,
        "pm_health_interval_secs": cfg.execution.health_check_interval_sec,
        "oe_session_timeout_secs": cfg.execution.tracking_timeout_sec,
        "oe_health_interval_secs": cfg.execution.health_check_interval_sec,
        "oe_scraper_config": to_oe_scraper_config(cfg),
        "oe_sport_aliases": dict(cfg.matching.sport_aliases),
        "oe_competition_aliases": dict(cfg.matching.competition_aliases),
        "pm_event_slug_tags": to_pm_event_slug_tags(cfg),
        "pm_competition_to_sport": to_pm_competition_to_sport(cfg),
    }


def to_pm_event_slug_tags(cfg: ArbConfig) -> list:
    """#55:`discovery.polymarket.sports[].competitions` 扁平化为目标 competition 列表;
    各 competition 字符串作为 PM `/sports` 的 `sport` 字段比对值(用户 config 用 PM 命名如 "atp" / "epl")。"""
    if not cfg.discovery.polymarket.enabled:
        return []
    tags = []
    for sf in cfg.discovery.polymarket.sports:
        for comp in sf.competitions:
            if comp and comp not in tags:
                tags.append(comp)
    return tags


def to_pm_competition_to_sport(cfg: ArbConfig) -> dict:
    """#55:competition(PM 缩写)→ sport 名,如 {"atp": "Tennis"};provider 写 `info["sport"]`。"""
    if not cfg.discovery.polymarket.enabled:
        return {}
    mapping = {}
    for sf in cfg.discovery.polymarket.sports:
        for comp in sf.competitions:
            if comp:
                mapping[comp.lower()] = sf.sport
    return mapping


def to_oe_scraper_config(cfg: ArbConfig):
    """`ArbConfig.discovery.orbitexch.sports` + `cfg.venues.orbitexch.{headless,user_data_dir,page_load_timeout_sec}`
    → `OrbitExchVenueConfig`(scraper 用)。

    返 `OrbitExchVenueConfig | None`;`enabled=False` → None(让 factory 装空 Provider)。
    """
    from src.arbitrage.common.venue_configs import BrowserConfig
    from src.arbitrage.common.venue_configs import OrbitExchVenueConfig
    from src.arbitrage.common.venue_configs import SportConfig

    oe_dis = cfg.discovery.orbitexch
    if not oe_dis.enabled:
        return None
    oe_venue = cfg.venues.orbitexch
    return OrbitExchVenueConfig(
        enabled=True,
        # #62:scraper 独立浏览器,**免登录、定时后台跑** —— 强制 headless(无需可见,也不干扰
        # data/exec 的登录会话)+ **不用登录 profile**(`user_data_dir=None`:免登录,且避免和 data BM
        # 抢同一 persistent 目录)。与 data/exec 的可见登录浏览器(共享单例,§6.2)解耦。
        browser=BrowserConfig(
            headless=True,
            user_data_dir=None,
            timeout_ms=int(oe_venue.page_load_timeout_sec * 1000),
        ),
        sports=[SportConfig(sport=s.sport, competitions=list(s.competitions)) for s in oe_dis.sports],
    )


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
