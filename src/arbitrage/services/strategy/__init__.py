"""
套利策略服务

提供插拔式的信号量计算、策略评估和机会检测功能。

架构：
- Signal: 信号量计算（rebate, multi-way, way-rebate 等）
- Strategy: 策略执行（信号顺序执行 + 方向选择）
- StrategyService: 服务层（管理比赛、赔率、机会检测）
"""

from .config import (
    SignalDefinition,
    StrategyDefinition,
    MatchConfig,
    StrategyServiceConfig,
)
from .service import StrategyService
from .strategies import (
    Strategy,
    StrategyResult,
    DefaultStrategy,
    MaxRebateStrategy,
    get_strategy_class,
    register_strategy,
    STRATEGY_REGISTRY,
)

__all__ = [
    # Config
    "SignalDefinition",
    "StrategyDefinition",
    "MatchConfig",
    "StrategyServiceConfig",
    # Service
    "StrategyService",
    # Strategies
    "Strategy",
    "StrategyResult",
    "DefaultStrategy",
    "MaxRebateStrategy",
    "get_strategy_class",
    "register_strategy",
    "STRATEGY_REGISTRY",
]
