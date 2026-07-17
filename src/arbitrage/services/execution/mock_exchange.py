"""
模拟交易所

用于 Debug 模式下模拟订单执行、撤单、成交流转。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from src.arbitrage.services.debug import debug_manager, MockCategory

from .models import (
    CancelResult,
    ExecutionResult,
    Order,
    OrderStatus,
)


class MockExchange:
    """
    模拟交易所

    功能：
    - 下单并异步流转状态
    - 支持部分成交/延迟回报
    - 支持撤单
    """

    def __init__(
        self,
        order_update_callback: Callable[[Order], None],
        logger: logging.Logger | None = None,
    ) -> None:
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._order_update_callback = order_update_callback
        self._tasks: dict[str, asyncio.Task] = {}

    async def place_order(self, order: Order) -> ExecutionResult:
        """模拟下单"""
        plan = self._get_execution_plan(order)
        if not plan.get("success", True):
            order.status = self._coerce_status(plan.get("status"), OrderStatus.REJECTED)
            order.error_message = plan.get("message", "[MOCK] Order rejected")
            order.updated_at = time.time()
            order.metadata["mock_exchange"] = True
            self._emit_update(order)
            return ExecutionResult(
                success=False,
                order=order,
                message=order.error_message,
                venue_response=plan,
            )

        order.status = self._coerce_status(plan.get("status"), OrderStatus.SUBMITTED)
        order.submitted_at = time.time()
        order.updated_at = order.submitted_at
        order.venue_order_id = plan.get("venue_order_id", f"mock-{order.order_id}")
        order.metadata["mock_exchange"] = True
        self._emit_update(order)

        timeline = self._build_timeline(order, plan)
        if timeline:
            task = asyncio.create_task(self._run_timeline(order, timeline))
            self._tasks[order.order_id] = task

        return ExecutionResult(
            success=True,
            order=order,
            message=plan.get("message", "[MOCK] Order accepted"),
            venue_response=plan,
        )

    async def cancel_order(self, order: Order) -> CancelResult:
        """模拟撤单"""
        self._cancel_timeline(order.order_id)
        if order.is_done:
            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message="[MOCK] Order already completed",
            )

        order.status = OrderStatus.CANCELLED
        order.updated_at = time.time()
        order.metadata["mock_exchange"] = True
        self._emit_update(order)

        return CancelResult(
            success=True,
            order_id=order.order_id,
            venue_order_id=order.venue_order_id,
            message="[MOCK] Order cancelled",
        )

    async def cancel_all_unmatched(self, orders: list[Order]) -> CancelResult:
        """模拟批量撤单"""
        cancelled = 0
        for order in orders:
            result = await self.cancel_order(order)
            if result.success:
                cancelled += 1

        return CancelResult(
            success=True,
            order_id="*",
            message=f"[MOCK] Cancelled {cancelled} orders",
        )

    def _emit_update(self, order: Order) -> None:
        """通知订单更新"""
        try:
            self._order_update_callback(order)
        except Exception as exc:
            self._log.error(f"Mock order update callback error: {exc}")

    def _cancel_timeline(self, order_id: str) -> None:
        """取消订单状态流转任务"""
        task = self._tasks.pop(order_id, None)
        if task and not task.done():
            task.cancel()

    def _get_execution_plan(self, order: Order) -> dict[str, Any]:
        """获取执行模拟计划"""
        if not debug_manager.enabled:
            return {}

        context = {
            "venue": order.venue.value,
            "pair_id": order.pair_id,
            "market_type": order.market_type,
            "side": order.side.value,
            "order_id": order.order_id,
        }
        scenario = order.metadata.get("test_scenario") or order.metadata.get("mock_scenario")
        if scenario:
            context["test_scenario"] = scenario

        plan = debug_manager.get_mock_data(MockCategory.EXECUTION, context)
        return plan or {}

    def _build_timeline(self, order: Order, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """构造默认状态流转"""
        if plan.get("timeline"):
            return list(plan["timeline"])

        live_delay = float(plan.get("live_delay", 0.2))
        partial_fill_ratio = float(plan.get("partial_fill_ratio", 0.5))
        partial_fill_delay = float(plan.get("partial_fill_delay", 0.5))
        final_fill_delay = float(plan.get("final_fill_delay", 1.0))

        timeline: list[dict[str, Any]] = []
        if live_delay >= 0:
            timeline.append({"delay": live_delay, "status": "live"})
        if 0 < partial_fill_ratio < 1:
            timeline.append({
                "delay": partial_fill_delay,
                "status": "partially_filled",
                "filled_size": order.size * partial_fill_ratio,
            })
        timeline.append({
            "delay": final_fill_delay,
            "status": "filled",
            "filled_size": order.size,
        })

        return timeline

    async def _run_timeline(self, order: Order, timeline: list[dict[str, Any]]) -> None:
        """按时间顺序推进订单状态"""
        for step in timeline:
            if order.is_done:
                return
            delay = max(0.0, float(step.get("delay", 0)))
            if delay:
                await asyncio.sleep(delay)
            if order.is_done:
                return

            status = self._coerce_status(step.get("status"), order.status)
            filled_size = step.get("filled_size")
            avg_fill_price = step.get("avg_fill_price")
            venue_order_id = step.get("venue_order_id")

            if filled_size is not None:
                order.filled_size = float(filled_size)
                if order.filled_size > 0:
                    order.avg_fill_price = (
                        float(avg_fill_price)
                        if avg_fill_price is not None
                        else (order.avg_fill_price or order.price)
                    )

            if venue_order_id:
                order.venue_order_id = venue_order_id

            order.status = status
            order.updated_at = time.time()
            order.metadata["mock_exchange"] = True
            self._emit_update(order)

    @staticmethod
    def _coerce_status(value: Any, default: OrderStatus) -> OrderStatus:
        """将状态值转换为 OrderStatus"""
        if isinstance(value, OrderStatus):
            return value
        if isinstance(value, str):
            try:
                return OrderStatus(value)
            except ValueError:
                return default
        return default
