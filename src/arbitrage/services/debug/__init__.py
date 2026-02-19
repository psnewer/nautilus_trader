"""
Debug 测试框架

单一配置文件管理所有测试数据和变量覆盖。

使用示例:
    from src.arbitrage.services.debug import debug_manager, MockCategory

    # 加载配置
    debug_manager.load("debug_config.json")

    # 获取覆盖值
    price = debug_manager.get_override("polymarket_price", default=real_price)

    # 获取模拟数据
    positions = debug_manager.get_mock_data(MockCategory.POSITIONS)
"""

from .config import (
    DebugConfig,
    DebugManager,
    DebugOverride,
    MockCategory,
    MockDataItem,
    OverrideTarget,
    debug_manager,
)

__all__ = [
    "DebugConfig",
    "DebugManager",
    "DebugOverride",
    "MockCategory",
    "MockDataItem",
    "OverrideTarget",
    "debug_manager",
]
