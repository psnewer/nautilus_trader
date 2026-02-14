"""
OrbitExch 订单执行器

使用 Playwright 与网页交互执行订单。

实现逻辑:
1. 下单: 通过 HTTP POST 请求到 /customer/api/placeBets
2. 撤单: 点击网页上的 'Cancel Bet' 按钮
3. 市价成交: 点击 'Take @XX' 按钮
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

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


class OrbitExchExecutor:
    """
    OrbitExch 订单执行器

    使用已存在的 Playwright 页面执行订单操作。
    所有订单使用 POC (Pending until Cancel) 方式。

    注意: OrbitExch 使用 (100/概率) 类型的赔率，不是直接的概率值。
    """

    def __init__(
        self,
        config: ExecutionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # 页面引用 (从 OrbitExchOddsClient 获取)
        self._pages: dict[str, Page] = {}  # competition_id -> Page

        # 订单追踪
        self._orders: dict[str, Order] = {}  # order_id -> Order
        self._venue_orders: dict[str, str] = {}  # bet_uuid -> order_id

    def set_page(self, competition_id: str, page: Page) -> None:
        """
        设置页面引用

        Args:
            competition_id: 联赛 ID
            page: Playwright 页面
        """
        self._pages[competition_id] = page
        self._log.debug(f"Page set for competition: {competition_id}")

    async def place_order(self, order: Order, page: Page | None = None) -> ExecutionResult:
        """
        下单

        通过 HTTP POST 请求执行下单。

        Args:
            order: 订单
            page: Playwright 页面 (可选，用于获取 cookies/csrf)

        Returns:
            执行结果
        """
        if order.venue != Venue.ORBITEXCH:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Invalid venue: {order.venue}",
            )

        if not page and not self._pages:
            return ExecutionResult(
                success=False,
                order=order,
                message="No page available for execution",
            )

        # 获取可用页面
        if not page:
            page = next(iter(self._pages.values()))

        try:
            # 转换赔率格式
            # OrbitExch 使用 (100/概率) 格式，如概率 0.5 对应赔率 2.0
            # 输入的 price 是概率值，需要转换
            odds_price = round(100 / (order.price * 100), 2) if order.price > 0 else 1.01

            # 转换方向
            side = "BACK" if order.side in (OrderSide.BUY, OrderSide.BACK) else "LAY"

            # 生成唯一的 bet UUID
            bet_uuid = f"{order.market_id}_{order.selection_id}_{int(order.handicap)}__{int(time.time() * 1000)}"

            # 构建请求数据
            bet_data = {
                "selectionId": int(order.selection_id),
                "handicap": order.handicap,
                "price": odds_price,
                "size": order.size,
                "side": side,
                "betUuid": bet_uuid,
                "betType": "EXCHANGE",
                "netPLBetslipEnabled": False,
                "netPLMarketPageEnabled": False,
                "quickStakesEnabled": True,
                "confirmBetsEnabled": False,
                "applicationType": "WEB",
                "mobile": False,
                "isEachWay": False,
                "eachWayData": {},
                "page": "event",
                "persistenceType": self.config.orbitexch_default_persistence,
                "placedUsingEnterKey": False,
                "fillOrKill": order.order_type == OrderType.FOK,
            }

            payload = {order.market_id: [bet_data]}

            self._log.info(
                f"Placing OrbitExch order: market={order.market_id}, "
                f"selection={order.selection_id}, side={side}, "
                f"price={odds_price}, size={order.size}"
            )

            # 通过页面上下文发送请求
            response = await page.evaluate(
                """async (payload) => {
                    try {
                        const response = await fetch('/customer/api/placeBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            body: JSON.stringify(payload),
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }""",
                payload,
            )

            order.submitted_at = time.time()
            order.updated_at = time.time()

            # 解析响应
            if response and not response.get("error"):
                # OrbitExch 返回格式: {"betId": xxx, "status": "xxx", ...}
                order.venue_order_id = str(response.get("betId", bet_uuid))
                order.status = OrderStatus.LIVE

                # 检查是否立即成交
                status = response.get("status", "").upper()
                if status == "MATCHED":
                    order.status = OrderStatus.FILLED
                    order.filled_size = order.size
                elif status == "PARTIALLY_MATCHED":
                    order.status = OrderStatus.PARTIALLY_FILLED
                    order.filled_size = float(response.get("sizeMatched", 0))

                # 保存订单追踪
                self._orders[order.order_id] = order
                self._venue_orders[bet_uuid] = order.order_id

                self._log.info(
                    f"Order placed: venue_order_id={order.venue_order_id}, "
                    f"status={order.status.value}"
                )

                return ExecutionResult(
                    success=True,
                    order=order,
                    message="Order placed successfully",
                    venue_response=response,
                )
            else:
                order.status = OrderStatus.REJECTED
                order.error_message = response.get("error", "Unknown error")

                self._log.warning(f"Order rejected: {order.error_message}")

                return ExecutionResult(
                    success=False,
                    order=order,
                    message=order.error_message,
                    venue_response=response,
                )

        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error_message = str(e)
            order.updated_at = time.time()

            self._log.error(f"Failed to place order: {e}")

            return ExecutionResult(
                success=False,
                order=order,
                message=str(e),
            )

    async def cancel_order(self, order: Order, page: Page | None = None) -> CancelResult:
        """
        撤销订单

        通过点击网页上的 'Cancel Bet' 按钮执行撤单。

        Args:
            order: 订单
            page: Playwright 页面

        Returns:
            撤单结果
        """
        if not page and not self._pages:
            return CancelResult(
                success=False,
                order_id=order.order_id,
                message="No page available",
            )

        if not page:
            page = next(iter(self._pages.values()))

        if not order.venue_order_id:
            return CancelResult(
                success=False,
                order_id=order.order_id,
                message="No venue order ID",
            )

        try:
            self._log.info(f"Cancelling order: {order.venue_order_id}")

            # 方法1: 使用 API 撤单 (优先)
            response = await page.evaluate(
                """async (betId) => {
                    try {
                        const response = await fetch('/customer/api/cancelBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            body: JSON.stringify({ betIds: [betId] }),
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }""",
                order.venue_order_id,
            )

            if response and not response.get("error"):
                order.status = OrderStatus.CANCELLED
                order.updated_at = time.time()

                self._log.info(f"Order cancelled via API: {order.venue_order_id}")

                return CancelResult(
                    success=True,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Order cancelled via API",
                    venue_response=response,
                )

            # 方法2: 点击页面上的 Cancel Bet 按钮
            # OrbitExch UI 结构: 订单在右侧 Betslip 面板
            # 每个订单有 "Cancel Bet" 按钮 (绿色)
            # 订单引用号格式: "Ref: 212280836"

            # 策略1: 通过订单引用号定位到订单容器，然后找 Cancel Bet 按钮
            order_container = page.locator(f':has-text("Ref: {order.venue_order_id}")')
            cancel_button = order_container.locator('button:has-text("Cancel Bet")').first

            if await cancel_button.count() > 0:
                await cancel_button.click()
                await asyncio.sleep(0.5)

                order.status = OrderStatus.CANCELLED
                order.updated_at = time.time()

                self._log.info(f"Order cancelled via UI: {order.venue_order_id}")

                return CancelResult(
                    success=True,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Order cancelled via UI",
                )

            # 策略2: 通用选择器查找 Cancel Bet 按钮
            cancel_button = page.locator('button:has-text("Cancel Bet")').first

            if await cancel_button.count() > 0:
                await cancel_button.click()
                await asyncio.sleep(0.5)

                order.status = OrderStatus.CANCELLED
                order.updated_at = time.time()

                self._log.info(f"Order cancelled via UI (generic): {order.venue_order_id}")

                return CancelResult(
                    success=True,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Order cancelled via UI",
                )

            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message="Cancel button not found",
                venue_response=response if response else {},
            )

        except Exception as e:
            self._log.error(f"Failed to cancel order: {e}")

            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message=str(e),
            )

    async def cancel_all_unmatched(self, page: Page | None = None) -> CancelResult:
        """
        撤销所有未成交订单

        通过点击 'Cancel All Unmatched' 链接执行批量撤单。

        Args:
            page: Playwright 页面

        Returns:
            撤单结果
        """
        if not page and not self._pages:
            return CancelResult(
                success=False,
                order_id="all",
                message="No page available",
            )

        if not page:
            page = next(iter(self._pages.values()))

        try:
            self._log.info("Cancelling all unmatched orders")

            # 方法1: 使用 API 批量撤单
            response = await page.evaluate(
                """async () => {
                    try {
                        const response = await fetch('/customer/api/cancelAllUnmatchedBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }"""
            )

            if response and not response.get("error"):
                self._log.info("All unmatched orders cancelled via API")

                # 清除本地追踪的活跃订单
                for order in self._orders.values():
                    if order.status in (OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED):
                        order.status = OrderStatus.CANCELLED
                        order.updated_at = time.time()

                return CancelResult(
                    success=True,
                    order_id="all",
                    message="All unmatched orders cancelled via API",
                    venue_response=response,
                )

            # 方法2: 点击 "Cancel All Unmatched" 链接
            cancel_all_link = page.locator('text="Cancel All Unmatched"')

            if await cancel_all_link.count() > 0:
                await cancel_all_link.click()
                await asyncio.sleep(1.0)

                self._log.info("All unmatched orders cancelled via UI")

                # 清除本地追踪的活跃订单
                for order in self._orders.values():
                    if order.status in (OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED):
                        order.status = OrderStatus.CANCELLED
                        order.updated_at = time.time()

                return CancelResult(
                    success=True,
                    order_id="all",
                    message="All unmatched orders cancelled via UI",
                )

            return CancelResult(
                success=False,
                order_id="all",
                message="Cancel All Unmatched link not found",
                venue_response=response if response else {},
            )

        except Exception as e:
            self._log.error(f"Failed to cancel all unmatched: {e}")

            return CancelResult(
                success=False,
                order_id="all",
                message=str(e),
            )

    async def take_remaining_at_market(
        self, order: Order, page: Page | None = None
    ) -> ExecutionResult:
        """
        将未成交部分按市价立即执行

        通过点击 'Take @XX' 按钮执行（如 "Take @1.14"）。
        按钮显示格式: "Take @{price}" (蓝色按钮)
        下方显示 "Profit: {amount}"

        Args:
            order: 原订单
            page: Playwright 页面

        Returns:
            执行结果
        """
        if not page and not self._pages:
            return ExecutionResult(
                success=False,
                order=order,
                message="No page available",
            )

        if not page:
            page = next(iter(self._pages.values()))

        if order.remaining_size <= 0:
            return ExecutionResult(
                success=True,
                order=order,
                message="No remaining size",
            )

        try:
            self._log.info(
                f"Taking remaining at market: order={order.venue_order_id}, "
                f"remaining={order.remaining_size}"
            )

            # 查找 Take @XX 按钮
            # OrbitExch UI 结构: 订单在右侧 Betslip 面板
            # 按钮格式: "Take @1.14" (蓝色按钮)
            # 订单引用号格式: "Ref: 212280836"

            take_button = None

            # 策略1: 通过订单引用号定位到订单容器，然后找 Take @ 按钮
            if order.venue_order_id:
                order_container = page.locator(f':has-text("Ref: {order.venue_order_id}")')
                take_button = order_container.locator('button:has-text("Take @")').first

                if await take_button.count() == 0:
                    take_button = None

            # 策略2: 通用选择器查找 Take @ 按钮
            if not take_button or await take_button.count() == 0:
                take_button = page.locator('button:has-text("Take @")').first

            if take_button and await take_button.count() > 0:
                # 检查按钮是否可用 (disabled 状态时市价为 0)
                is_disabled = await take_button.is_disabled()
                if is_disabled:
                    self._log.warning("Take button is disabled - no market price available")
                    return ExecutionResult(
                        success=False,
                        order=order,
                        message="Take button disabled - no market price available",
                    )

                # 获取按钮文本以记录接受的价格
                button_text = await take_button.text_content()
                self._log.info(f"Clicking take button: {button_text}")

                await take_button.click()
                await asyncio.sleep(1.0)  # 等待成交

                order.status = OrderStatus.FILLED
                order.filled_size = order.size
                order.updated_at = time.time()

                self._log.info(f"Order filled at market: {order.venue_order_id}")

                return ExecutionResult(
                    success=True,
                    order=order,
                    message=f"Filled at market price ({button_text})",
                )

            # 如果找不到 Take 按钮，尝试使用 FOK 订单
            self._log.warning("Take button not found, using FOK order")

            # 先撤销原订单
            await self.cancel_order(order, page)

            # 创建 FOK 订单
            market_order = Order(
                venue=Venue.ORBITEXCH,
                pair_id=order.pair_id,
                market_type=order.market_type,
                market_id=order.market_id,
                selection_id=order.selection_id,
                handicap=order.handicap,
                side=order.side,
                price=order.price,
                size=order.remaining_size,
                order_type=OrderType.FOK,
                metadata={"original_order_id": order.order_id},
            )

            return await self.place_order(market_order, page)

        except Exception as e:
            self._log.error(f"Failed to take at market: {e}")

            return ExecutionResult(
                success=False,
                order=order,
                message=str(e),
            )

    async def get_current_bets(self, page: Page | None = None) -> list[dict]:
        """
        获取当前挂单

        Args:
            page: Playwright 页面

        Returns:
            挂单列表
        """
        if not page and not self._pages:
            return []

        if not page:
            page = next(iter(self._pages.values()))

        try:
            # 通过 API 获取当前订单
            response = await page.evaluate(
                """async () => {
                    try {
                        const response = await fetch('/customer/api/currentBets', {
                            method: 'GET',
                            headers: {
                                'Accept': 'application/json',
                            },
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }"""
            )

            if response and not response.get("error"):
                return response.get("bets", [])

            return []

        except Exception as e:
            self._log.error(f"Failed to get current bets: {e}")
            return []
