"""
订单追踪器 (Order Tracker)

追踪订单执行状态：
1. 监听 WebSocket 事件确认订单执行状态
2. 超时后返回当前状态，由 orchestrator 的 recovery 循环继续处理
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .planner import OrderOperation, OperationType, OperationVenue


class TrackingStatus(Enum):
    """追踪状态"""
    PENDING = "pending"         # 等待追踪结果
    CONFIRMED = "confirmed"     # 已确认执行
    FAILED = "failed"           # 执行失败
    TIMEOUT = "timeout"         # 超时


@dataclass
class TrackingResult:
    """追踪结果"""
    operation: OrderOperation
    status: TrackingStatus
    venue_order_id: str = ""      # 平台订单 ID
    size_matched: float = 0.0     # 已成交数量
    size_remaining: float = 0.0   # 未成交数量
    error_message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.to_dict(),
            "status": self.status.value,
            "venue_order_id": self.venue_order_id,
            "size_matched": self.size_matched,
            "size_remaining": self.size_remaining,
            "error_message": self.error_message,
            "timestamp": self.timestamp,
        }


@dataclass
class BatchTrackingResult:
    """批量追踪结果"""
    results: list[TrackingResult]
    all_confirmed: bool = False
    all_failed: bool = False
    has_partial: bool = False     # 有部分成交
    timeout_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "all_confirmed": self.all_confirmed,
            "all_failed": self.all_failed,
            "has_partial": self.has_partial,
            "timeout_count": self.timeout_count,
        }


class OrderTracker:
    """
    订单追踪器

    职责：
    1. 监听 WebSocket 事件确认订单执行状态（TRADE_CONFIRMED 等）
    2. 超时后返回当前状态，由 orchestrator 的 recovery 循环处理后续
    """

    def __init__(
        self,
        timeout: float = 30.0,
        logger: logging.Logger | None = None,
    ):
        self._timeout = timeout
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 外部服务引用
        self._polymarket_client = None   # PolymarketOddsClient
        self._orbitexch_client = None    # OrbitExchOddsClient

        # 追踪状态
        self._pending_operations: dict[str, OrderOperation] = {}
        self._results: dict[str, TrackingResult] = {}
        self._tracking_events = asyncio.Event()

    def set_polymarket_client(self, client) -> None:
        """设置 Polymarket 客户端"""
        self._polymarket_client = client

    def set_orbitexch_client(self, client) -> None:
        """设置 OrbitExch 客户端"""
        self._orbitexch_client = client

    def update_timeout(self, timeout: float) -> None:
        """更新追踪超时配置"""
        self._timeout = timeout

    def _generate_operation_key(self, operation: OrderOperation) -> str:
        """生成操作唯一键"""
        return f"{operation.venue.value}_{operation.market_type}_{operation.operation_type.value}_{id(operation)}"

    # =========================================================================
    # 追踪主流程
    # =========================================================================

    async def track_operations(
        self,
        operations: list[OrderOperation],
        operation_results: list[dict],
    ) -> BatchTrackingResult:
        """
        追踪一批操作的执行状态

        Args:
            operations: 操作列表
            operation_results: 操作执行结果（包含 venue_order_id 等）

        Returns:
            批量追踪结果
        """
        if not operations:
            return BatchTrackingResult(results=[], all_confirmed=True)

        self._log.info(f"Starting tracking for {len(operations)} operations, timeout={self._timeout}s")

        # 初始化追踪状态
        self._pending_operations.clear()
        self._results.clear()

        for i, operation in enumerate(operations):
            key = self._generate_operation_key(operation)
            self._pending_operations[key] = operation

            op_result = operation_results[i] if i < len(operation_results) else {}

            self._results[key] = TrackingResult(
                operation=operation,
                status=TrackingStatus.PENDING,
                venue_order_id=(
                    op_result.get("venue_order_id")
                    or op_result.get("order_id")
                    or operation.order_id
                ),
            )
            if op_result.get("success") is False:
                self._results[key].status = TrackingStatus.FAILED
                self._results[key].error_message = op_result.get("message", "Operation failed")
                continue
            if operation.operation_type == OperationType.CANCEL and op_result.get("success"):
                # 撤单 API 返回成功，直接标记为已确认，无需等待快照对比
                result = self._results[key]
                result.status = TrackingStatus.CONFIRMED
                self._log.info(
                    f"CANCEL confirmed via API response: order_id={result.venue_order_id}"
                )
                continue
            if op_result.get("venue_order_id") and operation.operation_type == OperationType.PLACE:
                # 有下单回执时先标记为已确认挂单，避免被误判为失败
                self._results[key].status = TrackingStatus.CONFIRMED
                self._results[key].size_matched = 0.0
                self._results[key].size_remaining = operation.size

        # 追踪启动前补抓一次当前缓存，避免下单回执与 tracking 启动之间的竞态丢失。
        await self._bootstrap_from_cached_state()

        # 等待 WebSocket 事件或超时
        # 只有所有下单操作完全成交才提前退出，否则等到超时
        # 这样部分成交的订单有更多时间被撮合
        start_time = time.time()
        remaining_time = self._timeout

        while remaining_time > 0 and not self._all_fully_filled():
            try:
                await asyncio.wait_for(
                    self._tracking_events.wait(),
                    timeout=min(remaining_time, 5.0),
                )
                self._tracking_events.clear()
            except asyncio.TimeoutError:
                pass

            elapsed = time.time() - start_time
            remaining_time = self._timeout - elapsed

        self._log.info(f"Tracking ended (all_filled={self._all_fully_filled()})")
        return self._summarize_results()

    async def _bootstrap_from_cached_state(self) -> None:
        """从客户端当前缓存回填已发生成交的结果。"""
        if self._polymarket_client:
            try:
                await self._polymarket_client.fetch_open_orders()
            except Exception as e:
                self._log.warning(f"Bootstrap fetch_open_orders failed: {e}")

        updated = False
        for result in self._results.values():
            if result.status == TrackingStatus.FAILED:
                continue

            operation = result.operation
            if operation.operation_type != OperationType.PLACE:
                continue

            try:
                if operation.venue == OperationVenue.POLYMARKET:
                    if not self._polymarket_client or not operation.condition_id:
                        continue
                    orders = self._polymarket_client.get_current_orders(operation.condition_id)
                    for order in orders:
                        if result.venue_order_id and order.order_id != result.venue_order_id:
                            continue
                        if operation.token_id and order.asset_id != operation.token_id:
                            continue

                        result.status = TrackingStatus.CONFIRMED
                        result.size_matched = order.size_matched
                        result.size_remaining = max(0.0, order.original_size - order.size_matched)
                        updated = True
                        break

                elif operation.venue == OperationVenue.ORBITEXCH:
                    if not self._orbitexch_client or not operation.market_id:
                        continue
                    bets = self._orbitexch_client.get_current_bets(operation.market_id)
                    for bet in bets:
                        if result.venue_order_id and str(bet.get("offerId", "")) != result.venue_order_id:
                            continue
                        if operation.selection_id and str(bet.get("selectionId", "")) != operation.selection_id:
                            continue
                        if bet.get("side", "").upper() != "BACK":
                            continue

                        result.status = TrackingStatus.CONFIRMED
                        result.size_matched = float(bet.get("sizeMatched", 0))
                        result.size_remaining = float(bet.get("sizeRemaining", 0))
                        updated = True
                        break
            except Exception as e:
                self._log.warning(
                    f"Bootstrap refresh failed for {operation.venue.value}/{operation.market_type}: {e}"
                )

        if updated:
            self._tracking_events.set()

    def _all_fully_filled(self) -> bool:
        """
        检查是否所有操作都已完成（下单完全成交 / 撤单已确认）

        只有全部操作都成功且完全成交才返回 True。
        任何操作失败都视为未完成，继续等待直到超时。
        """
        for r in self._results.values():
            if r.status in (TrackingStatus.PENDING, TrackingStatus.FAILED):
                return False
            # PLACE:必须 size_remaining == 0 才算完全成交
            if r.operation.operation_type == OperationType.PLACE:
                if r.size_remaining > 0:
                    return False
        return True

    # =========================================================================
    # 汇总
    # =========================================================================

    def _summarize_results(self) -> BatchTrackingResult:
        """汇总追踪结果"""
        results = list(self._results.values())

        confirmed_count = sum(1 for r in results if r.status == TrackingStatus.CONFIRMED)
        failed_count = sum(1 for r in results if r.status == TrackingStatus.FAILED)
        timeout_count = sum(1 for r in results if r.status == TrackingStatus.TIMEOUT)

        has_partial = any(
            r.size_matched > 0 and r.size_remaining > 0
            for r in results
        )

        return BatchTrackingResult(
            results=results,
            all_confirmed=confirmed_count == len(results),
            all_failed=failed_count == len(results),
            has_partial=has_partial,
            timeout_count=timeout_count,
        )

    # =========================================================================
    # WebSocket 事件处理（由外部客户端调用）
    # =========================================================================

    def on_polymarket_event(self, event_type: str, data: dict) -> None:
        """
        处理 Polymarket WebSocket 事件

        Args:
            event_type: 事件类型 (PLACEMENT, UPDATE, CANCELLATION, TRADE_CONFIRMED)
            data: 事件数据
        """
        if event_type == "TRADE_CONFIRMED":
            # Trade CONFIRMED: 完全成交终态信号
            # 收集 trade 中所有 order_id（maker + taker），与我们的订单逐一比较
            candidate_ids = set()
            for maker in data.get("maker_orders", []):
                oid = maker.get("order_id", "")
                if oid:
                    candidate_ids.add(oid)
            taker_id = data.get("taker_order_id", "")
            if taker_id:
                candidate_ids.add(taker_id)

            self._log.info(
                f"TRADE_CONFIRMED: candidate_ids={candidate_ids}"
            )

            matched_any = False
            for key, result in self._results.items():
                if result.venue_order_id in candidate_ids:
                    result.size_matched = result.operation.size
                    result.size_remaining = 0.0
                    self._log.info(
                        f"TRADE_CONFIRMED matched: order_id={result.venue_order_id}, "
                        f"filled={result.size_matched}"
                    )
                    matched_any = True
            if matched_any:
                self._tracking_events.set()
            return

        order_id = data.get("id", "") or data.get("order_id", "")

        if not order_id:
            return

        for key, result in self._results.items():
            if result.venue_order_id != order_id:
                continue
            if event_type == "PLACEMENT":
                result.status = TrackingStatus.CONFIRMED
                result.size_matched = float(data.get("size_matched", 0))
                result.size_remaining = float(data.get("original_size", 0)) - result.size_matched
            elif event_type == "UPDATE":
                result.size_matched = float(data.get("size_matched", 0))
                result.size_remaining = float(data.get("original_size", 0)) - result.size_matched
            elif event_type == "CANCELLATION":
                result.status = TrackingStatus.CONFIRMED

            self._tracking_events.set()
            break

    def on_orbitexch_event(self, event_type: str, data: dict) -> None:
        """
        处理 OrbitExch WebSocket 事件

        Args:
            event_type: 事件类型
            data: 事件数据
        """
        offer_id = str(data.get("offerId", ""))
        market_id = str(data.get("marketId", ""))
        selection_id = str(data.get("selectionId", ""))

        for key, result in self._results.items():
            op = result.operation
            if offer_id and result.venue_order_id != offer_id:
                continue
            if not offer_id and not (op.market_id == market_id and op.selection_id == selection_id):
                continue
            if op.operation_type == OperationType.CANCEL:
                if float(data.get("sizeRemaining", 0)) == 0.0:
                    result.status = TrackingStatus.CONFIRMED
                    result.size_matched = float(data.get("sizeMatched", 0))
                    result.size_remaining = 0.0
                    self._tracking_events.set()
                    break
                result.size_matched = float(data.get("sizeMatched", 0))
                result.size_remaining = float(data.get("sizeRemaining", 0))
                continue
            result.status = TrackingStatus.CONFIRMED
            result.size_matched = float(data.get("sizeMatched", 0))
            result.size_remaining = float(data.get("sizeRemaining", 0))

            self._tracking_events.set()
            break
