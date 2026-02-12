"""
Multi-Way 信号

过滤套利方向，确保不在某平台某方向返水率为负时继续买入。

依赖：
- way_rebate: 各平台各结果的当前返水率（由 way_rebate 信号计算，基于已有订单）

逻辑：
- 遍历当前所有可执行套利方向
- 对于每个方向中的每条腿（leg），检查该平台该结果的 way_rebate
- 如果 way_rebate < 0（负返水），则该方向不允许执行，从列表中移除

示例：
- 假设 Polymarket 主胜 way_rebate = 0.05（正），主不胜 way_rebate = -0.02（负）
- 那么所有包含 "Polymarket 买 主胜" 的套利方向都应被移除
- 因为当前持仓在主不胜时亏损，不应继续加仓主胜方向
"""

from typing import Any

from .base import Signal, SignalResult, MatchContext, ArbitrageDirection


class MultiWaySignal(Signal):
    """
    Multi-Way 信号

    过滤套利方向，移除那些在某平台某结果 way_rebate 为负时仍要买入的方向。
    """

    @property
    def name(self) -> str:
        return "multi-way"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        过滤套利方向

        Args:
            context: 比赛上下文（需要 way_rebate 数据）
            params: 信号参数
                - strict: 是否严格模式，默认 True
                  - True: way_rebate < 0 时移除
                  - False: way_rebate <= threshold 时移除
                - threshold: 非严格模式下的阈值，默认 0

        Returns:
            计算结果
        """
        strict = params.get("strict", True)
        threshold = params.get("threshold", 0.0)

        # 如果没有 way_rebate 数据，直接通过（不过滤）
        if not context.way_rebate:
            return SignalResult(
                signal_name=self.name,
                satisfied=True,
                value=None,
                details={
                    "message": "No way_rebate data, skipping filter",
                    "directions_count": len(context.arbitrage_directions),
                },
            )

        original_count = len(context.arbitrage_directions)
        removed_directions = []

        def should_keep(direction: ArbitrageDirection) -> bool:
            """检查方向是否应保留"""
            for leg in direction.legs:
                venue = leg.venue.value  # "polymarket" or "orbitexch"
                outcome = leg.market_type  # "home", "draw", or "away"

                way_rebate = context.get_way_rebate(venue, outcome)

                if way_rebate is not None:
                    # 检查是否违反规则
                    if strict:
                        if way_rebate < 0:
                            removed_directions.append({
                                "direction_id": direction.direction_id,
                                "reason": f"{venue} {outcome} way_rebate={way_rebate:.4f} < 0",
                            })
                            return False
                    else:
                        if way_rebate <= threshold:
                            removed_directions.append({
                                "direction_id": direction.direction_id,
                                "reason": f"{venue} {outcome} way_rebate={way_rebate:.4f} <= {threshold}",
                            })
                            return False

            return True

        # 过滤方向
        removed_count = context.filter_directions(should_keep)

        # 信号满足条件：过滤后仍有可执行方向
        satisfied = len(context.arbitrage_directions) > 0

        return SignalResult(
            signal_name=self.name,
            satisfied=satisfied,
            value=len(context.arbitrage_directions),
            details={
                "original_count": original_count,
                "remaining_count": len(context.arbitrage_directions),
                "removed_count": removed_count,
                "removed_directions": removed_directions,
                "way_rebate": context.way_rebate,
            },
        )
