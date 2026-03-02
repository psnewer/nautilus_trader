"""
执行编排器 (Execution Orchestrator)

协调执行会话、规划器、追踪器完成完整的订单执行流程。
"""

import asyncio
import logging
import time
from typing import Any, Callable

from .config import ExecutionConfig
from .session import (
    ExecutionSession,
    SessionPhase,
    SessionEndReason,
    OutcomeShares,
    OutcomeProbabilities,
)
from .planner import ExecutionPlanner, ExecutionPlan, OrderOperation, OperationType, OperationVenue
from .tracker import OrderTracker, TrackingStatus, TrackingResult, BatchTrackingResult
from .models import Order, OrderSide, OrderType, Venue
from src.arbitrage.common.utils import check_min_size


class ExecutionOrchestrator:
    """
    执行编排器

    职责：
    1. 管理执行会话生命周期
    2. 协调 planner -> executor -> tracker 循环
    3. 处理补救逻辑
    """

    def __init__(
        self,
        config: ExecutionConfig,
        order_executor: Callable,
        order_canceller: Callable,
        order_info_getter: Callable,
        probabilities_getter: Callable,
        orbitexch_modify_and_take: Callable | None = None,
        logger: logging.Logger | None = None,
    ):
        """
        Args:
            config: 执行配置
            order_executor: 订单执行函数 async (Order) -> ExecutionResult
            order_canceller: 订单撤销函数 async (order_id) -> CancelResult
            order_info_getter: 订单信息获取函数 (pair_id, market_type) -> order_info
            probabilities_getter: 实时概率获取函数 (pair_id) -> {outcome: probability}
            orbitexch_modify_and_take: OrbitExch 修改并 Take 函数 async (order, new_size) -> ExecutionResult
        """
        self._config = config
        self._execute_order = order_executor
        self._cancel_order = order_canceller
        self._get_order_info = order_info_getter
        self._get_probabilities = probabilities_getter
        self._orbitexch_modify_and_take = orbitexch_modify_and_take
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 组件
        self._planner = ExecutionPlanner(logger=self._log)
        self._tracker = OrderTracker(
            timeout=config.tracking_timeout_sec,
            logger=self._log,
        )

        # 活跃会话
        self._sessions: dict[str, ExecutionSession] = {}  # session_id -> session

        # pair_id -> session_id 映射（用于互斥检查）
        # 每个 pair 同时只能有一个活跃 session
        self._active_pair_sessions: dict[str, str] = {}  # pair_id -> session_id

        # 回调
        self._session_callbacks: list[Callable[[ExecutionSession], None]] = []

    def update_config(self, config: ExecutionConfig) -> None:
        """更新执行配置"""
        self._config = config
        self._tracker.update_timeout(config.tracking_timeout_sec)

    def set_polymarket_client(self, client) -> None:
        """设置 Polymarket 客户端（用于追踪）"""
        self._tracker.set_polymarket_client(client)

    def set_orbitexch_client(self, client) -> None:
        """设置 OrbitExch 客户端（用于追踪）"""
        self._tracker.set_orbitexch_client(client)

    def has_active_session(self, pair_id: str) -> bool:
        """
        检查指定 pair 是否有活跃的执行会话

        每个 pair 同时只能有一个活跃 session，防止重复执行。

        Args:
            pair_id: 比赛 ID

        Returns:
            是否有活跃 session
        """
        if pair_id not in self._active_pair_sessions:
            return False

        session_id = self._active_pair_sessions[pair_id]
        session = self._sessions.get(session_id)

        if not session:
            # session 不存在，清理映射
            del self._active_pair_sessions[pair_id]
            return False

        # 检查 session 是否仍在活跃状态
        if session.phase in (SessionPhase.COMPLETED, SessionPhase.FAILED):
            # session 已结束，清理映射
            del self._active_pair_sessions[pair_id]
            return False

        return True

    def get_active_session_for_pair(self, pair_id: str) -> ExecutionSession | None:
        """获取指定 pair 的活跃 session"""
        if pair_id in self._active_pair_sessions:
            session_id = self._active_pair_sessions[pair_id]
            return self._sessions.get(session_id)
        return None

    async def execute_opportunity(
        self,
        pair_id: str,
        opportunity_id: str,
        legs: list[dict],
        target_shares: dict[str, float],
        probabilities: dict[str, float],
    ) -> ExecutionSession | None:
        """
        执行套利机会

        完整的执行流程：
        plan -> operate -> track -> (recovery loop) -> complete

        注意：每个 pair 同时只能有一个活跃 session，如果已有活跃 session 则拒绝执行。

        Args:
            pair_id: 比赛 ID
            opportunity_id: 机会 ID
            legs: 套利腿列表
            target_shares: 目标 share {home, draw, away}
            probabilities: 初始概率 {home, draw, away}

        Returns:
            执行会话（包含最终状态），如果被拒绝则返回 None
        """
        # 检查是否有活跃 session（互斥检查）
        if self.has_active_session(pair_id):
            active_session = self.get_active_session_for_pair(pair_id)
            self._log.warning(
                f"Opportunity {opportunity_id} rejected: pair {pair_id} "
                f"has active session {active_session.session_id if active_session else 'unknown'} "
                f"(phase={active_session.phase.value if active_session else 'unknown'})"
            )
            return None

        # 创建会话
        session = ExecutionSession.create(
            pair_id=pair_id,
            opportunity_id=opportunity_id,
            target_shares=target_shares,
            probabilities=probabilities,
            max_failure_retries=self._config.max_failure_retries,
        )
        self._sessions[session.session_id] = session

        # 注册 pair -> session 映射
        self._active_pair_sessions[pair_id] = session.session_id

        self._log.info(
            f"Session {session.session_id} created: "
            f"pair={pair_id}, target={target_shares}"
        )

        try:
            # 执行主循环
            await self._execute_session(session, legs)
        except Exception as e:
            self._log.error(f"Session {session.session_id} error: {e}")
            session.fail(SessionEndReason.ERROR)
        finally:
            # 清理 pair -> session 映射
            if pair_id in self._active_pair_sessions:
                del self._active_pair_sessions[pair_id]

        self._notify_session_update(session)
        return session

    async def _execute_session(
        self,
        session: ExecutionSession,
        legs: list[dict],
    ) -> None:
        """
        执行会话主循环

        流程：
        1. 初始规划并执行
        2. 追踪结果
        3. 达标 → 结束；零成交 → 结束
        4. 未达标 → 进入 recovery 循环

        Args:
            session: 执行会话
            legs: 套利腿列表
        """
        # 全 session 生命周期的追踪结果（跨所有轮次累加）
        all_tracking_results: list[TrackingResult] = []

        # 记录执行开始时间戳（毫秒），用于 recovery 刷新时过滤本 session 的订单
        session_start_ms = int(time.time() * 1000)

        # 从 legs 提取 outcome -> venue 映射，供 recovery 使用
        for leg in legs:
            mt = leg.get("market_type", "")
            venue_str = leg.get("venue", "")
            if mt and venue_str:
                session.outcome_venues[mt] = venue_str

        # === 初始轮 ===
        session.update_phase(SessionPhase.PLANNING)
        initial_plan = self._planner.plan_initial(
            session=session,
            legs=legs,
            order_info_getter=self._get_order_info,
        )

        if not initial_plan.operations:
            self._log.warning(f"Session {session.session_id}: no operations in initial plan")
            session.complete(SessionEndReason.ZERO_FILL)
            return

        session.update_phase(SessionPhase.OPERATING)
        self._tracker.take_snapshot(initial_plan.operations)
        operation_results = await self._execute_operations(session, initial_plan)

        session.update_phase(SessionPhase.TRACKING)
        tracking_result = await self._tracker.track_operations(
            initial_plan.operations,
            operation_results,
        )

        all_tracking_results.extend(tracking_result.results)
        self._update_session_filled(session, all_tracking_results)
        session.has_open_orders = bool(self._get_pending_results(all_tracking_results))
        if self._should_fail_for_tracking(session, operation_results, tracking_result):
            session.fail(SessionEndReason.MAX_FAILURE_RETRIES)
            return

        initial_plan_operations = [
            op for op in initial_plan.operations
            if op.operation_type in {OperationType.PLACE, OperationType.MODIFY}
        ]

        if self._is_plan_operations_filled(all_tracking_results, initial_plan_operations):
            self._log.info(f"Session {session.session_id}: initial plan operations filled")
            session.complete(SessionEndReason.TARGET_MET)
            return

        if session.is_zero_fill():
            if session.has_open_orders:
                self._log.info(
                    f"Session {session.session_id}: zero fill but pending orders, "
                    f"entering recovery"
                )
            else:
                self._log.info(f"Session {session.session_id}: zero fill, minimal loss")
                session.complete(SessionEndReason.ZERO_FILL)
                return

        # === Recovery 循环 ===
        # 初始阶段：last 和 current 都是 strategy 传入的目标
        initial_target = session.initial_target
        await self._recovery_loop(
            session, all_tracking_results, session_start_ms,
            last_plan_target=initial_target,
            current_plan_target=initial_target,
            last_plan_operations=initial_plan_operations,
            current_plan_operations=initial_plan_operations,
        )

    async def _execute_operations(
        self,
        session: ExecutionSession,
        plan: ExecutionPlan,
    ) -> list[dict]:
        """
        执行操作计划

        Args:
            session: 执行会话
            plan: 执行计划

        Returns:
            操作结果列表
        """
        self._log.info(
            f"Session {session.session_id}: executing {len(plan.operations)} operations"
        )

        results = []
        tasks = []

        for operation in plan.operations:
            if operation.operation_type == OperationType.PLACE:
                task = self._execute_place_operation(session, operation)
            elif operation.operation_type == OperationType.CANCEL:
                task = self._execute_cancel_operation(session, operation)
            elif operation.operation_type == OperationType.MODIFY:
                task = self._execute_modify_operation(session, operation)
            else:
                self._log.warning(f"Unknown operation type: {operation.operation_type}")
                continue
            tasks.append(task)

        # 并行执行
        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(task_results):
            if isinstance(result, Exception):
                self._log.error(f"Operation {i} error: {result}")
                results.append({"success": False, "error": str(result)})
            else:
                results.append(result)

        return results

    async def _execute_place_operation(
        self,
        session: ExecutionSession,
        operation: OrderOperation,
    ) -> dict:
        """执行下单操作"""
        # 构建订单
        venue = Venue.POLYMARKET if operation.venue == OperationVenue.POLYMARKET else Venue.ORBITEXCH

        if venue == Venue.POLYMARKET:
            order = Order(
                venue=venue,
                pair_id=session.pair_id,
                market_type=operation.market_type,
                token_id=operation.token_id,
                condition_id=operation.condition_id,
                side=OrderSide.BUY,
                price=operation.price,
                size=operation.size,
                order_type=OrderType.GTC,
                metadata={
                    "session_id": session.session_id,
                    "operation_type": "place",
                },
            )
        else:
            order = Order(
                venue=venue,
                pair_id=session.pair_id,
                market_type=operation.market_type,
                market_id=operation.market_id,
                selection_id=operation.selection_id,
                side=OrderSide.BACK,
                price=operation.price,
                size=operation.size,
                order_type=OrderType.GTC,
                metadata={
                    "session_id": session.session_id,
                    "operation_type": "place",
                },
            )

        result = await self._execute_order(order)

        return {
            "success": result.success,
            "venue_order_id": order.venue_order_id or order.order_id,
            "order_id": order.order_id,
            "message": result.message if hasattr(result, 'message') else "",
        }

    async def _execute_cancel_operation(
        self,
        session: ExecutionSession,
        operation: OrderOperation,
    ) -> dict:
        """执行撤单操作"""
        result = await self._cancel_order(operation.order_id)

        return {
            "success": result.success,
            "order_id": operation.order_id,
            "venue_order_id": operation.order_id,
            "message": result.message if hasattr(result, 'message') else "",
        }

    async def _execute_modify_operation(
        self,
        session: ExecutionSession,
        operation: OrderOperation,
    ) -> dict:
        """
        执行修改操作（修改 size 后按市价执行）

        对于 OrbitExch: 修改 Stake 输入框后点击 Take 按钮
        对于 Polymarket: 不支持修改，使用 FOK 模式避免此情况
        """
        if operation.venue == OperationVenue.ORBITEXCH:
            if self._orbitexch_modify_and_take:
                # 构建临时订单对象用于修改
                order = Order(
                    venue=Venue.ORBITEXCH,
                    pair_id=session.pair_id,
                    market_type=operation.market_type,
                    market_id=operation.market_id,
                    selection_id=operation.selection_id,
                    side=OrderSide.BACK,
                    price=operation.price,
                    size=operation.size,
                    order_type=OrderType.GTC,
                )
                order.venue_order_id = operation.order_id

                self._log.info(
                    f"Modifying OrbitExch order and taking at market: "
                    f"order_id={operation.order_id}, new_size={operation.size}"
                )

                result = await self._orbitexch_modify_and_take(order, operation.size)

                return {
                    "success": result.success,
                    "order_id": operation.order_id,
                    "message": result.message if hasattr(result, 'message') else "",
                }
            else:
                self._log.warning("OrbitExch modify_and_take not configured")
                return {
                    "success": False,
                    "order_id": operation.order_id,
                    "message": "OrbitExch modify_and_take not configured",
                }
        else:
            # Polymarket 使用 FOK，不应该有部分成交需要修改的情况
            self._log.warning(
                f"Modify not supported for {operation.venue.value} (using FOK mode)"
            )
            return {
                "success": False,
                "order_id": operation.order_id,
                "message": f"Modify not supported for {operation.venue.value}",
            }

    async def _recovery_loop(
        self,
        session: ExecutionSession,
        all_tracking_results: list[TrackingResult],
        session_start_ms: int,
        last_plan_target: OutcomeShares,
        current_plan_target: OutcomeShares,
        last_plan_operations: list[OrderOperation],
        current_plan_operations: list[OrderOperation],
    ) -> None:
        """
        Recovery 循环

        两个规划目标同时存在：
        - last_plan_target: 上一轮规划目标（循环顶部检查：上轮订单可能在间隙成交）
        - current_plan_target: 本轮规划目标（执行后检查）
        两个规划操作列表同时存在：
        - last_plan_operations: 上一轮规划的下单/修改操作（判断是否全部成交）
        - current_plan_operations: 本轮规划的下单/修改操作（执行后检查）
        新规划时：last = current（旧的"新"变"旧"），current = 新目标
        新规划时：last_ops = current_ops，current_ops = 新规划的下单/修改操作

        每轮流程：
        1. 刷新成交 → 检查 last_plan_operations 是否全部成交 → 是则结束
        2. 检查挂单 → 有挂单则先撤单
        3. 无挂单 → 规划新订单 → last = current, current = 新目标，更新对应操作列表
        4. 操作前新成交检查 → 有变化则回到步骤 1（检查 last）
        5. 执行 → 追踪
        6. 撤单轮：回到步骤 1；下单轮：检查 current 是否全部成交

        Args:
            session: 执行会话
            all_tracking_results: 全 session 的追踪结果列表（跨轮次累加）
            session_start_ms: session 开始时间戳（毫秒）
            last_plan_target: 上一轮规划目标
            current_plan_target: 本轮规划目标
            last_plan_operations: 上一轮规划的下单/修改操作
            current_plan_operations: 本轮规划的下单/修改操作
        """
        while session.needs_recovery():
            session.increment_retry()
            self._log.info(
                f"Session {session.session_id}: recovery round {session.retry_count}"
            )

            # =============================================
            # 1. 刷新成交，检查上一轮规划是否全部成交
            # =============================================
            await self._refresh_tracking_results(all_tracking_results, session_start_ms)
            self._update_session_filled(session, all_tracking_results)
            session.has_open_orders = bool(self._get_pending_results(all_tracking_results))

            if self._is_plan_operations_filled(all_tracking_results, last_plan_operations):
                self._log.info(
                    f"Session {session.session_id}: last plan operations filled"
                )
                session.complete(SessionEndReason.TARGET_MET)
                return

            # =============================================
            # 2. 检查挂单（size_remaining > 0 的订单）
            # =============================================
            pending = self._get_pending_results(all_tracking_results)

            if pending:
                # 有挂单 → 本轮只做撤单
                self._log.info(
                    f"Session {session.session_id}: {len(pending)} pending orders, cancelling"
                )
                plan = self._create_cancel_plan(pending)
            else:
                # 无挂单 → 规划新订单
                current_probs = self._get_probabilities(session.pair_id)
                if not current_probs:
                    self._log.error(f"Session {session.session_id}: cannot get probabilities")
                    session.fail(SessionEndReason.ERROR)
                    return

                session.update_phase(SessionPhase.PLANNING)
                plan = self._planner.plan_recovery(
                    session=session,
                    current_probabilities=OutcomeProbabilities.from_dict(current_probs),
                    pending_orders=[],
                    order_info_getter=self._get_order_info,
                )

                if not plan.operations:
                    self._log.info(f"Session {session.session_id}: no recovery operations needed")
                    break

                # 无挂单且新规划订单不满足最小 size → 上一轮订单全部完成
                if self._recovery_below_min_size(plan):
                    self._log.info(
                        f"Session {session.session_id}: recovery orders below minimum size, "
                        f"considering previous plan complete"
                    )
                    session.complete(SessionEndReason.TARGET_MET)
                    return

                # 旧的 current 变成 last，新规划变成 current
                last_plan_target = current_plan_target
                current_plan_target = OutcomeShares.from_dict(session.adjusted_target.to_dict())
                last_plan_operations = current_plan_operations
                current_plan_operations = [
                    op for op in plan.operations
                    if op.operation_type in {OperationType.PLACE, OperationType.MODIFY}
                ]

            # =============================================
            # 3. 操作前新成交检查
            # =============================================
            old_filled = session.filled.to_dict()
            await self._refresh_tracking_results(all_tracking_results, session_start_ms)
            self._update_session_filled(session, all_tracking_results)

            if session.filled.to_dict() != old_filled:
                self._log.info(
                    f"Session {session.session_id}: new fills during planning, "
                    f"old={old_filled}, new={session.filled.to_dict()}, re-planning"
                )
                # 回到循环顶部，检查 last_plan_operations 是否全部成交
                continue

            # =============================================
            # 4. 执行
            # =============================================
            session.update_phase(SessionPhase.OPERATING)
            self._tracker.take_snapshot(plan.operations)
            operation_results = await self._execute_operations(session, plan)

            # =============================================
            # 5. 追踪
            # =============================================
            session.update_phase(SessionPhase.TRACKING)
            tracking_result = await self._tracker.track_operations(
                plan.operations,
                operation_results,
            )
            if self._should_fail_for_tracking(session, operation_results, tracking_result):
                session.fail(SessionEndReason.MAX_FAILURE_RETRIES)
                return

            # =============================================
            # 6. 根据操作类型处理结果
            # =============================================
            if plan.has_cancels:
                # 撤单轮：标记已撤销，回到循环顶部进入新规划
                confirmed_cancel_ids = {
                    r.venue_order_id
                    for r in tracking_result.results
                    if r.operation.operation_type == OperationType.CANCEL
                    and r.status == TrackingStatus.CONFIRMED
                    and r.venue_order_id
                }
                self._mark_cancelled(all_tracking_results, pending, confirmed_cancel_ids)
                self._log.info(f"Session {session.session_id}: cancels done, re-planning")
                continue

            # 下单/修改轮：累加追踪结果
            all_tracking_results.extend(tracking_result.results)
            self._update_session_filled(session, all_tracking_results)

            if self._is_plan_operations_filled(all_tracking_results, current_plan_operations):
                self._log.info(
                    f"Session {session.session_id}: current plan operations filled"
                )
                session.complete(SessionEndReason.TARGET_MET)
                return

        # 循环结束
        if session.needs_recovery():
            self._log.warning(f"Session {session.session_id}: recovery incomplete")
            session.fail(SessionEndReason.MAX_FAILURE_RETRIES)
        else:
            session.complete(SessionEndReason.TARGET_MET)

    def _is_plan_operations_filled(
        self,
        all_results: list[TrackingResult],
        plan_operations: list[OrderOperation],
    ) -> bool:
        """
        检查规划中的下单/修改操作是否全部成交

        以 size_matched >= 操作 size 为准（允许小误差），撤单不参与判断。

        Args:
            all_results: 全 session 的追踪结果
            plan_operations: 规划的下单/修改操作列表
        """
        if not plan_operations:
            return True

        result_by_op_id = {id(r.operation): r for r in all_results}
        epsilon = 0.01

        for op in plan_operations:
            result = result_by_op_id.get(id(op))
            if not result:
                return False
            if result.status in {TrackingStatus.FAILED, TrackingStatus.TIMEOUT}:
                return False
            if op.size > epsilon and result.size_matched < op.size - epsilon:
                return False

        return True

    def _recovery_below_min_size(self, plan: ExecutionPlan) -> bool:
        """
        检查 recovery 规划的订单是否全部低于最小 size

        如果所有操作都不满足最小 size，认为上一轮订单已实质完成。

        Args:
            plan: recovery 规划

        Returns:
            True 表示所有操作都低于最小 size
        """
        place_or_modify = [
            op for op in plan.operations
            if op.operation_type in {OperationType.PLACE, OperationType.MODIFY}
        ]
        if not place_or_modify:
            return False

        for op in place_or_modify:
            if check_min_size(op.venue.value, op.size):
                return False

        return True

    def _should_fail_for_tracking(
        self,
        session: ExecutionSession,
        operation_results: list[dict],
        tracking_result: BatchTrackingResult,
    ) -> bool:
        """
        根据操作反馈与追踪结果判断是否累计失败次数

        失败条件：
        - 操作结果中存在 success=False
        - 追踪结果中存在 FAILED/TIMEOUT
        """
        has_failure = any(
            not result.get("success", True)
            for result in operation_results
        )
        has_failure = has_failure or any(
            result.status in {TrackingStatus.FAILED, TrackingStatus.TIMEOUT}
            for result in tracking_result.results
        )

        if not has_failure:
            return False

        if session.increment_failure():
            self._log.warning(
                f"Session {session.session_id}: failure retry "
                f"{session.failure_count}/{session.max_failure_retries}"
            )
            return False

        self._log.warning(f"Session {session.session_id}: max failure retries reached")
        return True

    def _update_session_filled(
        self,
        session: ExecutionSession,
        all_results: list[TrackingResult],
    ) -> None:
        """
        根据全部追踪结果更新会话已成交数量

        从所有轮次的追踪结果中聚合计算总 filled。

        Args:
            session: 执行会话
            all_results: 全 session 生命周期的所有追踪结果
        """
        filled = {"home": 0.0, "draw": 0.0, "away": 0.0}

        for result in all_results:
            market_type = result.operation.market_type
            if market_type in filled:
                # 根据平台类型转换 size_matched 为 share
                if result.operation.venue == OperationVenue.POLYMARKET:
                    # Polymarket: size_matched 就是 share
                    filled[market_type] += result.size_matched
                elif result.operation.venue == OperationVenue.ORBITEXCH:
                    # OrbitExch: share = size_matched * odds
                    odds = result.operation.price
                    if odds > 0:
                        filled[market_type] += result.size_matched * odds
                    else:
                        filled[market_type] += result.size_matched

        session.update_filled(filled)
        self._log.info(
            f"Session {session.session_id}: filled updated to {filled}"
        )

    # =========================================================================
    # 挂单管理
    # =========================================================================

    def _get_pending_results(
        self,
        all_results: list[TrackingResult],
    ) -> list[TrackingResult]:
        """
        获取仍有挂单的追踪结果（size_remaining > 0）

        这些订单在交易所上还有未成交的部分，需要在重新规划前先撤销。

        Args:
            all_results: 全 session 的追踪结果

        Returns:
            有挂单的追踪结果列表
        """
        return [
            r for r in all_results
            if r.size_remaining > 0 and r.status != TrackingStatus.FAILED
        ]

    def _create_cancel_plan(
        self,
        pending_results: list[TrackingResult],
    ) -> ExecutionPlan:
        """
        根据挂单创建撤单计划

        Args:
            pending_results: 有挂单的追踪结果

        Returns:
            只包含撤单操作的执行计划
        """
        operations = []
        for result in pending_results:
            op = result.operation
            cancel_op = OrderOperation(
                operation_type=OperationType.CANCEL,
                venue=op.venue,
                market_type=op.market_type,
                order_id=result.venue_order_id,
                market_id=op.market_id,
                selection_id=op.selection_id,
                token_id=op.token_id,
                condition_id=op.condition_id,
            )
            operations.append(cancel_op)

        return ExecutionPlan(
            operations=operations,
            has_cancels=True,
            description=f"Cancel {len(operations)} pending orders",
        )

    def _mark_cancelled(
        self,
        all_results: list[TrackingResult],
        cancelled_results: list[TrackingResult],
        confirmed_cancel_ids: set[str],
    ) -> None:
        """
        撤单完成后，标记对应追踪结果的 size_remaining 为 0

        撤销后订单不再挂在交易所上，但 size_matched 保留（已成交部分仍计入 filled）。

        Args:
            all_results: 全 session 的追踪结果
            cancelled_results: 已撤销的追踪结果
        """
        if not confirmed_cancel_ids:
            return
        cancelled_ids = {
            id(r) for r in cancelled_results
            if r.venue_order_id in confirmed_cancel_ids
        }
        for result in all_results:
            if id(result) in cancelled_ids:
                result.size_remaining = 0.0

    # =========================================================================
    # 成交刷新
    # =========================================================================

    async def _refresh_tracking_results(
        self,
        all_results: list[TrackingResult],
        session_start_ms: int,
    ) -> None:
        """
        重新查询所有历史操作的最新成交状态

        在 recovery 规划前调用，确保 session.filled 基于最新数据。
        通过 tracker 的 API 查询更新 all_results 中每个 result 的 size_matched。
        对于 OrbitExch，使用 session_start_ms 过滤本 session 的订单。

        Args:
            all_results: 全 session 的追踪结果列表（就地更新）
            session_start_ms: session 开始时间戳（毫秒）
        """
        for result in all_results:
            if result.status == TrackingStatus.FAILED:
                continue  # 失败的操作不需要重新查询

            operation = result.operation
            try:
                if operation.venue == OperationVenue.POLYMARKET:
                    if self._tracker._polymarket_client and operation.condition_id:
                        orders = self._tracker._polymarket_client.get_current_orders(
                            condition_id=operation.condition_id
                        )
                        # 时间戳过滤 + token_id 匹配方向
                        for order in orders:
                            if order.timestamp < session_start_ms:
                                continue
                            if order.asset_id == operation.token_id:
                                new_matched = order.size_matched
                                if new_matched > result.size_matched:
                                    self._log.info(
                                        f"Refresh: {operation.market_type} polymarket "
                                        f"size_matched {result.size_matched} -> {new_matched}"
                                    )
                                    result.size_matched = new_matched
                                    result.size_remaining = order.original_size - order.size_matched
                                    result.status = TrackingStatus.CONFIRMED
                                break

                elif operation.venue == OperationVenue.ORBITEXCH:
                    if self._tracker._orbitexch_client and operation.market_id:
                        bets = await self._tracker._orbitexch_client.get_current_bets(
                            operation.market_id
                        )
                        # 时间戳过滤 + selectionId + side 匹配方向
                        for bet in bets:
                            placed_date = int(bet.get("placedDate", 0))
                            if placed_date < session_start_ms:
                                continue
                            if (str(bet.get("selectionId")) == operation.selection_id
                                    and bet.get("side", "").upper() == "BACK"):
                                new_matched = float(bet.get("sizeMatched", 0))
                                if new_matched > result.size_matched:
                                    self._log.info(
                                        f"Refresh: {operation.market_type} orbitexch "
                                        f"size_matched {result.size_matched} -> {new_matched}"
                                    )
                                    result.size_matched = new_matched
                                    result.size_remaining = float(
                                        bet.get("sizeRemaining", 0)
                                    )
                                    result.status = TrackingStatus.CONFIRMED
                                break

            except Exception as e:
                self._log.warning(
                    f"Refresh failed for {operation.venue.value}/{operation.market_type}: {e}"
                )

    # =========================================================================
    # 回调
    # =========================================================================

    def register_session_callback(
        self,
        callback: Callable[[ExecutionSession], None],
    ) -> None:
        """注册会话更新回调"""
        self._session_callbacks.append(callback)

    def _notify_session_update(self, session: ExecutionSession) -> None:
        """通知会话更新"""
        for callback in self._session_callbacks:
            try:
                callback(session)
            except Exception as e:
                self._log.error(f"Session callback error: {e}")

    # =========================================================================
    # WebSocket 事件转发
    # =========================================================================

    def on_polymarket_event(self, event_type: str, data: dict) -> None:
        """转发 Polymarket WebSocket 事件到追踪器"""
        self._tracker.on_polymarket_event(event_type, data)

    def on_orbitexch_event(self, event_type: str, data: dict) -> None:
        """转发 OrbitExch WebSocket 事件到追踪器"""
        self._tracker.on_orbitexch_event(event_type, data)

    # =========================================================================
    # 查询
    # =========================================================================

    def get_session(self, session_id: str) -> ExecutionSession | None:
        """获取会话"""
        return self._sessions.get(session_id)

    def get_active_sessions(self) -> list[ExecutionSession]:
        """获取活跃会话"""
        return [
            s for s in self._sessions.values()
            if s.phase not in (SessionPhase.COMPLETED, SessionPhase.FAILED)
        ]

    def get_sessions_summary(self) -> dict[str, Any]:
        """获取会话统计"""
        total = len(self._sessions)
        by_phase = {}
        by_end_reason = {}

        for session in self._sessions.values():
            phase = session.phase.value
            by_phase[phase] = by_phase.get(phase, 0) + 1

            if session.end_reason:
                reason = session.end_reason.value
                by_end_reason[reason] = by_end_reason.get(reason, 0) + 1

        return {
            "total_sessions": total,
            "active_sessions": len(self.get_active_sessions()),
            "by_phase": by_phase,
            "by_end_reason": by_end_reason,
        }

    def get_all_sessions(self) -> list[ExecutionSession]:
        """获取全部会话"""
        return list(self._sessions.values())
