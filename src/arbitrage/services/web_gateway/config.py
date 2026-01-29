"""
Web Gateway 配置
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WebGatewayConfig:
    """Web Gateway 配置"""
    host: str = "127.0.0.1"
    port: int = 8080
    debug: bool = True
    title: str = "Arbitrage Dashboard"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebGatewayConfig":
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=data.get("port", 8080),
            debug=data.get("debug", True),
            title=data.get("title", "Arbitrage Dashboard"),
        )
