"""
补救计算器 (Recovery Calculator)

计算部分成交后的补救目标，实现平均返水。
"""

import logging
from dataclasses import dataclass
from typing import Any

from .session import OutcomeShares, OutcomeProbabilities


@dataclass
class RecoveryResult:
    """补救计算结果"""
    target_shares: OutcomeShares          # 补救后的目标 share
    additional_shares: OutcomeShares      # 需要额外下注的 share
    base_value: float                     # 基准值（用于比例计算）
    limiting_outcome: str                 # 限制因素（成交比例最高的方向）

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_shares": self.target_shares.to_dict(),
            "additional_shares": self.additional_shares.to_dict(),
            "base_value": self.base_value,
            "limiting_outcome": self.limiting_outcome,
        }


class RecoveryCalculator:
    """
    补救计算器

    计算平均返水策略下的最小补救目标。

    原理：
    要实现平均返水（无论哪个结果盈亏一致），必须按概率比例下注。
    如果概率为 (p1, p2, p3)，下注 (s1, s2, s3)，则需满足：
        s1/p1 = s2/p2 = s3/p3 = base

    当部分成交后，已成交 share 为 (f1, f2, f3)，
    为了补救，需要找到最小的 base 使得：
        base >= max(f1/p1, f2/p2, f3/p3)

    然后目标变为：
        target_i = base * p_i

    如果 target_i < f_i，说明已经超过目标，不需要额外下注。
    """

    def __init__(self, logger: logging.Logger | None = None):
        self._log = logger or logging.getLogger(self.__class__.__name__)

    def calculate(
        self,
        probabilities: OutcomeProbabilities,
        filled: OutcomeShares,
    ) -> RecoveryResult:
        """
        计算补救目标

        Args:
            probabilities: 当前实时概率 (0-1)
            filled: 已成交 share

        Returns:
            补救计算结果
        """
        outcomes = probabilities.outcomes()

        if not outcomes:
            self._log.warning("No valid outcomes for recovery calculation")
            return RecoveryResult(
                target_shares=OutcomeShares(),
                additional_shares=OutcomeShares(),
                base_value=0.0,
                limiting_outcome="",
            )

        # 计算每个方向的 filled/probability 比例
        ratios = {}
        for outcome in outcomes:
            prob = probabilities[outcome]
            fill = filled[outcome]
            if prob > 0:
                ratios[outcome] = fill / prob
            else:
                ratios[outcome] = 0

        # 找到最大比例（限制因素）
        limiting_outcome = max(ratios, key=lambda x: ratios[x])
        base_value = ratios[limiting_outcome]

        self._log.info(
            f"Recovery calculation: ratios={ratios}, "
            f"limiting={limiting_outcome}, base={base_value:.4f}"
        )

        # 计算目标 share
        target = OutcomeShares()
        additional = OutcomeShares()

        for outcome in outcomes:
            prob = probabilities[outcome]
            target_share = base_value * prob
            fill = filled[outcome]

            target[outcome] = target_share
            # 需要额外下注的量（不能为负）
            additional[outcome] = max(0, target_share - fill)

        self._log.info(
            f"Recovery result: target={target.to_dict()}, "
            f"additional={additional.to_dict()}"
        )

        return RecoveryResult(
            target_shares=target,
            additional_shares=additional,
            base_value=base_value,
            limiting_outcome=limiting_outcome,
        )

    def calculate_from_dicts(
        self,
        probabilities: dict[str, float],
        filled: dict[str, float],
    ) -> RecoveryResult:
        """从字典计算补救目标"""
        return self.calculate(
            OutcomeProbabilities.from_dict(probabilities),
            OutcomeShares.from_dict(filled),
        )

    def validate_mean_rebate(
        self,
        probabilities: OutcomeProbabilities,
        shares: OutcomeShares,
    ) -> dict[str, Any]:
        """
        验证是否实现平均返水

        Args:
            probabilities: 概率
            shares: 下注 share

        Returns:
            验证结果，包括各结果的盈亏
        """
        outcomes = probabilities.outcomes()
        total_cost = shares.total()

        results = {}
        pnls = []

        for outcome in outcomes:
            prob = probabilities[outcome]
            share = shares[outcome]

            if prob > 0:
                # 如果该结果发生，赢得 share/prob
                win = share / prob
                # 盈亏 = 赢得 - 总成本
                pnl = win - total_cost
                results[outcome] = {
                    "win": round(win, 4),
                    "cost": round(total_cost, 4),
                    "pnl": round(pnl, 4),
                }
                pnls.append(pnl)

        # 检查所有盈亏是否相等
        is_mean_rebate = len(set(round(p, 4) for p in pnls)) <= 1

        return {
            "outcomes": results,
            "is_mean_rebate": is_mean_rebate,
            "pnl_variance": max(pnls) - min(pnls) if pnls else 0,
        }
