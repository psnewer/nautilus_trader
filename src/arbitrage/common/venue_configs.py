"""
平台级配置 dataclass

跨服务与 adapter 共享的 venue 级配置定义。被 market_discovery 服务
（构建 MarketDiscoveryConfig）和 nautilus_trader.adapters.* 中的 scraper 共用。
"""

from dataclasses import dataclass, field


@dataclass
class BrowserConfig:
    """Playwright 浏览器配置"""
    headless: bool = True
    user_data_dir: str | None = None
    timeout_ms: int = 30000


@dataclass
class PolymarketApiConfig:
    """Polymarket Gamma API 配置"""
    base_url: str = "https://gamma-api.polymarket.com"
    sports_endpoint: str = "/sports"
    events_endpoint: str = "/events"
    events_limit: int = 10000
    closed: bool = False


@dataclass
class SportConfig:
    """单个 sport 配置"""
    sport: str
    competitions: list[str] = field(default_factory=list)


@dataclass
class PolymarketVenueConfig:
    """Polymarket 平台配置"""
    enabled: bool = True
    api: PolymarketApiConfig = field(default_factory=PolymarketApiConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    sports: list[SportConfig] = field(default_factory=list)


@dataclass
class OrbitExchVenueConfig:
    """OrbitExch 平台配置"""
    enabled: bool = True
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    sports: list[SportConfig] = field(default_factory=list)


@dataclass
class SharpExchVenueConfig:
    """SharpExch 平台配置"""
    enabled: bool = True
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    sports: list[SportConfig] = field(default_factory=list)
