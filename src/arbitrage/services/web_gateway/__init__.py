"""
Web Gateway 服务

提供 Web 界面用于：
- 查看市场发现结果
- 查看市场匹配结果
- 管理配置
"""

from .app import create_app, WebGatewayService

__all__ = [
    "create_app",
    "WebGatewayService",
]
