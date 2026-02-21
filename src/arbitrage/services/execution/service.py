"""
订单执行服务

协调 Polymarket 和 OrbitExch 执行器，提供统一的订单执行接口。

Debug 模式集成:
- 支持价格覆盖 (polymarket_price, orbitexch_price)
- 支持订单大小覆盖 (order_size)
- 支持跳过执行 (skip_execution)
- 支持执行延迟 (execution_delay)
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
from .orchestrator import ExecutionOrchestrator
from .session import ExecutionSession
from .mock_exchange import MockExchange

# Debug 管理器 (延迟导入避免循环依赖)
def _get_debug_manager():
    try:
        from src.arbitrage.services.debug import debug_manager
        return debug_manager
    except ImportError:
        return None


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

        # 模拟交易所
        self._mock_exchange = MockExchange(
            order_update_callback=self._on_mock_order_update,
            logger=logging.getLogger("MockExchange"),
        )

        # 外部服务引用
        self._odds_service = None  # OddsSubscriptionService
        self._arbitrage_config = None  # ArbitrageConfig
        self._risk_service = None  # RiskService

        # 执行编排器（用于会话式执行）
        self._orchestrator: ExecutionOrchestrator | None = None

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
        self._log.info(f"OrbitExch executor ready (current pages: {len(self._orbitexch_executor._pages)})")

        # 初始化执行编排器
        self._orchestrator = ExecutionOrchestrator(
            config=self.config,
            order_executor=self.execute_order,
            order_canceller=self.cancel_order,
            order_info_getter=self._get_order_info_wrapper,
            probabilities_getter=self._get_probabilities_wrapper,
            orbitexch_modify_and_take=self._orbitexch_modify_and_take_wrapper,
            logger=logging.getLogger("ExecutionOrchestrator"),
        )
        self._log.info("Execution orchestrator initialized")

        # 注册会话完成回调：触发风控返水率重新计算
        self._orchestrator.register_session_callback(self._on_session_complete)

        self._initialized = True
        self._log.info("Execution service initialized")

        return True

    def update_config(self, config: ExecutionConfig) -> None:
        """更新执行服务配置"""
        self.config = config
        if self._orchestrator:
            self._orchestrator.update_config(config)

    def set_orbitexch_page(self, competition_id: str, page: Page) -> None:
        """
        设置 OrbitExch 页面引用

        由 OddsSubscriptionService 调用，共享浏览器页面。

        Args:
            competition_id: 联赛 ID
            page: Playwright 页面
        """
        self._orbitexch_executor.set_page(competition_id, page)
        self._log.info(
            f"OrbitExch page set: competition={competition_id}, "
            f"total_pages={len(self._orbitexch_executor._pages)}"
        )

    # =========================================================================
    # 订单执行
    # =========================================================================

    async def execute_order(self, order: Order) -> ExecutionResult:
        """
        执行订单

        根据 venue 分发到对应的执行器。
        如果 debug 模式启用，会应用相应的覆盖。

        注意：pair 级别的去重检查在 on_opportunity 中完成。

        Args:
            order: 订单

        Returns:
            执行结果
        """
        if not self._initialized:
            self._log.error(f"Order {order.order_id} failed: Service not initialized")
            return ExecutionResult(
                success=False,
                order=order,
                message="Service not initialized",
            )

        # 应用 debug 覆盖
        order = self._apply_debug_overrides(order)
        # 应用市价开关（全局）
        order = self._apply_market_overrides(order)

        # 模拟交易所模式
        if self._use_mock_exchange():
            # 保存订单
            self._orders[order.order_id] = order
            return await self._mock_exchange.place_order(order)

        # 保存订单
        self._orders[order.order_id] = order

        # 检查是否跳过执行
        debug_mgr = _get_debug_manager()
        if debug_mgr and debug_mgr.is_override_active("skip_execution"):
            self._log.warning(
                f"[DEBUG] Skipping execution for order {order.order_id} "
                f"(venue={order.venue.value}, price={order.price}, size={order.size})"
            )
            order.status = OrderStatus.SUBMITTED
            order.submitted_at = time.time()
            order.metadata["debug_skipped"] = True

            return ExecutionResult(
                success=True,
                order=order,
                message="[DEBUG] Execution skipped",
            )

        # 执行延迟
        if debug_mgr and debug_mgr.is_override_active("execution_delay"):
            delay = debug_mgr.get_override("execution_delay", 0)
            if delay > 0:
                self._log.info(f"[DEBUG] Execution delay: {delay}s")
                await asyncio.sleep(delay)

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

        # 记录执行结果
        if result.success:
            self._log.info(f"Order {order.order_id} executed successfully")

            # 记录成交到风控服务（仅记录，不触发返水率计算）
            if self._risk_service and order.filled_size > 0:
                self._notify_risk_service(order)
        else:
            self._log.error(f"Order {order.order_id} failed: {result.message}")

        # 更新订单状态
        if result.success and order.is_active:
            self._active_orders[order.order_id] = order

        # 触发回调
        self._notify_order_update(order)

        return result

    def _apply_market_overrides(self, order: Order) -> Order:
        """应用市价单覆盖配置"""
        if not self.config.market_order_enabled:
            return order

        if order.venue == Venue.POLYMARKET:
            order.order_type = OrderType.FOK
            order.metadata["market_order"] = True
        elif order.venue == Venue.ORBITEXCH:
            order.order_type = OrderType.FOK
            if order.side in (OrderSide.BUY, OrderSide.BACK):
                order.price = 1.01
            else:
                order.price = 1000.0

        return order

    def _notify_risk_service(self, order: Order) -> None:
        """
        通知风控服务订单成交

        Args:
            order: 成交的订单
        """
        if not self._risk_service:
            return

        # 获取比赛信息
        context = None
        if self._odds_service:
            # 从 odds_service 获取 pair 信息
            pair_info = self._odds_service.get_pair_info(order.pair_id)
            if pair_info:
                competition = pair_info.get("competition", "")
                home_team = pair_info.get("polymarket_home", pair_info.get("home_team", ""))
                away_team = pair_info.get("polymarket_away", pair_info.get("away_team", ""))
            else:
                competition = ""
                home_team = ""
                away_team = ""
        else:
            competition = ""
            home_team = ""
            away_team = ""

        self._risk_service.add_fill(
            pair_id=order.pair_id,
            venue=order.venue.value,
            market_type=order.market_type,
            size=order.filled_size if order.filled_size > 0 else order.size,
            price=order.price,
            order_id=order.order_id,
            competition=competition,
            home_team=home_team,
            away_team=away_team,
        )

        self._log.debug(
            f"Recorded fill to risk service: {order.pair_id}/{order.market_type}, "
            f"size={order.filled_size or order.size}, price={order.price}"
        )

    def _on_session_complete(self, session) -> None:
        """
        会话完成回调

        由 ExecutionOrchestrator 在会话结束时调用。
        触发风控服务重新计算持仓返水率。

        Args:
            session: ExecutionSession
        """
        if self._risk_service:
            self._risk_service.on_execution_complete(session.pair_id)

    def _has_incomplete_orders(self, pair_id: str) -> bool:
        """
        检查指定 pair 是否有未完成的订单

        Args:
            pair_id: 比赛 ID

        Returns:
            是否有未完成订单
        """
        for existing in self._orders.values():
            if existing.pair_id == pair_id and not existing.is_done:
                return True
        return False

    def _calculate_min_other_rebate(
        self,
        rebate_market: str,
        way_rebate: dict,
        legs: list[dict],
    ) -> float | None:
        """
        计算其他方向持仓返水率的最小值

        Args:
            rebate_market: 返水方向 (home/draw/away)
            way_rebate: 各方向持仓返水率 {outcome: rebate_rate}
            legs: 套利腿列表

        Returns:
            最小返水率，如果没有则返回 None
        """
        # 收集所有涉及的市场类型
        all_markets = set()
        for leg in legs:
            all_markets.add(leg.get("market_type", ""))

        # 收集其他方向的持仓返水率
        other_rebates = []
        for outcome, holding_rebate in way_rebate.items():
            # 跳过返水方向
            if outcome == rebate_market:
                continue
            # 只计算涉及的方向，且返水率 > 0
            if outcome in all_markets and holding_rebate > 0:
                other_rebates.append(holding_rebate)

        return min(other_rebates) if other_rebates else None

    def _calculate_final_size(
        self,
        base_size: float,
        venue: Venue,
        rebate_rate: float,
        rebate_prob: float,
        discount: float,
        take_off: float,
        share: float,
        min_other_rebate: float | None,
        is_rebate_leg: bool,
    ) -> float:
        """
        计算订单的最终 size

        公式：
        - OrbitExch: size + discount * size * rebate_rate + take_off * share * min_other_rebate
        - Polymarket: size + discount * size * rebate_rate + take_off * share * min_other_rebate / rebate_prob

        注意：只对返水方向的腿应用额外计算，非返水腿直接返回 base_size

        Args:
            base_size: 基础 size
            venue: 平台
            rebate_rate: 当前机会的返水率
            rebate_prob: 返水方向的概率 (0-1)
            discount: 折扣系数
            take_off: 从其他方向持仓返水中拿走的比例
            share: 基准金额
            min_other_rebate: 其他方向持仓返水率的最小值
            is_rebate_leg: 是否为返水方向的腿

        Returns:
            最终 size
        """
        # 非返水腿，直接返回基础 size
        if not is_rebate_leg:
            return base_size

        # 计算 discount 贡献: discount * size * rebate_rate
        discount_contribution = discount * base_size * rebate_rate

        # 计算 take_off 贡献
        take_off_contribution = 0.0
        if take_off > 0 and min_other_rebate is not None and min_other_rebate > 0:
            take_off_base = take_off * share * min_other_rebate
            if venue == Venue.POLYMARKET and rebate_prob > 0:
                take_off_contribution = take_off_base / rebate_prob
            else:
                take_off_contribution = take_off_base

        final_size = base_size + discount_contribution + take_off_contribution

        self._log.info(
            f"Final size for {venue.value}: {base_size:.2f} + "
            f"{discount} * {base_size:.2f} * {rebate_rate:.4f} + "
            f"take_off_contrib={take_off_contribution:.2f} = {final_size:.2f}"
        )

        return final_size

    def _apply_debug_overrides(self, order: Order) -> Order:
        """
        应用 debug 覆盖到订单

        Args:
            order: 原始订单

        Returns:
            应用覆盖后的订单 (可能是修改后的副本)
        """
        debug_mgr = _get_debug_manager()
        if not debug_mgr or not debug_mgr.enabled:
            return order

        modified = False
        original_values = {}

        # 价格覆盖
        if order.venue == Venue.POLYMARKET and debug_mgr.is_override_active("polymarket_price"):
            original_values["price"] = order.price
            order.price = debug_mgr.get_override("polymarket_price", order.price)
            modified = True
            self._log.warning(
                f"[DEBUG] Polymarket price override: {original_values['price']} -> {order.price}"
            )

        elif order.venue == Venue.ORBITEXCH and debug_mgr.is_override_active("orbitexch_price"):
            original_values["price"] = order.price
            order.price = debug_mgr.get_override("orbitexch_price", order.price)
            modified = True
            self._log.warning(
                f"[DEBUG] OrbitExch price override: {original_values['price']} -> {order.price}"
            )

        # 订单大小覆盖（分平台）
        if order.venue == Venue.POLYMARKET and debug_mgr.is_override_active("polymarket_size"):
            original_values["size"] = order.size
            order.size = debug_mgr.get_override("polymarket_size", order.size)
            modified = True
            self._log.warning(
                f"[DEBUG] Polymarket size override: {original_values['size']} -> {order.size}"
            )
        elif order.venue == Venue.ORBITEXCH and debug_mgr.is_override_active("orbitexch_size"):
            original_values["size"] = order.size
            order.size = debug_mgr.get_override("orbitexch_size", order.size)
            modified = True
            self._log.warning(
                f"[DEBUG] OrbitExch size override: {original_values['size']} -> {order.size}"
            )
        elif debug_mgr.is_override_active("order_size"):
            # 通用 size 覆盖（兼容旧配置）
            original_values["size"] = order.size
            order.size = debug_mgr.get_override("order_size", order.size)
            modified = True
            self._log.warning(
                f"[DEBUG] Order size override: {original_values['size']} -> {order.size}"
            )

        # 记录原始值到元数据
        if modified:
            order.metadata["debug_overrides"] = original_values
            order.metadata["debug_mode"] = True

        return order

    def _use_mock_exchange(self) -> bool:
        """判断是否启用模拟交易所"""
        debug_mgr = _get_debug_manager()
        return bool(
            debug_mgr
            and debug_mgr.enabled
            and debug_mgr.is_override_active("use_mock_exchange")
        )

    def _on_mock_order_update(self, order: Order) -> None:
        """处理模拟交易所的订单状态更新"""
        if order.is_active:
            self._active_orders[order.order_id] = order
        elif order.order_id in self._active_orders:
            del self._active_orders[order.order_id]

        if order.filled_size > 0:
            self._notify_risk_service(order)

        self._notify_order_update(order)

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

        if self._use_mock_exchange():
            return await self._mock_exchange.cancel_order(order)

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

        if self._use_mock_exchange():
            return await self._mock_exchange.take_remaining_at_market(order)

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

        if self._use_mock_exchange():
            orbit_orders = [
                order for order in self._active_orders.values()
                if order.venue == Venue.ORBITEXCH
            ]
            return await self._mock_exchange.cancel_all_unmatched(orbit_orders)

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

    # =========================================================================
    # 策略回调
    # =========================================================================

    def set_odds_service(self, odds_service) -> None:
        """
        设置赔率服务引用

        用于在创建订单时获取 token_id、selection_id 等信息。

        Args:
            odds_service: OddsSubscriptionService 实例
        """
        self._odds_service = odds_service
        self._log.info("Odds service reference set")

    def set_arbitrage_config(self, arbitrage_config) -> None:
        """
        设置套利配置引用

        用于计算订单大小。

        Args:
            arbitrage_config: ArbitrageConfig 实例
        """
        self._arbitrage_config = arbitrage_config
        self._log.info("Arbitrage config set")

    def set_risk_service(self, risk_service) -> None:
        """
        设置风控服务引用

        用于在订单成交时通知风控服务更新持仓。

        Args:
            risk_service: RiskService 实例
        """
        self._risk_service = risk_service
        self._log.info("Risk service reference set")

    async def on_opportunity(self, opportunity: dict) -> None:
        """
        处理策略服务发送的套利机会

        从机会信息创建订单并执行。
        支持 2-way（2 legs）和 3-way（3 legs）套利。

        Args:
            opportunity: 机会数据，包含 best_direction 等
        """
        opportunity_id = opportunity.get("opportunity_id", "unknown")
        pair_id = opportunity.get("pair_id", "")
        best_direction = opportunity.get("best_direction")

        if not best_direction:
            self._log.debug(f"Opportunity {opportunity_id}: no best_direction")
            return

        # 检查是否有活跃的执行会话（每个 pair 同时只能有一个）
        if self._orchestrator and self._orchestrator.has_active_session(pair_id):
            active_session = self._orchestrator.get_active_session_for_pair(pair_id)
            self._log.warning(
                f"Opportunity {opportunity_id}: pair {pair_id} has active session "
                f"{active_session.session_id if active_session else 'unknown'}, skipping"
            )
            return

        self._log.info(
            f"Processing opportunity {opportunity_id}: "
            f"pair={pair_id}, rebate_rate={best_direction.get('rebate_rate')}"
        )

        # 获取 size 计算参数
        discount = self.config.discount
        take_off = self.config.take_off
        way_rebate = opportunity.get("way_rebate", {})

        await self._process_opportunity(
            opportunity_id=opportunity_id,
            pair_id=pair_id,
            best_direction=best_direction,
            discount=discount,
            take_off=take_off,
            way_rebate=way_rebate,
        )

    async def _process_opportunity(
        self,
        opportunity_id: str,
        pair_id: str,
        best_direction: dict,
        discount: float = 1.0,
        take_off: float = 0.0,
        way_rebate: dict = None,
    ) -> None:
        """
        处理 opportunity（内部方法）

        Args:
            opportunity_id: 机会 ID
            pair_id: 比赛 ID
            best_direction: 最佳套利方向
            discount: 折扣系数
            take_off: 从其他方向持仓返水中拿走的比例
            way_rebate: 各方向持仓返水率 {outcome: rebate_rate}
        """
        way_rebate = way_rebate or {}

        # 从 best_direction 提取所有套利腿
        legs = best_direction.get("legs", [])
        if len(legs) < 2:
            self._log.warning(f"Opportunity {opportunity_id}: insufficient legs ({len(legs)})")
            return

        # 分离 Polymarket 和 OrbitExch 腿（可能有多个）
        poly_legs = [leg for leg in legs if leg.get("venue") == "polymarket"]
        orbit_legs = [leg for leg in legs if leg.get("venue") == "orbitexch"]

        self._log.info(
            f"Opportunity {opportunity_id}: {len(legs)} legs "
            f"({len(poly_legs)} poly + {len(orbit_legs)} orbit)"
        )

        # 获取返水方向信息
        rebate_rate = best_direction.get("rebate_rate", 0.0)
        rebate_market = best_direction.get("rebate_market", "")  # home/draw/away
        rebate_venue = best_direction.get("rebate_venue", "")    # polymarket/orbitexch

        # 获取 share（基准金额）
        share = self._arbitrage_config.share if self._arbitrage_config else 100.0

        # 计算其他方向持仓返水率的最小值
        min_other_rebate = self._calculate_min_other_rebate(
            rebate_market=rebate_market,
            way_rebate=way_rebate,
            legs=legs,
        )

        # 获取返水方向的概率（用于 Polymarket size 计算）
        rebate_prob = 0.0
        for leg in legs:
            if leg.get("market_type") == rebate_market:
                rebate_prob = leg.get("probability", 0) / 100  # 转为 0-1
                break

        # Size 计算参数
        size_params = {
            "rebate_rate": rebate_rate,
            "rebate_prob": rebate_prob,
            "discount": discount,
            "take_off": take_off,
            "share": share,
            "min_other_rebate": min_other_rebate,
        }

        # 为每条腿创建订单
        orders: list[Order] = []

        # 创建 Polymarket 订单（可能有 1-2 个）
        for leg in poly_legs:
            # 判断此腿是否为返水方向
            is_rebate_leg = (
                leg.get("market_type") == rebate_market and
                rebate_venue == "polymarket"
            )
            order = await self._create_order_from_leg(
                opportunity_id, pair_id, leg, Venue.POLYMARKET,
                is_rebate_leg=is_rebate_leg,
                size_params=size_params,
            )
            if order:
                orders.append(order)

        # 创建 OrbitExch 订单（可能有 1-2 个）
        for leg in orbit_legs:
            # 判断此腿是否为返水方向
            is_rebate_leg = (
                leg.get("market_type") == rebate_market and
                rebate_venue == "orbitexch"
            )
            order = await self._create_order_from_leg(
                opportunity_id, pair_id, leg, Venue.ORBITEXCH,
                is_rebate_leg=is_rebate_leg,
                size_params=size_params,
            )
            if order:
                orders.append(order)

        if not orders:
            self._log.warning(f"Opportunity {opportunity_id}: no orders created")
            return

        self._log.info(f"Opportunity {opportunity_id}: executing {len(orders)} orders")

        # 确保 OrbitExch 页面可用（可能在订阅过程中还未设置）
        orbit_orders = [o for o in orders if o.venue == Venue.ORBITEXCH]
        if orbit_orders and not self._orbitexch_executor._pages:
            # 尝试从 odds_service 获取页面
            if self._odds_service:
                orbitexch_pages = self._odds_service.get_orbitexch_pages()
                for comp_id, page in orbitexch_pages.items():
                    if comp_id != "main":
                        self._orbitexch_executor.set_page(comp_id, page)
                        self._log.info(f"Dynamically set OrbitExch page: {comp_id}")

            if not self._orbitexch_executor._pages:
                self._log.warning(
                    f"Opportunity {opportunity_id}: OrbitExch pages not yet available, skipping"
                )
                return

        # 并行执行所有订单
        results = await asyncio.gather(
            *[self.execute_order(order) for order in orders],
            return_exceptions=True,
        )

        # 记录结果（包括详细错误信息）
        success_count = 0
        has_fills = False
        for i, r in enumerate(results):
            order = orders[i]
            if isinstance(r, Exception):
                self._log.error(
                    f"Order {order.order_id} ({order.venue.value}/{order.market_type}) "
                    f"raised exception: {r}"
                )
            elif r.success:
                success_count += 1
                if order.filled_size > 0:
                    has_fills = True
            else:
                self._log.error(
                    f"Order {order.order_id} ({order.venue.value}/{order.market_type}) "
                    f"failed: {r.message}"
                )

        self._log.info(
            f"Opportunity {opportunity_id} execution: "
            f"{success_count}/{len(orders)} orders successful"
        )

        # 所有订单执行完毕后，触发返水率重新计算
        # （各订单成交已在 execute_order 中通过 add_fill 记录）
        if self._risk_service and has_fills:
            self._risk_service.on_execution_complete(pair_id)

    async def _create_order_from_leg(
        self,
        opportunity_id: str,
        pair_id: str,
        leg: dict,
        venue: Venue,
        is_rebate_leg: bool = False,
        size_params: dict = None,
    ) -> Order | None:
        """
        从套利腿创建订单

        Args:
            opportunity_id: 机会 ID
            pair_id: 比赛 ID
            leg: 套利腿数据
            venue: 平台
            is_rebate_leg: 是否为返水方向的腿
            size_params: size 计算参数 {rebate_rate, rebate_prob, discount, take_off, share, min_other_rebate}

        Returns:
            订单对象，如果创建失败返回 None
        """
        size_params = size_params or {}
        market_type = leg.get("market_type", "")
        probability = leg.get("probability", 0)  # 0-100 scale
        raw_odds = leg.get("raw_odds", 0)
        action = leg.get("action", "buy")

        # 获取订单信息
        order_info = None
        if self._odds_service:
            order_info = self._odds_service.get_order_info(pair_id, market_type)

        if not order_info:
            self._log.warning(
                f"Opportunity {opportunity_id}: cannot get order info "
                f"for {pair_id}/{market_type}/{venue.value}"
            )
            return None

        # 记录订单信息以便调试
        self._log.info(
            f"Order info for {pair_id}/{market_type}/{venue.value}: {order_info}"
        )

        # 计算订单大小
        if venue == Venue.POLYMARKET:
            base_size = 10.0
            if self._arbitrage_config:
                base_size = self._arbitrage_config.calculate_polymarket_size(probability)

            # 计算最终 size
            size = self._calculate_final_size(
                base_size=base_size,
                venue=venue,
                rebate_rate=size_params.get("rebate_rate", 0.0),
                rebate_prob=size_params.get("rebate_prob", 0.0),
                discount=size_params.get("discount", 1.0),
                take_off=size_params.get("take_off", 0.0),
                share=size_params.get("share", 100.0),
                min_other_rebate=size_params.get("min_other_rebate"),
                is_rebate_leg=is_rebate_leg,
            )

            return Order(
                venue=venue,
                pair_id=pair_id,
                market_type=market_type,
                token_id=order_info.get("polymarket", {}).get("token_id", ""),
                condition_id=order_info.get("polymarket", {}).get("condition_id", ""),
                side=OrderSide.BUY if action == "buy" else OrderSide.SELL,
                price=probability / 100,  # 转换为 0-1
                size=size,
                order_type=OrderType.GTC,
                metadata={
                    "opportunity_id": opportunity_id,
                    "leg_action": action,
                    "original_probability": probability,
                    "base_size": base_size,
                    "is_rebate_leg": is_rebate_leg,
                },
            )

        elif venue == Venue.ORBITEXCH:
            base_size = 10.0
            if self._arbitrage_config:
                base_size = self._arbitrage_config.calculate_orbitexch_size(raw_odds)

            # 计算最终 size
            size = self._calculate_final_size(
                base_size=base_size,
                venue=venue,
                rebate_rate=size_params.get("rebate_rate", 0.0),
                rebate_prob=size_params.get("rebate_prob", 0.0),
                discount=size_params.get("discount", 1.0),
                take_off=size_params.get("take_off", 0.0),
                share=size_params.get("share", 100.0),
                min_other_rebate=size_params.get("min_other_rebate"),
                is_rebate_leg=is_rebate_leg,
            )

            return Order(
                venue=venue,
                pair_id=pair_id,
                market_type=market_type,
                market_id=order_info.get("orbitexch", {}).get("market_id", ""),
                selection_id=order_info.get("orbitexch", {}).get("selection_id", ""),
                side=OrderSide.BACK if action == "buy" else OrderSide.LAY,
                price=raw_odds,
                size=size,
                order_type=OrderType.GTC,
                metadata={
                    "opportunity_id": opportunity_id,
                    "leg_action": action,
                    "original_odds": raw_odds,
                    "base_size": base_size,
                    "is_rebate_leg": is_rebate_leg,
                },
            )

        return None

    # =========================================================================
    # 编排器支持方法
    # =========================================================================

    def _get_order_info_wrapper(self, pair_id: str, market_type: str) -> dict | None:
        """
        获取订单信息（编排器回调）

        Args:
            pair_id: 比赛 ID
            market_type: 市场类型

        Returns:
            订单信息
        """
        if self._odds_service:
            return self._odds_service.get_order_info(pair_id, market_type)
        return None

    async def _orbitexch_modify_and_take_wrapper(
        self, order: Order, new_size: float
    ) -> ExecutionResult:
        """
        OrbitExch 修改 size 并按市价执行（编排器回调）

        执行后通知 risk service 记录成交（与 execute_order 路径一致）。

        Args:
            order: 订单
            new_size: 新的 size

        Returns:
            执行结果
        """
        result = await self._orbitexch_executor.modify_size_and_take(order, new_size)

        # 记录成交到风控服务
        if result.success and self._risk_service and order.filled_size > 0:
            self._notify_risk_service(order)

        return result

    def _get_probabilities_wrapper(self, pair_id: str) -> dict[str, float] | None:
        """
        获取实时概率（编排器回调）

        Args:
            pair_id: 比赛 ID

        Returns:
            概率字典 {home, draw, away}
        """
        if self._odds_service:
            # 从 odds_service 获取实时赔率并转换为概率
            odds_data = self._odds_service.get_pair_odds(pair_id)
            if odds_data:
                probs = {}
                for outcome in ["home", "draw", "away"]:
                    # 尝试从 polymarket 或 orbitexch 获取概率
                    poly_data = odds_data.get("polymarket", {}).get(outcome, {})
                    orbit_data = odds_data.get("orbitexch", {}).get(outcome, {})

                    # 优先使用 polymarket 的 ask 价格作为概率
                    if poly_data and poly_data.get("ask"):
                        probs[outcome] = poly_data["ask"]
                    elif orbit_data and orbit_data.get("back_odds"):
                        # OrbitExch odds 转换为概率
                        probs[outcome] = 1.0 / orbit_data["back_odds"]
                    else:
                        probs[outcome] = 0.0
                return probs
        return None

    # =========================================================================
    # 会话式执行
    # =========================================================================

    async def execute_with_session(
        self,
        pair_id: str,
        opportunity_id: str,
        legs: list[dict],
        target_shares: dict[str, float],
        probabilities: dict[str, float],
    ) -> ExecutionSession:
        """
        使用会话式执行套利机会

        包含完整的执行流程：规划 -> 执行 -> 追踪 -> 补救循环

        Args:
            pair_id: 比赛 ID
            opportunity_id: 机会 ID
            legs: 套利腿列表
            target_shares: 目标 share {home, draw, away}
            probabilities: 初始概率 {home, draw, away}

        Returns:
            执行会话（包含最终状态）
        """
        if not self._orchestrator:
            raise RuntimeError("Orchestrator not initialized")

        return await self._orchestrator.execute_opportunity(
            pair_id=pair_id,
            opportunity_id=opportunity_id,
            legs=legs,
            target_shares=target_shares,
            probabilities=probabilities,
        )

    def get_session(self, session_id: str) -> ExecutionSession | None:
        """获取执行会话"""
        if self._orchestrator:
            return self._orchestrator.get_session(session_id)
        return None

    def get_active_sessions(self) -> list[ExecutionSession]:
        """获取活跃执行会话"""
        if self._orchestrator:
            return self._orchestrator.get_active_sessions()
        return []

    def get_sessions_summary(self) -> dict:
        """获取会话统计"""
        if self._orchestrator:
            return self._orchestrator.get_sessions_summary()
        return {"total_sessions": 0, "active_sessions": 0}

    # =========================================================================
    # WebSocket 事件转发
    # =========================================================================

    def on_polymarket_ws_event(self, event_type: str, data: dict) -> None:
        """
        处理 Polymarket WebSocket 事件

        转发给编排器用于订单追踪。

        Args:
            event_type: 事件类型 (PLACEMENT, UPDATE, CANCELLATION)
            data: 事件数据
        """
        if self._orchestrator:
            self._orchestrator.on_polymarket_event(event_type, data)

    def on_orbitexch_ws_event(self, event_type: str, data: dict) -> None:
        """
        处理 OrbitExch WebSocket 事件

        转发给编排器用于订单追踪。

        Args:
            event_type: 事件类型
            data: 事件数据
        """
        if self._orchestrator:
            self._orchestrator.on_orbitexch_event(event_type, data)
