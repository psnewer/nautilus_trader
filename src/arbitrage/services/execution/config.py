"""
执行服务配置

定义执行服务的配置参数。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionConfig:
    """
    执行服务配置

    配置示例:
    ```yaml
    services:
      execution:
        enabled: true
        polymarket:
          api_key: "xxx"
          api_secret: "xxx"
          passphrase: "xxx"
          private_key: "xxx"
        orbitexch:
          default_persistence: "LAPSE"
    ```
    """

    enabled: bool = True

    # Polymarket 配置
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_private_key: str = ""  # 用于签名订单
    polymarket_funder: str = ""  # funder 地址

    # Polymarket API 端点
    polymarket_clob_url: str = "https://clob.polymarket.com"

    # OrbitExch 配置
    orbitexch_api_url: str = "https://www.orbitexch.com/customer/api"
    orbitexch_default_persistence: str = "LAPSE"  # LAPSE 或 PERSIST

    # 执行参数
    default_order_type: str = "GTC"  # GTC, FOK, FAK
    max_retry_attempts: int = 3
    retry_delay_sec: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionConfig":
        """从字典创建配置实例"""
        return cls(
            enabled=data.get("enabled", True),
            polymarket_api_key=data.get("polymarket_api_key", ""),
            polymarket_api_secret=data.get("polymarket_api_secret", ""),
            polymarket_passphrase=data.get("polymarket_passphrase", ""),
            polymarket_private_key=data.get("polymarket_private_key", ""),
            polymarket_funder=data.get("polymarket_funder", ""),
            polymarket_clob_url=data.get(
                "polymarket_clob_url", "https://clob.polymarket.com"
            ),
            orbitexch_api_url=data.get(
                "orbitexch_api_url", "https://www.orbitexch.com/customer/api"
            ),
            orbitexch_default_persistence=data.get(
                "orbitexch_default_persistence", "LAPSE"
            ),
            default_order_type=data.get("default_order_type", "GTC"),
            max_retry_attempts=data.get("max_retry_attempts", 3),
            retry_delay_sec=data.get("retry_delay_sec", 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "polymarket_api_key": self.polymarket_api_key,
            "polymarket_api_secret": self.polymarket_api_secret,
            "polymarket_passphrase": self.polymarket_passphrase,
            "polymarket_private_key": "***" if self.polymarket_private_key else "",
            "polymarket_funder": self.polymarket_funder,
            "polymarket_clob_url": self.polymarket_clob_url,
            "orbitexch_api_url": self.orbitexch_api_url,
            "orbitexch_default_persistence": self.orbitexch_default_persistence,
            "default_order_type": self.default_order_type,
            "max_retry_attempts": self.max_retry_attempts,
            "retry_delay_sec": self.retry_delay_sec,
        }
