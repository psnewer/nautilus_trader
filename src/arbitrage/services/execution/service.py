"""
订单执行服务

协调 Polymarket 和 OrbitExch 执行器，提供统一的订单执行接口。
"""

import asyncio
import logging
import time
from typing import Any, Callable

from playwright.async_api import Page

from .config import ExecutionConfig
from .models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    ExecutionResult,
    CancelResult,
    Venue,
)
from .polymarket_executor import PolymarketExecutor
from .orbitexch_executor import OrbitExchExecutor


class ExecutionService:
    """
    订单执行服务

    功能:
    1. 接收来自 StrategyService 的执行请求
    2. 根据 venue 分发到对应的执行器
    3. 管理订单生命周期
    4. 提供订单查询接口
    """

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config or ExecutionConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 执行器
        self._polymarket_executor = PolymarketExecutor(
            config=self.config,
            logger=logging.getLogger("PolymarketExecutor"),
        )
        self._orbitexch_executor = OrbitExchExecutor(
            config=self.config,
            logger=logging.getLogger("OrbitExchExecutor"),
        )

        # 订单管理
        self._orders: dict[str, Order] = {}  # order_id -> Order
        self._active_orders: dict[str, Order] = {}  # order_id -> Order (活跃订单)

        # 回调
        self._order_update_callbacks: list[Callable[[Order], None]] = []

        # 状态
        self._initialized = False

    # =========================================================================
    # 生命周期
    # =========================================================================

    async def initialize(self) -> bool:
        """
        初始化服务

        Returns:
            是否初始化成功
        """
        if not self.config.enabled:
            self._log.info("Execution service disabled")
            return False

        self._log.info("Initializing execution service...")

        # 初始化 Polymarket 执行器
        polymarket_ok = await self._polymarket_executor.initialize()
        if polymarket_ok:
            self._log.info("Polymarket executor initialized")
        else:
            self._log.warning("Polymarket executor initialization failed")

        # OrbitExch 执行器不需要单独初始化，使用页面引用
        self._log.info("OrbitExch executor ready (requires page reference)")

        self._initialized = True
        self._log.info("Execution service initialized")

        return True

    def set_orbitexch_page(self, competition_id: str, page: Page) -> None:
        """
        设置 OrbitExch 页面引用

        由 OddsSubscriptionService 调用，共享浏览器页面。

        Args:
            competition_id: 联赛 ID
            page: Playwright 页面
        """
        self._orbitexch_executor.set_page(competition_id, page)

    # =========================================================================
    # 订单执行
    # =========================================================================

    async def execute_order(self, order: Order) -> ExecutionResult:
        """
        执行订单

        根据 venue 分发到对应的执行器。

        Args:
            order: 订单

        Returns:
            执行结果
        """
        if not self._initialized:
            return ExecutionResult(
                success=False,
                order=order,
                message="Service not initialized",
            )

        # 保存订单
        self._orders[order.order_id] = order

        # 分发到对应执行器
        if order.venue == Venue.POLYMARKET:
            result = await self._polymarket_executor.place_order(order)
        elif order.venue == Venue.ORBITEXCH:
            result = await self._orbitexch_executor.place_order(order)
        else:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Unknown venue: {order.venue}",
            )

        # 更新订单状态
        if result.success and order.is_active:
            self._active_orders[order.order_id] = order

        # 触发回调
        self._notify_order_update(order)

        return result

    async def cancel_order(self, order_id: str) -> CancelResult:
        """
        撤销订单

        Args:
            order_id: 订单 ID

        Returns:
            撤单结果
        """
        order = self._orders.get(order_id)
        if not order:
            return CancelResult(
                success=False,
                order_id=order_id,
                message="Order not found",
            )

        # 分发到对应执行器
        if order.venue == Venue.POLYMARKET:
            result = await self._polymarket_executor.cancel_order(order)
        elif order.venue == Venue.ORBITEXCH:
            result = await self._orbitexch_executor.cancel_order(order)
        else:
            return CancelResult(
                success=False,
                order_id=order_id,
                message=f"Unknown venue: {order.venue}",
            )

        # 更新活跃订单
        if result.success and order_id in self._active_orders:
            del self._active_orders[order_id]

        # 触发回调
        self._notify_order_update(order)

        return result

    async def take_remaining_at_market(self, order_id: str) -> ExecutionResult:
        """
        将未成交部分按市价立即执行

        Args:
            order_id: 订单 ID

        Returns:
            执行结果
        """
        order = self._orders.get(order_id)
        if not order:
            return ExecutionResult(
                success=False,
                order=Order(order_id=order_id),
                message="Order not found",
            )

        # 分发到对应执行器
        if order.venue == Venue.POLYMARKET:
            result = await self._polymarket_executor.take_remaining_at_market(order)
        elif order.venue == Venue.ORBITEXCH:
            result = await self._orbitexch_executor.take_remaining_at_market(order)
        else:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Unknown venue: {order.venue}",
            )

        # 更新活跃订单
        if result.success and result.order.is_done:
            if order_id in self._active_orders:
                del self._active_orders[order_id]

        # 触发回调
        self._notify_order_update(result.order)

        return result

    # =========================================================================
    # 批量操作
    # =========================================================================

    async def execute_arbitrage_orders(
        self,
        polymarket_order: Order,
        orbitexch_order: Order,
    ) -> tuple[ExecutionResult, ExecutionResult]:
        """
        执行套利订单对

        同时在两个平台下单。

        Args:
            polymarket_order: Polymarket 订单
            orbitexch_order: OrbitExch 订单

        Returns:
            (Polymarket 结果, OrbitExch 结果)
        """
        self._log.info(
            f"Executing arbitrage orders: "
            f"polymarket={polymarket_order.market_type}, "
            f"orbitexch={orbitexch_order.market_type}"
        )

        # 并行执行
        results = await asyncio.gather(
            self.execute_order(polymarket_order),
            self.execute_order(orbitexch_order),
            return_exceptions=True,
        )

        poly_result = results[0] if not isinstance(results[0], Exception) else ExecutionResult(
            success=False,
            order=polymarket_order,
            message=str(results[0]),
        )

        orbit_result = results[1] if not isinstance(results[1], Exception) else ExecutionResult(
            success=False,
            order=orbitexch_order,
            message=str(results[1]),
        )

        return poly_result, orbit_result

    async def cancel_all_orders(self, venue: Venue | None = None) -> list[CancelResult]:
        """
        撤销所有活跃订单

        Args:
            venue: 可选，指定平台

        Returns:
            撤单结果列表
        """
        orders_to_cancel = [
            order for order in self._active_orders.values()
            if venue is None or order.venue == venue
        ]

        if not orders_to_cancel:
            return []

        self._log.info(f"Cancelling {len(orders_to_cancel)} orders")

        results = await asyncio.gather(
            *[self.cancel_order(order.order_id) for order in orders_to_cancel],
            return_exceptions=True,
        )

        return [
            r if not isinstance(r, Exception) else CancelResult(
                success=False,
                order_id="",
                message=str(r),
            )
            for r in results
        ]

    async def cancel_all_orbitexch_unmatched(self) -> CancelResult:
        """
        撤销 OrbitExch 上所有未成交订单

        使用 OrbitExch 的 "Cancel All Unmatched" 功能批量撤单。

        Returns:
            撤单结果
        """
        self._log.info("Cancelling all OrbitExch unmatched orders")

        result = await self._orbitexch_executor.cancel_all_unmatched()

        # 更新本地订单状态
        if result.success:
            for order_id, order in list(self._active_orders.items()):
                if order.venue == Venue.ORBITEXCH:
                    order.status = OrderStatus.CANCELLED
                    order.updated_at = time.time()
                    del self._active_orders[order_id]
                    self._notify_order_update(order)

        return result

    # =========================================================================
    # 回调
    # =========================================================================

    def register_order_callback(self, callback: Callable[[Order], None]) -> None:
        """注册订单更新回调"""
        self._order_update_callbacks.append(callback)

    def _notify_order_update(self, order: Order) -> None:
        """通知订单更新"""
        for callback in self._order_update_callbacks:
            try:
                callback(order)
            except Exception as e:
                self._log.error(f"Order callback error: {e}")

    # =========================================================================
    # 查询
    # =========================================================================

    def get_order(self, order_id: str) -> Order | None:
        """获取订单"""
        return self._orders.get(order_id)

    def get_active_orders(self, venue: Venue | None = None) -> list[Order]:
        """获取活跃订单"""
        orders = list(self._active_orders.values())
        if venue:
            orders = [o for o in orders if o.venue == venue]
        return orders

    def get_all_orders(
        self,
        venue: Venue | None = None,
        status: OrderStatus | None = None,
        limit: int = 100,
    ) -> list[Order]:
        """获取所有订单"""
        orders = list(self._orders.values())

        if venue:
            orders = [o for o in orders if o.venue == venue]
        if status:
            orders = [o for o in orders if o.status == status]

        # 按创建时间倒序
        orders.sort(key=lambda o: o.created_at, reverse=True)

        return orders[:limit]

    def get_orders_summary(self) -> dict[str, Any]:
        """获取订单统计"""
        total = len(self._orders)
        active = len(self._active_orders)

        by_venue = {}
        by_status = {}

        for order in self._orders.values():
            venue = order.venue.value
            status = order.status.value

            by_venue[venue] = by_venue.get(venue, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1

        return {
            "total_orders": total,
            "active_orders": active,
            "by_venue": by_venue,
            "by_status": by_status,
        }
