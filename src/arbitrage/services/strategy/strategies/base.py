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

    @property
    def strict_filter(self) -> bool:
        """
        优先级筛选严格模式

        True: 优先级 1 筛选无结果时返回 None（不放行）
        False: 筛选无结果时全部放过到下一级（默认）

        子类可覆盖此属性。
        """
        return False

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

        triggered = all_satisfied
        if context.arbitrage_directions or selected_direction is None:
            triggered = all_satisfied and selected_direction is not None

        return StrategyResult(
            strategy_name=self.name,
            triggered=triggered,
            signal_results=signal_results,
            selected_direction=selected_direction,
            details={
                "directions_count": len(context.arbitrage_directions),
            },
        )

    def select_direction(self, context: MatchContext) -> ArbitrageDirection | None:
        """
        从多个套利方向中选择最优的一个

        默认选择逻辑（优先级降序）：
        1. 平台维度约束过滤：
           如果某平台存在持仓返水率 < 0 的方向，则不放通该平台持仓返水率 > 0 的方向
        2. 合并维度返水最小的方向（相同返水时按 rebate_rate 最大）
        3. 无持仓数据时，按 rebate_rate 最大选择

        当 strict_filter=True 时，优先级 1 筛选无结果则返回 None（不放行）。
        当 strict_filter=False 时，筛选无结果则全部放过到下一级（默认行为）。

        子类可以覆盖此方法实现自定义选择逻辑。

        Args:
            context: 比赛上下文

        Returns:
            选择的套利方向，无方向时返回 None
        """
        directions = context.arbitrage_directions
        if not directions:
            return None

        def get_leg_way_rebate(venue: str, market_type: str) -> float | None:
            if not context.way_rebate_by_venue:
                return None
            return context.way_rebate_by_venue.get(venue, {}).get(market_type)

        def venue_has_negative_rebate(venue: str) -> bool:
            if not context.way_rebate_by_venue:
                return False
            venue_rebates = context.way_rebate_by_venue.get(venue, {})
            return any(rebate < 0 for rebate in venue_rebates.values())

        def direction_passes_platform_filter(direction: ArbitrageDirection) -> bool:
            for leg in direction.legs:
                leg_rebate = get_leg_way_rebate(leg.venue.value, leg.market_type)
                if (
                    leg_rebate is not None
                    and leg_rebate > 0
                    and venue_has_negative_rebate(leg.venue.value)
                ):
                    return False
            return True

        def get_venue_way_rebate(direction: ArbitrageDirection) -> float | None:
            market_type = direction.rebate_market
            rebate_venue = direction.rebate_venue.value
            return get_leg_way_rebate(rebate_venue, market_type)

        # 优先级 1：平台维度过滤
        platform_filtered_directions = [
            d for d in directions if direction_passes_platform_filter(d)
        ]
        if platform_filtered_directions:
            directions = platform_filtered_directions
        elif self.strict_filter:
            context.arbitrage_directions = []
            return None

        candidates = list(directions)

        # 优先级 2：按合并维度返水最小选择（相同返水时按 rebate_rate 最大）
        if context.way_rebate:
            best_direction = min(
                candidates,
                key=lambda d: (context.way_rebate.get(d.rebate_market, float('inf')), -d.rebate_rate),
            )
        else:
            # 无持仓数据时，按 rebate_rate 最大选择
            best_direction = max(candidates, key=lambda d: d.rebate_rate)

        # 更新 context，只保留选中的方向
        context.arbitrage_directions = [best_direction]

        return best_direction
