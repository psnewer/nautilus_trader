"""
策略基类

定义策略的抽象接口和默认实现。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..signals import SignalResult, MatchContext, ArbitrageDirection


@dataclass
class StrategyResult:
    """
    策略执行结果

    Attributes:
        strategy_name: 策略名称
        triggered: 是否触发
        signal_results: 各信号的计算结果
        selected_direction: 选择的套利方向
        details: 详细信息
    """
    strategy_name: str
    triggered: bool
    signal_results: dict[str, SignalResult] = field(default_factory=dict)
    selected_direction: ArbitrageDirection | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "strategy_name": self.strategy_name,
            "triggered": self.triggered,
            "signal_results": {
                name: result.to_dict()
                for name, result in self.signal_results.items()
            },
            "selected_direction": (
                self.selected_direction.to_dict()
                if self.selected_direction else None
            ),
            "details": self.details,
        }


class Strategy(ABC):
    """
    策略抽象基类

    策略负责：
    1. 按顺序执行信号（signals）
    2. 在所有信号满足后，选择最优套利方向

    子类可以覆盖 `select_direction` 方法来自定义方向选择逻辑。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称"""
        pass

    @property
    def signals(self) -> list[str]:
        """
        策略包含的信号列表（按执行顺序）

        子类应覆盖此属性定义信号列表。
        默认返回空列表。
        """
        return []

    def execute(
        self,
        context: MatchContext,
        signal_calculator: callable,
    ) -> StrategyResult:
        """
        执行策略

        按顺序执行信号，任意信号返回 satisfied=False 则停止。
        所有信号满足后，调用 select_direction 选择最优方向。

        Args:
            context: 比赛上下文
            signal_calculator: 信号计算函数 (signal_name, context) -> SignalResult

        Returns:
            策略执行结果
        """
        signal_results: dict[str, SignalResult] = {}
        all_satisfied = True

        # 顺序执行信号
        for signal_name in self.signals:
            result = signal_calculator(signal_name, context)

            if result:
                signal_results[signal_name] = result

            if not result or not result.satisfied:
                all_satisfied = False
                break  # 立即停止

        # 如果所有信号满足，选择最优方向
        selected_direction = None
        if all_satisfied and context.arbitrage_directions:
            selected_direction = self.select_direction(context)

        return StrategyResult(
            strategy_name=self.name,
            triggered=all_satisfied,
            signal_results=signal_results,
            selected_direction=selected_direction,
            details={
                "directions_count": len(context.arbitrage_directions),
            },
        )

    def select_direction(self, context: MatchContext) -> ArbitrageDirection | None:
        """
        从多个套利方向中选择最优的一个

        默认选择逻辑（优先级降序，逐步筛选）：
        1. 平台维度持仓返水率为负的一方（需要平衡持仓，仅筛负的方向）
        2. 当前持仓返水率最小的一方（合并两平台，仅筛对应方向）
        3. rebate 信号计算出返水率最大的一方（在剩余方向中选最大）

        子类可以覆盖此方法实现自定义选择逻辑。

        Args:
            context: 比赛上下文

        Returns:
            选择的套利方向，无方向时返回 None
        """
        directions = context.arbitrage_directions
        if not directions:
            return None

        if len(directions) == 1:
            return directions[0]

        def get_venue_way_rebate(direction: ArbitrageDirection) -> float | None:
            market_type = direction.rebate_market
            rebate_venue = direction.rebate_venue.value
            if not context.way_rebate_by_venue:
                return None
            return context.way_rebate_by_venue.get(rebate_venue, {}).get(market_type)

        # 优先级 1：仅保留平台维度负返水方向
        negative_directions = [
            d for d in directions
            if (get_venue_way_rebate(d) is not None and get_venue_way_rebate(d) < 0)
        ]
        candidates = negative_directions if negative_directions else list(directions)

        # 优先级 2：按合并维度最小返水筛方向
        if context.way_rebate:
            outcomes = {d.rebate_market for d in candidates}
            valid_outcomes = {
                outcome: rebate
                for outcome, rebate in context.way_rebate.items()
                if outcome in outcomes
            }
            if valid_outcomes:
                min_outcome = min(valid_outcomes, key=valid_outcomes.get)
                candidates = [d for d in candidates if d.rebate_market == min_outcome]

        # 优先级 3：按 rebate_rate 最大选择
        best_direction = max(candidates, key=lambda d: d.rebate_rate)

        # 更新 context，只保留选中的方向
        context.arbitrage_directions = [best_direction]

        return best_direction
