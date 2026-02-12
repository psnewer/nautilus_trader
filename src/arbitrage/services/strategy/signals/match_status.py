"""
比赛状态信号

包含赛前盘 (pre-match) 和赛中盘 (live) 信号。
"""

from typing import Any

from .base import Signal, SignalResult, MatchContext


class PreMatchSignal(Signal):
    """
    赛前盘信号

    当比赛未开始时（is_live=False），信号为 True。
    """

    @property
    def name(self) -> str:
        return "pre-match"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算赛前盘信号

        Args:
            context: 比赛上下文
            params: 信号参数（目前无参数）

        Returns:
            计算结果
        """
        is_pre_match = not context.is_live

        return SignalResult(
            signal_name=self.name,
            satisfied=is_pre_match,
            value=1.0 if is_pre_match else 0.0,
            details={
                "is_live": context.is_live,
                "description": "比赛未开始" if is_pre_match else "比赛进行中",
            },
        )


class LiveSignal(Signal):
    """
    赛中盘信号

    当比赛进行中时（is_live=True），信号为 True。
    """

    @property
    def name(self) -> str:
        return "live"

    def calculate(self, context: MatchContext, params: dict[str, Any]) -> SignalResult:
        """
        计算赛中盘信号

        Args:
            context: 比赛上下文
            params: 信号参数（目前无参数）

        Returns:
            计算结果
        """
        is_live = context.is_live

        return SignalResult(
            signal_name=self.name,
            satisfied=is_live,
            value=1.0 if is_live else 0.0,
            details={
                "is_live": context.is_live,
                "description": "比赛进行中" if is_live else "比赛未开始",
            },
        )
