"""
最大返水率策略

自定义方向选择：始终选择返水率最高的方向，忽略持仓情况。
"""

from .base import Strategy, StrategyResult
from ..signals import MatchContext, ArbitrageDirection


class MaxRebateStrategy(Strategy):
    """
    最大返水率策略

    自定义方向选择逻辑：
    - 始终选择 rebate_rate 最高的方向
    - 忽略当前持仓的 way_rebate

    适用场景：
    - 新开仓，没有历史持仓
    - 不考虑持仓平衡的激进策略
    """

    def __init__(self, name: str = "max-rebate", signals: list[str] | None = None):
        self._name = name
        self._signals = signals or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def signals(self) -> list[str]:
        return self._signals

    def select_direction(self, context: MatchContext) -> ArbitrageDirection | None:
        """
        选择返水率最高的方向

        覆盖基类的默认逻辑，只考虑 rebate_rate。

        Args:
            context: 比赛上下文

        Returns:
            返水率最高的套利方向
        """
        directions = context.arbitrage_directions
        if not directions:
            return None

        # 按 rebate_rate 降序排序
        best_direction = max(directions, key=lambda d: d.rebate_rate)

        # 更新 context
        context.arbitrage_directions = [best_direction]

        return best_direction
