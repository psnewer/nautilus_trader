"""
赔率订阅服务配置

定义赔率订阅服务的配置参数。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OddsSubscriptionConfig:
    """
    赔率订阅服务配置

    配置示例:
    ```yaml
    services:
      odds_subscription:
        enabled: true
        staleness_timeout_sec: 300
        orbitexch_zoom_level: 0.8
    ```
    """

    enabled: bool = True

    # 数据超时配置
    staleness_timeout_sec: int = 300  # 5分钟无数据更新则刷新

    # Polymarket 配置
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # OrbitExch 配置
    orbitexch_base_url: str = "https://www.orbitexch.com"
    orbitexch_zoom_level: float = 0.8  # 页面缩放比例
    orbitexch_page_refresh_sec: int = 600  # 10分钟刷新页面一次
    orbitexch_username: str = ""
    orbitexch_password: str = ""

    # 支持的市场类型
    supported_market_types: list[str] = field(
        default_factory=lambda: ["home", "draw", "away"]
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OddsSubscriptionConfig":
        """从字典创建配置实例"""
        supported_market_types = data.get("supported_market_types")
        if supported_market_types is None:
            supported_market_types = ["home", "draw", "away"]

        return cls(
            enabled=data.get("enabled", True),
            staleness_timeout_sec=data.get("staleness_timeout_sec", 300),
            polymarket_api_key=data.get("polymarket_api_key", ""),
            polymarket_api_secret=data.get("polymarket_api_secret", ""),
            polymarket_passphrase=data.get("polymarket_passphrase", ""),
            polymarket_ws_url=data.get(
                "polymarket_ws_url",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),
            orbitexch_base_url=data.get(
                "orbitexch_base_url", "https://www.orbitexch.com"
            ),
            orbitexch_zoom_level=data.get("orbitexch_zoom_level", 0.8),
            orbitexch_page_refresh_sec=data.get("orbitexch_page_refresh_sec", 600),
            orbitexch_username=data.get("orbitexch_username", ""),
            orbitexch_password=data.get("orbitexch_password", ""),
            supported_market_types=supported_market_types,
        )
