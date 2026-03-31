"""
执行规划器 (Execution Planner)

规划订单操作，包括初始下单和补救。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .session import ExecutionSession, OutcomeShares, OutcomeProbabilities
from .recovery import RecoveryCalculator


class OperationType(Enum):
    """操作类型"""
    PLACE = "place"       # 下单
    CANCEL = "cancel"     # 撤单
    MODIFY = "modify"     # 修改（按市价执行未成交部分）


class OperationVenue(Enum):
    """操作平台"""
    POLYMARKET = "polymarket"
    ORBITEXCH = "orbitexch"


@dataclass
class OrderOperation:
    """订单操作"""
    operation_type: OperationType
    venue: OperationVenue
    market_type: str                      # home/draw/away
    size: float = 0.0                     # 下单/修改的 size
    price: float = 0.0                    # 下单/修改的价格
    order_id: str = ""                    # 撤单/修改时的订单 ID
    token_id: str = ""                    # Polymarket token ID
    condition_id: str = ""                # Polymarket condition ID
    market_id: str = ""                   # OrbitExch market ID
    selection_id: str = ""                # OrbitExch selection ID
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type.value,
            "venue": self.venue.value,
            "market_type": self.market_type,
            "size": self.size,
            "price": self.price,
            "order_id": self.order_id,
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "metadata": self.metadata,
        }


@dataclass
class ExecutionPlan:
    """执行计划"""
    operations: list[OrderOperation]
    has_cancels: bool = False
    has_places: bool = False
    has_modifies: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": [op.to_dict() for op in self.operations],
            "has_cancels": self.has_cancels,
            "has_places": self.has_places,
            "has_modifies": self.has_modifies,
            "description": self.description,
        }


class ExecutionPlanner:
    """
    执行规划器

    职责：
    1. 根据策略订单规划初始操作
    2. 根据成交情况规划补救操作
    3. 分离撤单和下单操作
    """

    def __init__(self, fx_getter: "Callable | None" = None, logger: logging.Logger | None = None):
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._recovery_calculator = RecoveryCalculator(logger=self._log)
        self._get_fx = fx_getter

    def plan_initial(
        self,
        session: ExecutionSession,
        legs: list[dict],
        order_info_getter,
    ) -> ExecutionPlan:
        """
        规划初始订单操作

        Args:
            session: 执行会话
            legs: 套利腿列表（来自策略）
            order_info_getter: 获取订单信息的函数 (pair_id, market_type) -> order_info

        Returns:
            执行计划
        """
        operations = []

        for leg in legs:
            venue_str = leg.get("venue", "")
            market_type = leg.get("market_type", "")
            probability = leg.get("probability", 0) / 100  # 转为 0-1
            raw_odds = leg.get("raw_odds", 0)

            # 获取目标 share
            target_share = session.adjusted_target[market_type]
            if target_share <= 0:
                continue

            # 获取订单信息
            order_info = order_info_getter(session.pair_id, market_type) if order_info_getter else None

            if venue_str == "polymarket":
                venue = OperationVenue.POLYMARKET
                # Polymarket: size = share
                size = target_share
                price = probability

                op = OrderOperation(
                    operation_type=OperationType.PLACE,
                    venue=venue,
                    market_type=market_type,
                    size=size,
                    price=price,
                    token_id=order_info.get("polymarket", {}).get("token_id", "") if order_info else "",
                    condition_id=order_info.get("polymarket", {}).get("condition_id", "") if order_info else "",
                )
                operations.append(op)

            elif venue_str == "orbitexch":
                venue = OperationVenue.ORBITEXCH
                # OrbitExch: size = share / odds = share * probability
                size = target_share * probability if probability > 0 else 0
                price = raw_odds  # 使用原始赔率

                op = OrderOperation(
                    operation_type=OperationType.PLACE,
                    venue=venue,
                    market_type=market_type,
                    size=size,
                    price=price,
                    market_id=order_info.get("orbitexch", {}).get("market_id", "") if order_info else "",
                    selection_id=order_info.get("orbitexch", {}).get("selection_id", "") if order_info else "",
                )
                operations.append(op)

        self._log.info(f"Initial plan: {len(operations)} operations")

        return ExecutionPlan(
            operations=operations,
            has_places=len(operations) > 0,
            description="Initial order placement",
        )

    def plan_recovery(
        self,
        session: ExecutionSession,
        current_probabilities: OutcomeProbabilities,
        pending_orders: list[dict],
        order_info_getter,
    ) -> ExecutionPlan:
        """
        规划补救操作

        策略：
        - 所有未成交订单由 orchestrator 统一撤单（撤单在调用本方法前已完成）
        - 本方法只规划新的 PLACE 操作，根据 session.outcome_venues 选择平台

        注意：OrbitExch MODIFY 操作已弃用，改为撤单后重新下单。
        MODIFY 实现保留在 orchestrator._execute_modify_operation 中供需要时使用。

        Args:
            session: 执行会话
            current_probabilities: 当前实时概率
            pending_orders: 未成交订单列表（已弃用，应为空列表）
            order_info_getter: 获取订单信息的函数

        Returns:
            执行计划（PLACE 操作）
        """
        # 计算补救目标
        recovery_result = self._recovery_calculator.calculate(
            current_probabilities,
            session.filled,
        )

        # 更新会话目标
        session.update_target(recovery_result.target_shares.to_dict())

        self._log.info(
            f"Recovery target: {recovery_result.target_shares.to_dict()}, "
            f"additional: {recovery_result.additional_shares.to_dict()}"
        )

        operations = []
        outcomes = current_probabilities.outcomes()

        for outcome in outcomes:
            additional = recovery_result.additional_shares[outcome]
            if additional <= 0:
                continue

            prob = current_probabilities[outcome]
            order_info = order_info_getter(session.pair_id, outcome) if order_info_getter else None

            # 根据 session.outcome_venues 决定下单平台
            venue_str = session.outcome_venues.get(outcome, "polymarket")

            if venue_str == "orbitexch":
                # OrbitExch: additional 是 USD share, 需要转为 GBP stake
                # GBP stake = USD share * prob / fx
                fx = self._get_fx() if self._get_fx else 1.0
                size = additional * prob / fx if prob > 0 else 0
                price = 1 / prob if prob > 0 else 0  # 转为赔率

                if size <= 0:
                    continue

                self._log.info(
                    f"OrbitExch PLACE: {outcome}, share={additional:.2f}, "
                    f"size={size:.2f}, odds={price:.4f}"
                )

                operations.append(OrderOperation(
                    operation_type=OperationType.PLACE,
                    venue=OperationVenue.ORBITEXCH,
                    market_type=outcome,
                    size=size,
                    price=price,
                    market_id=order_info.get("orbitexch", {}).get("market_id", "") if order_info else "",
                    selection_id=order_info.get("orbitexch", {}).get("selection_id", "") if order_info else "",
                ))
            else:
                # Polymarket: size = share
                size = additional
                price = prob

                self._log.info(
                    f"Polymarket PLACE: {outcome}, size={size:.2f}, price={price:.4f}"
                )

                operations.append(OrderOperation(
                    operation_type=OperationType.PLACE,
                    venue=OperationVenue.POLYMARKET,
                    market_type=outcome,
                    size=size,
                    price=price,
                    token_id=order_info.get("polymarket", {}).get("token_id", "") if order_info else "",
                    condition_id=order_info.get("polymarket", {}).get("condition_id", "") if order_info else "",
                ))

        self._log.info(f"Recovery plan: {len(operations)} PLACE operations")

        return ExecutionPlan(
            operations=operations,
            has_places=len(operations) > 0,
            description="Recovery: place orders on original venues",
        )

    def plan_modify_to_market(
        self,
        pending_orders: list[dict],
    ) -> ExecutionPlan:
        """
        规划按市价执行未成交部分

        Args:
            pending_orders: 未成交订单列表

        Returns:
            修改计划
        """
        operations = []

        for order in pending_orders:
            if order.get("size_remaining", 0) > 0:
                venue_str = order.get("venue", "")
                venue = OperationVenue.POLYMARKET if venue_str == "polymarket" else OperationVenue.ORBITEXCH

                operations.append(OrderOperation(
                    operation_type=OperationType.MODIFY,
                    venue=venue,
                    market_type=order.get("market_type", ""),
                    size=order.get("size_remaining", 0),
                    order_id=order.get("order_id", ""),
                    market_id=order.get("market_id", ""),
                    selection_id=order.get("selection_id", ""),
                    metadata={"action": "take_market_price"},
                ))

        self._log.info(f"Modify plan: {len(operations)} operations")

        return ExecutionPlan(
            operations=operations,
            has_modifies=len(operations) > 0,
            description="Modify orders to market price",
        )
