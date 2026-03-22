"""
订单追踪器 (Order Tracker)

追踪订单执行状态：
1. 操作前拍摄快照（当前订单列表）
2. 操作后等待 WebSocket 事件确认
3. 超时后刷新数据（Polymarket 调 API，OrbitExch 刷新页面）
4. 对比快照差异：多了的 = 下单结果，少了的 = 撤单结果，变了的 = 成交更新
5. 将差异匹配到对应操作
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
    1. 操作前拍摄快照
    2. 监听 WebSocket 事件确认订单执行状态
    3. 超时后刷新数据并与快照对比差异
    4. 将差异匹配到对应操作，确定成功/失败
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

        # 操作前快照
        # Polymarket: {condition_id: {order_id: PolymarketOrder}}
        self._poly_snapshot: dict[str, dict[str, Any]] = {}
        # OrbitExch: {market_id: {offerId: bet_dict}}
        self._orbit_snapshot: dict[str, dict[str, dict]] = {}

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

    def _polymarket_snapshot_key(self, operation: OrderOperation) -> tuple[str, str, str]:
        """
        生成 Polymarket 快照 key

        返回 (key, key_type, key_value)
        """
        if operation.condition_id:
            return f"condition:{operation.condition_id}", "condition", operation.condition_id
        if operation.token_id:
            return f"asset:{operation.token_id}", "asset", operation.token_id
        return "", "", ""

    # =========================================================================
    # 快照
    # =========================================================================

    def take_snapshot(self, operations: list[OrderOperation]) -> None:
        """
        操作前拍摄快照

        记录当前各市场的订单状态，用于操作后对比差异。
        在 orchestrator 执行操作之前调用。

        Args:
            operations: 即将执行的操作列表
        """
        self._poly_snapshot.clear()
        self._orbit_snapshot.clear()

        for op in operations:
            if op.venue == OperationVenue.POLYMARKET:
                key, key_type, key_value = self._polymarket_snapshot_key(op)
                if key and key not in self._poly_snapshot and self._polymarket_client:
                    if key_type == "condition":
                        orders = self._polymarket_client.get_current_orders(
                            condition_id=key_value
                        )
                    else:
                        orders = self._polymarket_client.get_current_orders(
                            asset_id=key_value
                        )
                    self._poly_snapshot[key] = {o.order_id: o for o in orders}
                    self._log.debug(
                        f"Snapshot polymarket {key}: {len(self._poly_snapshot[key])} orders"
                    )

            elif op.venue == OperationVenue.ORBITEXCH:
                mid = op.market_id
                if mid and mid not in self._orbit_snapshot and self._orbitexch_client:
                    bets = self._orbitexch_client.get_current_bets(mid)
                    self._orbit_snapshot[mid] = {
                        str(b.get("offerId", "")): b for b in bets
                    }
                    self._log.debug(
                        f"Snapshot orbitexch {mid}: {len(self._orbit_snapshot[mid])} bets"
                    )

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
            if op_result.get("venue_order_id") and operation.operation_type in (
                OperationType.PLACE,
                OperationType.MODIFY,
            ):
                # 有下单回执时先标记为已确认挂单，避免被误判为失败
                self._results[key].status = TrackingStatus.CONFIRMED
                self._results[key].size_matched = 0.0
                self._results[key].size_remaining = operation.size

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

        # 超时后，刷新数据并对比快照（仍有未完全成交的操作）
        if not self._all_fully_filled():
            self._log.info("Timeout reached, refreshing and comparing snapshots")
            await self._refresh_and_diff()

        return self._summarize_results()

    def _all_fully_filled(self) -> bool:
        """
        检查是否所有操作都已完成（下单完全成交 / 撤单已确认）

        只有全部操作都成功且完全成交才返回 True。
        任何操作失败都视为未完成，继续等待直到超时。
        """
        for r in self._results.values():
            if r.status in (TrackingStatus.PENDING, TrackingStatus.FAILED):
                return False
            # PLACE/MODIFY: 必须 size_remaining == 0 才算完全成交
            if r.operation.operation_type in (OperationType.PLACE, OperationType.MODIFY):
                if r.size_remaining > 0:
                    return False
        return True

    # =========================================================================
    # 快照对比
    # =========================================================================

    async def _refresh_and_diff(self) -> None:
        """
        刷新数据并与操作前快照对比

        1. Polymarket: 调 REST API 获取最新订单
        2. OrbitExch: 刷新页面后读取缓存
        3. 对比前后差异，匹配到对应操作
        """
        # 收集需要刷新的市场（包含未完全成交的操作，不仅限于 PENDING）
        poly_keys: dict[str, tuple[str, str]] = {}
        orbit_markets = set()

        for result in self._results.values():
            # 跳过已失败或已完全成交的操作
            if result.status == TrackingStatus.FAILED:
                continue
            if result.status == TrackingStatus.CONFIRMED and result.size_remaining <= 0:
                continue
            op = result.operation
            if op.venue == OperationVenue.POLYMARKET:
                key, key_type, key_value = self._polymarket_snapshot_key(op)
                if key:
                    poly_keys[key] = (key_type, key_value)
            elif op.venue == OperationVenue.ORBITEXCH and op.market_id:
                orbit_markets.add(op.market_id)

        # 刷新 Polymarket（调 REST API 获取最新订单）
        poly_after: dict[str, dict[str, Any]] = {}
        if poly_keys and self._polymarket_client:
            try:
                # fetch_open_orders 会更新内存缓存
                await self._polymarket_client.fetch_open_orders()
                for key, (key_type, key_value) in poly_keys.items():
                    if key_type == "condition":
                        orders = self._polymarket_client.get_current_orders(
                            condition_id=key_value
                        )
                    else:
                        orders = self._polymarket_client.get_current_orders(
                            asset_id=key_value
                        )
                    poly_after[key] = {o.order_id: o for o in orders}
            except Exception as e:
                self._log.error(f"Failed to refresh Polymarket orders: {e}")

        # 刷新 OrbitExch（刷新页面）
        orbit_after: dict[str, dict[str, dict]] = {}
        if orbit_markets and self._orbitexch_client:
            try:
                await self._orbitexch_client.refresh_page()
                # 等待 CURRENT_BETS 推送更新缓存
                await asyncio.sleep(3)
                for mid in orbit_markets:
                    bets = self._orbitexch_client.get_current_bets(mid)
                    orbit_after[mid] = {
                        str(b.get("offerId", "")): b for b in bets
                    }
            except Exception as e:
                self._log.error(f"Failed to refresh OrbitExch bets: {e}")

        # 对比差异并匹配操作
        self._match_polymarket_diff(poly_after)
        self._match_orbitexch_diff(orbit_after)

    def _match_polymarket_diff(self, after: dict[str, dict[str, Any]]) -> None:
        """
        对比 Polymarket 快照差异，匹配到操作

        Args:
            after: 刷新后的订单状态 {condition_id: {order_id: order}}
        """
        for rkey, result in self._results.items():
            # 跳过已失败或已完全成交的操作
            if result.status == TrackingStatus.FAILED:
                continue
            if result.status == TrackingStatus.CONFIRMED and result.size_remaining <= 0:
                continue
            op = result.operation
            if op.venue != OperationVenue.POLYMARKET:
                continue

            key, _, _ = self._polymarket_snapshot_key(op)
            before = self._poly_snapshot.get(key, {})
            current = after.get(key, {})

            if op.operation_type == OperationType.PLACE:
                # 下单：找新增的、token_id 匹配的订单
                added_ids = set(current.keys()) - set(before.keys())
                for oid in added_ids:
                    order = current[oid]
                    if order.asset_id == op.token_id:
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = order.order_id
                        result.size_matched = order.size_matched
                        result.size_remaining = order.original_size - order.size_matched
                        self._log.info(
                            f"Polymarket PLACE confirmed via diff: "
                            f"order_id={oid}, matched={result.size_matched}"
                        )
                        break
                else:
                    # 也检查 before 中已有但 sizeMatched 变化的（极端情况：ws 推送了 placement 但没触发事件）
                    for oid in set(before.keys()) & set(current.keys()):
                        order = current[oid]
                        old_order = before[oid]
                        if (order.asset_id == op.token_id
                                and order.size_matched > old_order.size_matched):
                            result.status = TrackingStatus.CONFIRMED
                            result.venue_order_id = order.order_id
                            result.size_matched = order.size_matched
                            result.size_remaining = order.original_size - order.size_matched
                            self._log.info(
                                f"Polymarket PLACE confirmed via size change: "
                                f"order_id={oid}, matched={result.size_matched}"
                            )
                            break
                    else:
                        result.status = TrackingStatus.FAILED
                        result.error_message = "Order not found in diff"

            elif op.operation_type == OperationType.CANCEL:
                # 撤单：优先按订单号确认
                if result.venue_order_id and result.venue_order_id not in current:
                    result.status = TrackingStatus.CONFIRMED
                    self._log.info(
                        f"Polymarket CANCEL confirmed via missing order_id: "
                        f"order_id={result.venue_order_id}"
                    )
                    continue
                # 撤单：找消失的、token_id 匹配的订单
                removed_ids = set(before.keys()) - set(current.keys())
                for oid in removed_ids:
                    old_order = before[oid]
                    if old_order.asset_id == op.token_id:
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = oid
                        self._log.info(
                            f"Polymarket CANCEL confirmed via diff: "
                            f"order_id={oid}, token_id={op.token_id}"
                        )
                        break
                else:
                    result.status = TrackingStatus.FAILED
                    result.error_message = "Cancelled order not found in diff"

            elif op.operation_type == OperationType.MODIFY:
                # 修改提交：找 token_id 匹配且 sizeMatched 增加的订单
                for oid in set(before.keys()) & set(current.keys()):
                    order = current[oid]
                    old_order = before[oid]
                    if (order.asset_id == op.token_id
                            and order.size_matched > old_order.size_matched):
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = order.order_id
                        result.size_matched = order.size_matched
                        result.size_remaining = order.original_size - order.size_matched
                        self._log.info(
                            f"Polymarket MODIFY confirmed via size change: "
                            f"order_id={oid}, matched={result.size_matched}"
                        )
                        break
                else:
                    result.status = TrackingStatus.FAILED
                    result.error_message = "Modified order not found in diff"

    def _match_orbitexch_diff(self, after: dict[str, dict[str, dict]]) -> None:
        """
        对比 OrbitExch 快照差异，匹配到操作

        Args:
            after: 刷新后的 bet 状态 {market_id: {offerId: bet_dict}}
        """
        for key, result in self._results.items():
            # 跳过已失败或已完全成交的操作
            if result.status == TrackingStatus.FAILED:
                continue
            if result.status == TrackingStatus.CONFIRMED and result.size_remaining <= 0:
                continue
            op = result.operation
            if op.venue != OperationVenue.ORBITEXCH:
                continue

            mid = op.market_id
            before = self._orbit_snapshot.get(mid, {})
            current = after.get(mid, {})

            if op.operation_type == OperationType.PLACE:
                # 下单：找新增的、selectionId + side 匹配的 bet
                added_ids = set(current.keys()) - set(before.keys())
                for offer_id in added_ids:
                    bet = current[offer_id]
                    if (str(bet.get("selectionId")) == op.selection_id
                            and bet.get("side", "").upper() == "BACK"):
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = offer_id
                        result.size_matched = float(bet.get("sizeMatched", 0))
                        result.size_remaining = float(bet.get("sizeRemaining", 0))
                        self._log.info(
                            f"OrbitExch PLACE confirmed via diff: "
                            f"offerId={offer_id}, matched={result.size_matched}"
                        )
                        break
                else:
                    # 检查已有 bet 的 sizeMatched 是否变化
                    for offer_id in set(before.keys()) & set(current.keys()):
                        bet = current[offer_id]
                        old_bet = before[offer_id]
                        if (str(bet.get("selectionId")) == op.selection_id
                                and bet.get("side", "").upper() == "BACK"
                                and float(bet.get("sizeMatched", 0)) > float(old_bet.get("sizeMatched", 0))):
                            result.status = TrackingStatus.CONFIRMED
                            result.venue_order_id = offer_id
                            result.size_matched = float(bet.get("sizeMatched", 0))
                            result.size_remaining = float(bet.get("sizeRemaining", 0))
                            self._log.info(
                                f"OrbitExch PLACE confirmed via size change: "
                                f"offerId={offer_id}, matched={result.size_matched}"
                            )
                            break
                    else:
                        result.status = TrackingStatus.FAILED
                        result.error_message = "Bet not found in diff"

            elif op.operation_type == OperationType.CANCEL:
                # 撤单：优先按 offerId 确认
                if result.venue_order_id:
                    if result.venue_order_id not in current:
                        result.status = TrackingStatus.CONFIRMED
                        self._log.info(
                            f"OrbitExch CANCEL confirmed via missing offerId: "
                            f"offerId={result.venue_order_id}"
                        )
                        continue
                    bet = current.get(result.venue_order_id, {})
                    if float(bet.get("sizeRemaining", 0)) == 0.0:
                        result.status = TrackingStatus.CONFIRMED
                        self._log.info(
                            f"OrbitExch CANCEL confirmed via sizeRemaining=0: "
                            f"offerId={result.venue_order_id}"
                        )
                        continue
                # 撤单：找消失的、selectionId + side 匹配的 bet
                removed_ids = set(before.keys()) - set(current.keys())
                for offer_id in removed_ids:
                    old_bet = before[offer_id]
                    if (str(old_bet.get("selectionId")) == op.selection_id
                            and old_bet.get("side", "").upper() == "BACK"):
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = offer_id
                        self._log.info(
                            f"OrbitExch CANCEL confirmed via diff: "
                            f"offerId={offer_id}, selectionId={op.selection_id}"
                        )
                        break
                else:
                    # 允许订单仍在列表但 sizeRemaining=0 的情况
                    for offer_id in current.keys():
                        bet = current[offer_id]
                        if (str(bet.get("selectionId")) == op.selection_id
                                and bet.get("side", "").upper() == "BACK"
                                and float(bet.get("sizeRemaining", 0)) == 0.0):
                            result.status = TrackingStatus.CONFIRMED
                            result.venue_order_id = offer_id
                            self._log.info(
                                f"OrbitExch CANCEL confirmed via sizeRemaining=0: "
                                f"offerId={offer_id}, selectionId={op.selection_id}"
                            )
                            break
                    else:
                        result.status = TrackingStatus.FAILED
                        result.error_message = "Cancelled bet not found in diff"

            elif op.operation_type == OperationType.MODIFY:
                # 修改提交：找 selectionId + side 匹配且 sizeMatched 增加的 bet
                for offer_id in set(before.keys()) & set(current.keys()):
                    bet = current[offer_id]
                    old_bet = before[offer_id]
                    if (str(bet.get("selectionId")) == op.selection_id
                            and bet.get("side", "").upper() == "BACK"
                            and float(bet.get("sizeMatched", 0)) > float(old_bet.get("sizeMatched", 0))):
                        result.status = TrackingStatus.CONFIRMED
                        result.venue_order_id = offer_id
                        result.size_matched = float(bet.get("sizeMatched", 0))
                        result.size_remaining = float(bet.get("sizeRemaining", 0))
                        self._log.info(
                            f"OrbitExch MODIFY confirmed via size change: "
                            f"offerId={offer_id}, matched={result.size_matched}"
                        )
                        break
                else:
                    result.status = TrackingStatus.FAILED
                    result.error_message = "Modified bet not found in diff"

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
