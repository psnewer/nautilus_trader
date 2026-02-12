"""
策略模块

提供策略基类和具体策略实现。

使用方式：
1. 使用内置策略：DefaultStrategy（默认方向选择）、MaxRebateStrategy（最大返水率）
2. 自定义策略：继承 Strategy 基类，覆盖 select_direction 方法
"""

from .base import Strategy, StrategyResult
from .default import DefaultStrategy
from .max_rebate import MaxRebateStrategy

# 策略注册表
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "default": DefaultStrategy,
    "max-rebate": MaxRebateStrategy,
}


def get_strategy_class(strategy_name: str) -> type[Strategy] | None:
    """
    获取策略类

    Args:
        strategy_name: 策略名称

    Returns:
        策略类，未注册则返回 None
    """
    return STRATEGY_REGISTRY.get(strategy_name)


def register_strategy(name: str, strategy_class: type[Strategy]) -> None:
    """
    注册策略

    Args:
        name: 策略名称
        strategy_class: 策略类
    """
    STRATEGY_REGISTRY[name] = strategy_class


__all__ = [
    "Strategy",
    "StrategyResult",
    "DefaultStrategy",
    "STRATEGY_REGISTRY",
    "get_strategy_class",
    "register_strategy",
]
