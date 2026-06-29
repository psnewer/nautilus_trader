"""套利运行参数。

这些参数不属于 Risk 门控本身,而是策略规划 / exposure 计算 / 货币换算共享的普通运行配置。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArbitrageParams:
    """Web Arbitrage 标签页配置的运行时默认值。"""

    share: float = 22.5
    max_leg_share: float | None = None
    fx: float = 1.33
