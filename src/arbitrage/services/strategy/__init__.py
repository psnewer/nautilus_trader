"""
套利策略服务

提供插拔式的信号量计算、策略评估和机会检测功能。
"""

from .config import (
    SignalDefinition,
    StrategyDefinition,
    MatchConfig,
    StrategyServiceConfig,
)
from .service import StrategyService

__all__ = [
    "SignalDefinition",
    "StrategyDefinition",
    "MatchConfig",
    "StrategyServiceConfig",
    "StrategyService",
]
