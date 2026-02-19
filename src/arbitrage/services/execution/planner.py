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

    def __init__(self, logger: logging.Logger | None = None):
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._recovery_calculator = RecoveryCalculator(logger=self._log)

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
        - OrbitExch: 使用 MODIFY 操作（修改 size + Take 按市价执行），无需撤单
        - Polymarket: 使用 FOK 模式，不会有部分成交，无需补救

        Args:
            session: 执行会话
            current_probabilities: 当前实时概率
            pending_orders: 未成交订单列表
            order_info_getter: 获取订单信息的函数

        Returns:
            执行计划（包含 MODIFY 和/或 PLACE 操作）
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

        # 处理未成交订单
        # OrbitExch: 修改 size 并按市价执行（MODIFY）
        # Polymarket: 使用 FOK，不应该有未成交订单
        for order in pending_orders:
            size_remaining = order.get("size_remaining", 0)
            if size_remaining <= 0:
                continue

            venue_str = order.get("venue", "")
            market_type = order.get("market_type", "")

            if venue_str == "orbitexch":
                # OrbitExch: 计算新的目标 size 并修改+Take
                # 新 size = 需要额外下注的 share 对应的 size
                additional_share = recovery_result.additional_shares[market_type]
                prob = current_probabilities[market_type]

                if additional_share > 0 and prob > 0:
                    # OrbitExch size = share * probability
                    new_size = additional_share * prob

                    self._log.info(
                        f"OrbitExch MODIFY: {market_type}, "
                        f"additional_share={additional_share:.2f}, new_size={new_size:.2f}"
                    )

                    operations.append(OrderOperation(
                        operation_type=OperationType.MODIFY,
                        venue=OperationVenue.ORBITEXCH,
                        market_type=market_type,
                        size=new_size,  # 新的 size
                        price=order.get("price", 0),  # 保留原价格用于记录
                        order_id=order.get("order_id", ""),
                        market_id=order.get("market_id", ""),
                        selection_id=order.get("selection_id", ""),
                        metadata={"action": "modify_and_take"},
                    ))
                else:
                    # 不需要额外下注，直接按当前 size 市价执行
                    self._log.info(
                        f"OrbitExch MODIFY (take current): {market_type}, size={size_remaining}"
                    )

                    operations.append(OrderOperation(
                        operation_type=OperationType.MODIFY,
                        venue=OperationVenue.ORBITEXCH,
                        market_type=market_type,
                        size=size_remaining,  # 保持当前 size
                        price=order.get("price", 0),
                        order_id=order.get("order_id", ""),
                        market_id=order.get("market_id", ""),
                        selection_id=order.get("selection_id", ""),
                        metadata={"action": "take_current"},
                    ))

            elif venue_str == "polymarket":
                # Polymarket 使用 FOK，理论上不应该有未成交订单
                # 如果有，记录警告（可能是旧订单或异常情况）
                self._log.warning(
                    f"Unexpected Polymarket pending order: {market_type}, "
                    f"remaining={size_remaining} (FOK should prevent this)"
                )

        # 检查是否需要额外下单（没有对应的未成交订单可以修改）
        outcomes = current_probabilities.outcomes()
        pending_markets = {o.get("market_type") for o in pending_orders if o.get("size_remaining", 0) > 0}

        for outcome in outcomes:
            additional = recovery_result.additional_shares[outcome]
            if additional <= 0:
                continue

            # 如果该方向已经有 MODIFY 操作，跳过
            if outcome in pending_markets:
                continue

            prob = current_probabilities[outcome]

            # 获取订单信息
            order_info = order_info_getter(session.pair_id, outcome) if order_info_getter else None

            # 新下单使用 Polymarket FOK
            size = additional  # Polymarket: size = share
            price = prob

            self._log.info(
                f"Polymarket PLACE (FOK): {outcome}, size={size:.2f}, price={price:.4f}"
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

        self._log.info(
            f"Recovery plan: {len(operations)} operations "
            f"({sum(1 for o in operations if o.operation_type == OperationType.MODIFY)} modify, "
            f"{sum(1 for o in operations if o.operation_type == OperationType.PLACE)} place)"
        )

        return ExecutionPlan(
            operations=operations,
            has_modifies=any(o.operation_type == OperationType.MODIFY for o in operations),
            has_places=any(o.operation_type == OperationType.PLACE for o in operations),
            description="Recovery: modify OrbitExch + place Polymarket FOK",
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
