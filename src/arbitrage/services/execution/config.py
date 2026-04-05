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
    # Builder Settings creds (用于 Relayer merge/redeem)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    # CLOB API creds (从 private key 派生, 用于下单)
    polymarket_clob_api_key: str = ""
    polymarket_clob_secret: str = ""
    polymarket_clob_passphrase: str = ""
    polymarket_private_key: str = ""  # 用于签名订单
    polymarket_funder: str = ""  # funder 地址

    # Polymarket API 端点
    polymarket_clob_url: str = "https://clob.polymarket.com"

    # OrbitExch 配置
    orbitexch_api_url: str = "https://www.orbitexch.com/customer/api"
    orbitexch_default_persistence: str = "LAPSE"  # LAPSE 或 PERSIST

    # 执行参数
    default_order_type: str = "GTC"  # GTC, FOK, FAK
    discount: float = 1.0  # size 调整折扣系数
    take_off: float = 0.0  # size 调整从其他方向持仓中拿走的比例
    market_order_enabled: bool = False  # 是否强制按市价执行
    # 订单追踪参数
    tracking_timeout_sec: float = 30.0       # 追踪超时时间
    tracking_check_interval_sec: float = 5.0  # 追踪检查间隔
    max_failure_retries: int = 3             # 失败重试次数上限

    # Relayer 配置 (用于链上操作: merge/redeem)
    polymarket_relayer_url: str = "https://relayer-v2.polymarket.com/"
    polygon_rpc_url: str = "https://polygon-rpc.com/"

    # 健康检查
    health_check_interval_sec: float = 30.0  # 健康检查间隔

    # Post-session cleanup 开关
    cleanup_enabled: bool = True
    cleanup_merge_enabled: bool = True
    cleanup_claim_enabled: bool = True

    def update_from_dict(self, data: dict[str, Any]) -> None:
        """原地更新配置（保持引用不变）"""
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionConfig":
        """从字典创建配置实例"""
        return cls(
            enabled=data.get("enabled", True),
            polymarket_api_key=data.get("polymarket_api_key", ""),
            polymarket_api_secret=data.get("polymarket_api_secret", ""),
            polymarket_passphrase=data.get("polymarket_passphrase", ""),
            polymarket_clob_api_key=data.get("polymarket_clob_api_key", ""),
            polymarket_clob_secret=data.get("polymarket_clob_secret", ""),
            polymarket_clob_passphrase=data.get("polymarket_clob_passphrase", ""),
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
            discount=data.get("discount", 1.0),
            take_off=data.get("take_off", 0.0),
            market_order_enabled=data.get("market_order_enabled", False),
            tracking_timeout_sec=data.get("tracking_timeout_sec", 30.0),
            tracking_check_interval_sec=data.get("tracking_check_interval_sec", 5.0),
            max_failure_retries=data.get("max_failure_retries", 3),
            polymarket_relayer_url=data.get(
                "polymarket_relayer_url", "https://relayer-v2.polymarket.com/"
            ),
            polygon_rpc_url=data.get("polygon_rpc_url", "https://polygon-rpc.com/"),
            health_check_interval_sec=data.get("health_check_interval_sec", 30.0),
            cleanup_enabled=data.get("cleanup_enabled", True),
            cleanup_merge_enabled=data.get("cleanup_merge_enabled", True),
            cleanup_claim_enabled=data.get("cleanup_claim_enabled", True),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "polymarket_api_key": self.polymarket_api_key,
            "polymarket_api_secret": self.polymarket_api_secret,
            "polymarket_passphrase": self.polymarket_passphrase,
            "polymarket_clob_api_key": self.polymarket_clob_api_key,
            "polymarket_clob_secret": "***" if self.polymarket_clob_secret else "",
            "polymarket_clob_passphrase": "***" if self.polymarket_clob_passphrase else "",
            "polymarket_private_key": "***" if self.polymarket_private_key else "",
            "polymarket_funder": self.polymarket_funder,
            "polymarket_clob_url": self.polymarket_clob_url,
            "orbitexch_api_url": self.orbitexch_api_url,
            "orbitexch_default_persistence": self.orbitexch_default_persistence,
            "default_order_type": self.default_order_type,
            "discount": self.discount,
            "take_off": self.take_off,
            "market_order_enabled": self.market_order_enabled,
            "tracking_timeout_sec": self.tracking_timeout_sec,
            "tracking_check_interval_sec": self.tracking_check_interval_sec,
            "max_failure_retries": self.max_failure_retries,
            "polymarket_relayer_url": self.polymarket_relayer_url,
            "polygon_rpc_url": self.polygon_rpc_url,
            "health_check_interval_sec": self.health_check_interval_sec,
            "cleanup_enabled": self.cleanup_enabled,
            "cleanup_merge_enabled": self.cleanup_merge_enabled,
            "cleanup_claim_enabled": self.cleanup_claim_enabled,
        }
