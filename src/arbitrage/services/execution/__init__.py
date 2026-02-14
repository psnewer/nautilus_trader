"""
订单执行服务

提供 Polymarket 和 OrbitExch 的订单执行功能。

功能:
1. 订单执行 (下单)
2. 订单撤销
3. 未成交部分按市价执行

架构:
- ExecutionService: 主服务，协调执行器
- PolymarketExecutor: Polymarket 执行器 (使用 py-clob-client)
- OrbitExchExecutor: OrbitExch 执行器 (使用 Playwright)
"""

from .config import ExecutionConfig
from .models import (
    Venue,
    OrderSide,
    OrderStatus,
    OrderType,
    Order,
    ExecutionResult,
    CancelResult,
)
from .service import ExecutionService
from .polymarket_executor import PolymarketExecutor
from .orbitexch_executor import OrbitExchExecutor

__all__ = [
    # Config
    "ExecutionConfig",
    # Models
    "Venue",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Order",
    "ExecutionResult",
    "CancelResult",
    # Service
    "ExecutionService",
    # Executors
    "PolymarketExecutor",
    "OrbitExchExecutor",
]
