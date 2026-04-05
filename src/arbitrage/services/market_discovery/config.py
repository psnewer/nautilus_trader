"""
市场发现服务配置模型

配置路径: services.market_discovery
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrowserConfig:
    """Playwright 浏览器配置"""
    headless: bool = True
    user_data_dir: str | None = None
    timeout_ms: int = 30000


@dataclass
class PolymarketApiConfig:
    """Polymarket API 配置"""
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
class MatchingConfig:
    """匹配算法配置"""
    use_get_similar: bool = True
    prefer_exact_match: bool = True
    shorten_mode: bool = False


@dataclass
class VenueConfig:
    """所有平台配置容器"""
    polymarket: PolymarketVenueConfig = field(default_factory=PolymarketVenueConfig)
    orbitexch: OrbitExchVenueConfig = field(default_factory=OrbitExchVenueConfig)


@dataclass
class MarketDiscoveryConfig:
    """
    市场发现服务配置

    配置示例:
    ```yaml
    services:
      market_discovery:
        enabled: true
        poll_interval_sec: 60
        venues:
          polymarket:
            enabled: true
            sports:
              - sport: soccer
                competitions:
                  - English Premier League
          orbitexch:
            enabled: true
            sports:
              - sport: soccer
                competitions:
                  - English Premier League
    ```
    """
    enabled: bool = True
    poll_interval_sec: int = 3600
    venues: VenueConfig = field(default_factory=VenueConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)

    def update_from_dict(self, data: dict[str, Any]) -> None:
        """原地更新配置（保持引用不变）"""
        new = MarketDiscoveryConfig.from_dict(data)
        self.enabled = new.enabled
        self.poll_interval_sec = new.poll_interval_sec
        self.venues = new.venues
        self.matching = new.matching

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketDiscoveryConfig":
        """从字典创建配置实例"""
        venues_data = data.get("venues", {})

        # 解析 polymarket 配置
        poly_data = venues_data.get("polymarket", {})
        poly_api = PolymarketApiConfig(**poly_data.get("api", {}))
        poly_browser = BrowserConfig(**poly_data.get("browser", {}))
        poly_sports = [
            SportConfig(**s) for s in poly_data.get("sports", [])
        ]
        polymarket_config = PolymarketVenueConfig(
            enabled=poly_data.get("enabled", True),
            api=poly_api,
            browser=poly_browser,
            sports=poly_sports,
        )

        # 解析 orbitexch 配置
        orbit_data = venues_data.get("orbitexch", {})
        orbit_browser = BrowserConfig(**orbit_data.get("browser", {}))
        orbit_sports = [
            SportConfig(**s) for s in orbit_data.get("sports", [])
        ]
        orbitexch_config = OrbitExchVenueConfig(
            enabled=orbit_data.get("enabled", True),
            browser=orbit_browser,
            sports=orbit_sports,
        )

        # 解析 matching 配置
        matching_data = data.get("matching", {})
        matching_config = MatchingConfig(**matching_data)

        return cls(
            enabled=data.get("enabled", True),
            poll_interval_sec=data.get("poll_interval_sec", 3600),
            venues=VenueConfig(
                polymarket=polymarket_config,
                orbitexch=orbitexch_config,
            ),
            matching=matching_config,
        )
