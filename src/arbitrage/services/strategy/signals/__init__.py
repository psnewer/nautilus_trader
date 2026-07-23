"""
信号量模块

提供各类信号量的计算实现。
"""

from .base import ArbitrageAction
from .base import ArbitrageDirection
from .base import ArbitrageLeg
from .base import ArbitrageVenue
from .base import MatchContext
from .base import Signal
from .base import SignalResult
from .match_status import LiveSignal
from .mean_rebate import MeanRebateSignal
from .multi_way import MultiWaySignal
from .rebate import RebateSignal


# 信号类型注册表
SIGNAL_REGISTRY: dict[str, type[Signal]] = {
    "rebate": RebateSignal,
    "mean_rebate": MeanRebateSignal,
    "live": LiveSignal,
    "multi-way": MultiWaySignal,
}


def get_signal(signal_name: str) -> Signal | None:
    """
    获取信号量实例

    Args:
        signal_name: 信号名称

    Returns:
        信号量实例，未注册则返回 None
    """
    signal_class = SIGNAL_REGISTRY.get(signal_name)
    if signal_class:
        return signal_class()
    return None


def get_all_signal_names() -> list[str]:
    """获取所有已注册的信号名称"""
    return list(SIGNAL_REGISTRY.keys())


__all__ = [
    "Signal",
    "SignalResult",
    "MatchContext",
    "ArbitrageDirection",
    "ArbitrageLeg",
    "ArbitrageVenue",
    "ArbitrageAction",
    "RebateSignal",
    "MeanRebateSignal",
    "LiveSignal",
    "MultiWaySignal",
    "SIGNAL_REGISTRY",
    "get_signal",
    "get_all_signal_names",
]
