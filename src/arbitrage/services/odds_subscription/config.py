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
    pair_activity_timeout_sec: int = 300  # 活跃互斥超时，避免异常导致一直阻塞

    # Polymarket 配置
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # OrbitExch 配置
    orbitexch_base_url: str = "https://www.orbitexch.com"
    orbitexch_zoom_level: float = 0.8  # 页面缩放比例
    orbitexch_page_refresh_sec: int = 600  # 10分钟刷新页面一次
    orbitexch_staleness_timeout_sec: int = 60  # 赔率超时时间，超过该时间无更新则刷新页面
    orbitexch_username: str = ""
    orbitexch_password: str = ""
    orbitexch_user_data_dir: str = ""  # 浏览器用户数据目录（用于持久化登录状态）
    orbitexch_cdp_url: str = ""  # 连接到已存在的 Chrome（如 http://localhost:9222）

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
            pair_activity_timeout_sec=data.get("pair_activity_timeout_sec", 300),
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
            orbitexch_staleness_timeout_sec=data.get("orbitexch_staleness_timeout_sec", 60),
            orbitexch_username=data.get("orbitexch_username", ""),
            orbitexch_password=data.get("orbitexch_password", ""),
            orbitexch_user_data_dir=data.get("orbitexch_user_data_dir", ""),
            orbitexch_cdp_url=data.get("orbitexch_cdp_url", ""),
            supported_market_types=supported_market_types,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "staleness_timeout_sec": self.staleness_timeout_sec,
            "pair_activity_timeout_sec": self.pair_activity_timeout_sec,
            "polymarket_api_key": self.polymarket_api_key,
            "polymarket_api_secret": self.polymarket_api_secret,
            "polymarket_passphrase": self.polymarket_passphrase,
            "polymarket_ws_url": self.polymarket_ws_url,
            "orbitexch_base_url": self.orbitexch_base_url,
            "orbitexch_zoom_level": self.orbitexch_zoom_level,
            "orbitexch_page_refresh_sec": self.orbitexch_page_refresh_sec,
            "orbitexch_staleness_timeout_sec": self.orbitexch_staleness_timeout_sec,
            "orbitexch_username": self.orbitexch_username,
            "orbitexch_password": self.orbitexch_password,
            "orbitexch_user_data_dir": self.orbitexch_user_data_dir,
            "orbitexch_cdp_url": self.orbitexch_cdp_url,
            "supported_market_types": self.supported_market_types,
        }
