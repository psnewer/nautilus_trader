"""Web 层:Step 7 只读监控 WebGatewayActor(详细设计 architectures/web/architecture.md)。"""

from src.arbitrage.web.actor import WebGatewayActor
from src.arbitrage.web.actor import WebGatewayConfig
from src.arbitrage.web.actor import WebGatewayDeps

__all__ = ["WebGatewayActor", "WebGatewayConfig", "WebGatewayDeps"]
