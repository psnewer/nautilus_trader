"""
Multi-Way 信号

过滤套利方向，确保不在某方向持仓返水率为负时继续买入。

数据来源：
- context.way_rebate 由 StrategyService 在 _evaluate_match 时从 RiskService 获取
- RiskService 在服务启动时加载历史持仓，之后通过 add_fill + on_execution_complete 更新
- 格式: {outcome: rebate_rate}
- 例: {"home": 0.05, "draw": -0.02, "away": 0.03}

逻辑：
- 遍历当前所有可执行套利方向
- 对于每个方向中的每条腿（leg），检查该结果的 way_rebate
- 如果 way_rebate < 0（负返水），则该方向不允许执行，从列表中移除

示例：
- 假设 way_rebate = {"home": 0.05, "draw": -0.02, "away": 0.03}
- draw 的持仓返水率为负（-2%），意味着当前持仓在 draw 发生时亏损
- 那么所有包含 "买 draw" 的套利方向都应被移除
"""

from typing import Any

from .base import Signal, SignalResult, MatchContext, ArbitrageDirection


class MultiWaySignal(Signal):
    """
    Multi-Way 信号

    过滤套利方向，移除那些在某结果 way_rebate 为负时仍要买入的方向。
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
            计算结果（satisfied=False 如果执行前或执行后套利数组为空）
        """
        strict = params.get("strict", True)
        threshold = params.get("threshold", 0.0)

        # 如果执行前套利数组为空，返回 False
        if not context.arbitrage_directions:
            return SignalResult(
                signal_name=self.name,
                satisfied=False,
                value=0,
                details={
                    "message": "No arbitrage directions before filtering",
                    "original_count": 0,
                    "remaining_count": 0,
                },
            )

        # 如果没有 way_rebate 数据，检查是否有套利方向
        if not context.way_rebate:
            return SignalResult(
                signal_name=self.name,
                satisfied=len(context.arbitrage_directions) > 0,
                value=len(context.arbitrage_directions),
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
                outcome = leg.market_type  # "home", "draw", or "away"

                # 获取该方向的持仓返水率（合并两平台）
                outcome_way_rebate = context.get_way_rebate(outcome)

                if outcome_way_rebate is not None:
                    # 检查是否违反规则
                    if strict:
                        if outcome_way_rebate < 0:
                            removed_directions.append({
                                "direction_id": direction.direction_id,
                                "reason": f"{outcome} way_rebate={outcome_way_rebate:.4f} < 0",
                            })
                            return False
                    else:
                        if outcome_way_rebate <= threshold:
                            removed_directions.append({
                                "direction_id": direction.direction_id,
                                "reason": f"{outcome} way_rebate={outcome_way_rebate:.4f} <= {threshold}",
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
