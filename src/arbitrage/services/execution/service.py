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

from src.arbitrage.services.strategy.messages import OpportunityMessage
from src.arbitrage.services.strategy.topics import OPPORTUNITY_TOPIC_PATTERN
from src.arbitrage.services.odds_subscription.messages import PairActivityMessage
from src.arbitrage.services.odds_subscription.topics import pair_activity_topic
from src.arbitrage.services.execution.messages import SessionCompleteMessage
from src.arbitrage.services.execution.topics import session_complete_topic

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

        # 消息总线
        self._msgbus = None

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
        self._sync_tracking_clients()

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
            return await self._mock_exchange.place_order(order)

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
                self._log.debug(f"Execution delay: {delay}s")
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
        else:
            self._log.error(f"Order {order.order_id} failed: {result.message}")

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

    def _on_session_complete(self, session) -> None:
        """
        会话完成回调

        由 ExecutionOrchestrator 在会话结束时调用。
        通过消息总线发布 SessionCompleteMessage。

        Args:
            session: ExecutionSession
        """
        self._publish_session_complete(session)

    def _publish_session_complete(self, session) -> None:
        """通过消息总线发布会话完成消息"""
        if not self._msgbus:
            self._log.warning("Cannot publish session_complete: msgbus not set")
            return

        msg = SessionCompleteMessage(
            session_id=session.session_id,
            pair_id=session.pair_id,
            opportunity_id=getattr(session, "opportunity_id", ""),
            phase=getattr(session, "phase", ""),
            end_reason=getattr(session, "end_reason", ""),
        )
        topic = session_complete_topic(session.pair_id)
        self._msgbus.publish(topic, msg)
        self._log.info(f"Published session_complete to {topic}")

    def _publish_pair_activity(self, pair_id: str, is_active: bool, source: str) -> None:
        """发布 pair 活跃状态消息"""
        if not self._msgbus:
            return
        msg = PairActivityMessage(
            pair_id=pair_id,
            is_active=is_active,
            source=source,
        )
        topic = pair_activity_topic(pair_id)
        self._msgbus.publish(topic, msg)


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
        self._notify_order_update(order)

    async def cancel_order(self, order_id: str, venue: Venue | None = None) -> CancelResult:
        """
        撤销订单

        Args:
            order_id: 平台订单 ID
            venue: 交易平台（未指定时从 _orders 查找）

        Returns:
            撤单结果
        """
        # 如果未指定 venue，从活跃订单中查找
        if venue is None:
            for o in self.get_active_orders():
                if o["order_id"] == order_id:
                    venue = Venue(o["venue"])
                    break

        if venue is None:
            return CancelResult(
                success=False,
                order_id=order_id,
                message="Cannot determine venue for order",
            )

        if self._use_mock_exchange():
            cancel_order_obj = Order(order_id=order_id, venue=venue, venue_order_id=order_id)
            return await self._mock_exchange.cancel_order(cancel_order_obj)

        # 构建最小 Order 对象用于 executor
        cancel_order = Order(
            order_id=order_id,
            venue=venue,
            venue_order_id=order_id,
        )

        # 分发到对应执行器
        if venue == Venue.POLYMARKET:
            result = await self._polymarket_executor.cancel_order(cancel_order)
        elif venue == Venue.ORBITEXCH:
            result = await self._orbitexch_executor.cancel_order(cancel_order)
        else:
            return CancelResult(
                success=False,
                order_id=order_id,
                message=f"Unknown venue: {venue}",
            )

        return result

    async def take_remaining_at_market(self, order_id: str, venue: Venue | None = None) -> ExecutionResult:
        """
        将未成交部分按市价立即执行

        Args:
            order_id: 平台订单 ID
            venue: 交易平台

        Returns:
            执行结果
        """
        # 从活跃订单中查找
        order_data = None
        for o in self.get_active_orders(venue=venue):
            if o["order_id"] == order_id:
                order_data = o
                break

        if not order_data:
            return ExecutionResult(
                success=False,
                order=Order(order_id=order_id),
                message="Active order not found",
            )

        venue = Venue(order_data["venue"])
        remaining = order_data.get("size_remaining", 0)

        if remaining <= 0:
            return ExecutionResult(
                success=True,
                order=Order(order_id=order_id, venue=venue),
                message="No remaining size",
            )

        # 构建 Order 对象
        order = Order(
            order_id=order_id,
            venue=venue,
            venue_order_id=order_id,
            token_id=order_data.get("asset_id", order_data.get("token_id", "")),
            condition_id=order_data.get("condition_id", ""),
            market_id=order_data.get("market_id", ""),
            selection_id=order_data.get("selection_id", ""),
            side=OrderSide(order_data.get("side", "BUY")),
            price=order_data.get("price", 0),
            size=order_data.get("original_size", 0),
            filled_size=order_data.get("size_matched", 0),
        )

        if self._use_mock_exchange():
            return await self._mock_exchange.take_remaining_at_market(order)

        # 分发到对应执行器
        if venue == Venue.POLYMARKET:
            result = await self._polymarket_executor.take_remaining_at_market(order)
        elif venue == Venue.ORBITEXCH:
            result = await self._orbitexch_executor.take_remaining_at_market(order)
        else:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Unknown venue: {venue}",
            )

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
        active = self.get_active_orders(venue=venue)

        if not active:
            return []

        self._log.info(f"Cancelling {len(active)} orders")

        results = await asyncio.gather(
            *[self.cancel_order(o["order_id"], venue=Venue(o["venue"])) for o in active],
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
            orbit_orders = []
            for o in self.get_active_orders(venue=Venue.ORBITEXCH):
                orbit_orders.append(Order(
                    order_id=o["order_id"],
                    venue=Venue.ORBITEXCH,
                    venue_order_id=o["order_id"],
                ))
            return await self._mock_exchange.cancel_all_unmatched(orbit_orders)

        result = await self._orbitexch_executor.cancel_all_unmatched()

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

    def get_active_orders(self, venue: Venue | None = None) -> list[dict]:
        """
        获取活跃订单

        直接从 odds_subscription 内存缓存读取，返回统一格式的字典列表。

        Returns:
            [{"venue": str, "order_id": str, "price": float,
              "original_size": float, "size_matched": float, "size_remaining": float, ...}]
        """
        orders: list[dict] = []

        if not self._odds_service:
            return orders

        # Polymarket 活跃订单
        if venue is None or venue == Venue.POLYMARKET:
            for po in self._odds_service.get_polymarket_open_orders():
                orders.append({
                    "venue": "polymarket",
                    "order_id": po.order_id,
                    "asset_id": po.asset_id,
                    "condition_id": po.market,
                    "side": po.side,
                    "price": po.price,
                    "original_size": po.original_size,
                    "size_matched": po.size_matched,
                    "size_remaining": po.original_size - po.size_matched,
                })

        # OrbitExch 活跃订单
        if venue is None or venue == Venue.ORBITEXCH:
            for bet in self._odds_service.get_orbitexch_open_orders():
                orders.append({
                    "venue": "orbitexch",
                    "order_id": str(bet.get("offerId", "")),
                    "market_id": str(bet.get("marketId", "")),
                    "selection_id": str(bet.get("selectionId", "")),
                    "side": bet.get("side", ""),
                    "price": bet.get("price", 0),
                    "original_size": float(bet.get("sizePlaced", 0)),
                    "size_matched": float(bet.get("sizeMatched", 0)),
                    "size_remaining": float(bet.get("sizeRemaining", 0)),
                })

        return orders

    def get_orders_summary(self) -> dict[str, Any]:
        """获取订单统计"""
        active = self.get_active_orders()
        active_count = len(active)

        by_venue: dict[str, int] = {}
        for o in active:
            v = o["venue"]
            by_venue[v] = by_venue.get(v, 0) + 1

        return {
            "total_orders": active_count,
            "active_orders": active_count,
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
        self._sync_tracking_clients()

    def _sync_tracking_clients(self) -> None:
        """同步追踪所需的赔率客户端引用"""
        if not self._orchestrator or not self._odds_service:
            return
        try:
            self._orchestrator.set_polymarket_client(
                self._odds_service.get_polymarket_client()
            )
            self._orchestrator.set_orbitexch_client(
                self._odds_service.get_orbitexch_client()
            )
            self._odds_service.register_polymarket_order_callback(
                self.on_polymarket_ws_event
            )
            self._odds_service.register_orbitexch_bets_callback(
                self.on_orbitexch_ws_event
            )
        except Exception as e:
            self._log.warning(f"Failed to sync tracking clients: {e}")

    def set_arbitrage_config(self, arbitrage_config) -> None:
        """
        设置套利配置引用

        用于计算订单大小。

        Args:
            arbitrage_config: ArbitrageConfig 实例
        """
        self._arbitrage_config = arbitrage_config
        self._log.info("Arbitrage config set")

    def set_msgbus(self, msgbus) -> None:
        """设置消息总线并订阅主题"""
        if self._msgbus is msgbus:
            return
        self._msgbus = msgbus
        if not self._msgbus:
            return
        self._msgbus.subscribe(OPPORTUNITY_TOPIC_PATTERN, self._on_opportunity_message)
        self._log.info("Subscribed to opportunity messages")

    def _on_opportunity_message(self, msg: Any) -> None:
        """处理套利机会消息"""
        if isinstance(msg, OpportunityMessage):
            opportunity = {
                "opportunity_id": msg.opportunity_id,
                "pair_id": msg.pair_id,
                "competition": msg.competition,
                "home_team": msg.home_team,
                "away_team": msg.away_team,
                "is_live": msg.is_live,
                "detected_at": msg.detected_at,
                "triggered_strategies": msg.triggered_strategies,
                "rebate_value": msg.rebate_value,
                "way_rebate": msg.way_rebate,
                "best_direction": msg.best_direction,
                "all_directions": msg.all_directions,
                "signals": msg.signals,
                "adjusted_share": msg.adjusted_share,
                "status": msg.status,
            }
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.on_opportunity(opportunity))
            except RuntimeError:
                asyncio.create_task(self.on_opportunity(opportunity))

    async def on_opportunity(self, opportunity: dict) -> None:
        """
        处理策略服务发送的套利机会

        从机会信息创建订单并执行。
        支持 2-way（2 legs）和 3-way（3 legs）套利。

        所有验证在锁定前完成，使用 try-finally 确保解锁。

        Args:
            opportunity: 机会数据，包含 best_direction 等
        """
        opportunity_id = opportunity.get("opportunity_id", "unknown")
        pair_id = opportunity.get("pair_id", "")
        best_direction = opportunity.get("best_direction")

        # ==== 所有验证在锁定前完成 ====

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

        legs = best_direction.get("legs", [])
        if len(legs) < 2:
            self._log.warning(f"Opportunity {opportunity_id}: insufficient legs ({len(legs)})")
            return

        target_shares, probabilities = self._build_session_targets(
            best_direction=best_direction,
            legs=legs,
            way_rebate=opportunity.get("way_rebate", {}),
            adjusted_share=opportunity.get("adjusted_share"),
        )

        if not any(value > 0 for value in target_shares.values()):
            self._log.warning(f"Opportunity {opportunity_id}: invalid target shares {target_shares}")
            return

        # ==== 所有验证通过，锁定 pair 并执行 ====

        if pair_id:
            self._publish_pair_activity(pair_id, True, "execution")

        try:
            self._log.info(
                f"Processing opportunity {opportunity_id}: "
                f"pair={pair_id}, rebate_rate={best_direction.get('rebate_rate')}"
            )

            await self.execute_with_session(
                pair_id=pair_id,
                opportunity_id=opportunity_id,
                legs=legs,
                target_shares=target_shares,
                probabilities=probabilities,
            )

        except Exception as e:
            self._log.error(f"Opportunity {opportunity_id} execution error: {e}")

        finally:
            if pair_id:
                self._publish_pair_activity(pair_id, False, "execution")
                self._log.info(f"Pair {pair_id} unlocked (source=execution)")

    def _build_session_targets(
        self,
        best_direction: dict,
        legs: list[dict],
        way_rebate: dict | None = None,
        adjusted_share: float | None = None,
    ) -> tuple[dict[str, float], dict[str, float]]:
        """
        构建会话执行的目标 share 与初始概率

        返回:
            (target_shares, probabilities)
        """
        way_rebate = way_rebate or {}
        target_shares = {"home": 0.0, "draw": 0.0, "away": 0.0}
        probabilities = {"home": 0.0, "draw": 0.0, "away": 0.0}

        share = adjusted_share or (
            self._arbitrage_config.share if self._arbitrage_config else 100.0
        )

        rebate_rate = best_direction.get("rebate_rate", 0.0)
        rebate_market = best_direction.get("rebate_market", "")
        rebate_venue = best_direction.get("rebate_venue", "")
        min_other_rebate = self._calculate_min_other_rebate(
            rebate_market=rebate_market,
            way_rebate=way_rebate,
            legs=legs,
        )

        rebate_prob = 0.0
        for leg in legs:
            if leg.get("market_type") == rebate_market:
                rebate_prob = leg.get("probability", 0) / 100
                break

        for leg in legs:
            market_type = leg.get("market_type", "")
            if not market_type:
                continue

            prob_pct = leg.get("probability", 0)
            probabilities[market_type] = prob_pct / 100 if prob_pct else 0.0

            venue_str = leg.get("venue", "")
            raw_odds = leg.get("raw_odds", 0)
            is_rebate_leg = market_type == rebate_market and rebate_venue == venue_str

            if venue_str == "polymarket":
                base_size = share
                if self._arbitrage_config:
                    base_size = self._arbitrage_config.calculate_polymarket_size(prob_pct)
                final_size = self._calculate_final_size(
                    base_size=base_size,
                    venue=Venue.POLYMARKET,
                    rebate_rate=rebate_rate,
                    rebate_prob=rebate_prob,
                    discount=self.config.discount,
                    take_off=self.config.take_off,
                    share=share,
                    min_other_rebate=min_other_rebate,
                    is_rebate_leg=is_rebate_leg,
                )
                target_shares[market_type] = final_size
            elif venue_str == "orbitexch":
                if raw_odds <= 0:
                    continue
                base_size = share / raw_odds
                if self._arbitrage_config:
                    base_size = self._arbitrage_config.calculate_orbitexch_size(raw_odds)
                final_size = self._calculate_final_size(
                    base_size=base_size,
                    venue=Venue.ORBITEXCH,
                    rebate_rate=rebate_rate,
                    rebate_prob=rebate_prob,
                    discount=self.config.discount,
                    take_off=self.config.take_off,
                    share=share,
                    min_other_rebate=min_other_rebate,
                    is_rebate_leg=is_rebate_leg,
                )
                target_shares[market_type] = final_size * raw_odds

        return target_shares, probabilities


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

        Args:
            order: 订单
            new_size: 新的 size

        Returns:
            执行结果
        """
        return await self._orbitexch_executor.modify_size_and_take(order, new_size)

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
