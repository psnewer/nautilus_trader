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
            self._log.error(f"Order {order.order_id} failed: No OrbitExch page available for execution")
            return ExecutionResult(
                success=False,
                order=order,
                message="No page available for execution",
            )

        # 获取可用页面
        if not page:
            page = next(iter(self._pages.values()))

        # 验证订单数据
        if not order.market_id or not order.selection_id:
            self._log.error(
                f"Order {order.order_id} failed: missing market_id={order.market_id} "
                f"or selection_id={order.selection_id}"
            )
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Missing market_id or selection_id",
            )

        try:
            # order.price 已经是 OrbitExch 的赔率格式（如 2.0 表示 50% 概率）
            # 直接使用，无需转换
            odds_price = round(order.price, 2) if order.price > 0 else 1.01

            # 验证赔率范围（OrbitExch 通常接受 1.01 - 1000）
            if odds_price < 1.01:
                odds_price = 1.01
                self._log.warning(f"Adjusted odds_price to minimum 1.01 (was {order.price})")
            elif odds_price > 1000:
                odds_price = 1000
                self._log.warning(f"Adjusted odds_price to maximum 1000 (was {order.price})")

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
                f"price={odds_price}, size={order.size}, payload={payload}"
            )

            # 通过页面上下文发送请求，包含 CSRF token
            response = await page.evaluate(
                """async (payload) => {
                    try {
                        // 从 cookie 中提取 CSRF token
                        const cookies = document.cookie.split(';');
                        let csrfToken = '';
                        for (const cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'CSRF-TOKEN') {
                                csrfToken = decodeURIComponent(value);
                                break;
                            }
                        }

                        if (!csrfToken) {
                            return { error: 'CSRF token not found in cookies' };
                        }

                        const bodyStr = JSON.stringify(payload);

                        const response = await fetch('/customer/api/placeBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken,
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
                            },
                            body: bodyStr,
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

            # 记录原始响应以便调试
            self._log.info(f"OrbitExch API response: {response}")

            # 解析响应
            # OrbitExch 返回格式:
            # - 成功: {market_id: {"status": "OK", "betDelay": 1, "offerIds": {bet_uuid: offer_id}}}
            # - 错误: {"code": 405, "message": "..."} 或 {"error": "..."}

            # 检查全局错误
            if not response:
                order.status = OrderStatus.REJECTED
                order.error_message = "No response"
                self._log.warning(f"Order rejected: {order.error_message}")
                return ExecutionResult(
                    success=False,
                    order=order,
                    message=order.error_message,
                    venue_response=response,
                )

            if response.get("error"):
                order.status = OrderStatus.REJECTED
                order.error_message = response.get("error")
                self._log.warning(f"Order rejected: {order.error_message}")
                return ExecutionResult(
                    success=False,
                    order=order,
                    message=order.error_message,
                    venue_response=response,
                )

            error_code = response.get("code")
            if error_code and error_code != 200:
                order.status = OrderStatus.REJECTED
                order.error_message = response.get("message") or f"code={error_code}"
                self._log.warning(f"Order rejected: {order.error_message}")
                return ExecutionResult(
                    success=False,
                    order=order,
                    message=order.error_message,
                    venue_response=response,
                )

            # 检查市场级别响应
            market_response = response.get(order.market_id)
            if market_response and market_response.get("status") == "OK":
                # 成功！从 offerIds 中获取订单 ID
                offer_ids = market_response.get("offerIds", {})
                venue_order_id = offer_ids.get(bet_uuid)

                if venue_order_id:
                    order.venue_order_id = str(venue_order_id)
                else:
                    # 如果找不到特定 bet_uuid，取第一个
                    order.venue_order_id = str(list(offer_ids.values())[0]) if offer_ids else bet_uuid

                order.status = OrderStatus.LIVE

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

            # 市场级别错误
            if market_response:
                error_msg = market_response.get("message") or market_response.get("status") or "Unknown market error"
            else:
                error_msg = f"No response for market {order.market_id}"

            order.status = OrderStatus.REJECTED
            order.error_message = error_msg
            self._log.warning(f"Order rejected: {order.error_message}, response={response}")

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

        通过 API 撤单执行撤单。

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

            if not order.market_id:
                return CancelResult(
                    success=False,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Missing market_id for cancel",
                )

            cookies = await page.context.cookies()
            cookie_names = {
                "BIAB_AN",
                "BIAB_LANGUAGE",
                "BIAB_TZ",
                "_gid",
                "_gat_gtag_UA_252822765_1",
                "CSRF-TOKEN",
                "COLLAPSE-LEFT_PANEL_COLLAPSE_GROUP-SPORT_COLLAPSE",
                "BIAB_CUSTOMER",
                "BIAB_LOGIN_POP_UP_SHOWN",
                "BIAB_SHOW_TOOLTIPS",
                "_ga",
                "_ga_R0X6ZP423B",
                "AWSALB",
                "AWSALBCORS",
            }
            cookie_pairs = [
                f"{c['name']}={c['value']}"
                for c in cookies
                if c.get("name") in cookie_names
            ]
            cookie_header = "; ".join(cookie_pairs)
            csrf_token = ""
            for c in cookies:
                if c.get("name") == "CSRF-TOKEN":
                    csrf_token = c.get("value", "")
                    break

            if not csrf_token:
                return CancelResult(
                    success=False,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="CSRF token not found",
                )

            response = await page.evaluate(
                """async (payload) => {
                    try {
                        const response = await fetch('/customer/api/cancelBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': payload.csrfToken,
                                'x-device': 'DESKTOP',
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
                                'Cookie': payload.cookieHeader,
                            },
                            body: JSON.stringify(payload.body),
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }""",
                {
                    "csrfToken": csrf_token,
                    "cookieHeader": cookie_header,
                    "body": {
                        order.market_id: [
                            {
                                "offerId": order.venue_order_id,
                                "betType": "EXCHANGE",
                            }
                        ]
                    },
                },
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

            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message=response.get("error", "Cancel failed"),
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

    async def _cancel_order_legacy(self, order: Order, page: Page | None = None) -> CancelResult:
        """
        旧版撤单流程（暂时保留）
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
            self._log.info(f"Cancelling order (legacy): {order.venue_order_id}")

            response = await page.evaluate(
                """async (betId) => {
                    try {
                        // 从 cookie 中提取 CSRF token
                        const cookies = document.cookie.split(';');
                        let csrfToken = '';
                        for (const cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'CSRF-TOKEN') {
                                csrfToken = decodeURIComponent(value);
                                break;
                            }
                        }

                        const response = await fetch('/customer/api/cancelBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken,
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
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

                self._log.info(f"Order cancelled via API (legacy): {order.venue_order_id}")

                return CancelResult(
                    success=True,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Order cancelled via API (legacy)",
                    venue_response=response,
                )

            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message=response.get("error", "Cancel failed"),
                venue_response=response if response else {},
            )

        except Exception as e:
            self._log.error(f"Failed to cancel order (legacy): {e}")

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
                        // 从 cookie 中提取 CSRF token
                        const cookies = document.cookie.split(';');
                        let csrfToken = '';
                        for (const cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'CSRF-TOKEN') {
                                csrfToken = decodeURIComponent(value);
                                break;
                            }
                        }

                        const response = await fetch('/customer/api/cancelAllUnmatchedBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken,
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
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
        self, order: Order, page: Page | None = None, new_size: float | None = None
    ) -> ExecutionResult:
        """
        将未成交部分按市价立即执行

        通过修改 Stake 输入框（可选）然后点击 'Take @XX' 按钮执行。
        OrbitExch UI 结构:
        - Stake 输入框: class='_input__size_a91v6_40'
        - Take 按钮: "Take @{price}" (绿色按钮)

        Args:
            order: 原订单
            page: Playwright 页面
            new_size: 新的 size（如果需要修改）

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

        try:
            self._log.info(
                f"Taking at market: order={order.venue_order_id}, "
                f"new_size={new_size}"
            )

            # 定位订单容器
            order_container = None
            if order.venue_order_id:
                # 通过订单引用号定位
                order_container = page.locator(f':has-text("Ref: {order.venue_order_id}")')
                if await order_container.count() == 0:
                    order_container = None

            # 如果需要修改 size
            if new_size is not None and new_size > 0:
                self._log.info(f"Modifying stake to: {new_size}")

                # 查找 Stake 输入框
                stake_input = None
                if order_container:
                    stake_input = order_container.locator('input[class*="_input__size_"]').first
                    if await stake_input.count() == 0:
                        stake_input = None

                if not stake_input:
                    # 通用选择器
                    stake_input = page.locator('input[class*="_input__size_"]').first

                if stake_input and await stake_input.count() > 0:
                    # 清空并填入新 size
                    await stake_input.click()
                    await stake_input.fill("")
                    await stake_input.fill(str(new_size))
                    await asyncio.sleep(0.5)  # 等待 UI 更新
                    self._log.info(f"Stake modified to: {new_size}")
                else:
                    self._log.warning("Stake input not found, proceeding without modification")

            # 查找 Take @XX 按钮
            take_button = None
            if order_container:
                take_button = order_container.locator('button:has-text("Take @")').first
                if await take_button.count() == 0:
                    take_button = None

            if not take_button:
                take_button = page.locator('button:has-text("Take @")').first

            if take_button and await take_button.count() > 0:
                # 检查按钮是否可用
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
                order.filled_size = new_size if new_size else order.size
                order.updated_at = time.time()

                self._log.info(f"Order filled at market: {order.venue_order_id}")

                return ExecutionResult(
                    success=True,
                    order=order,
                    message=f"Filled at market price ({button_text})",
                )

            # 如果找不到 Take 按钮
            self._log.warning("Take button not found")
            return ExecutionResult(
                success=False,
                order=order,
                message="Take button not found",
            )

        except Exception as e:
            self._log.error(f"Failed to take at market: {e}")

            return ExecutionResult(
                success=False,
                order=order,
                message=str(e),
            )

    async def modify_size_and_take(
        self, order: Order, new_size: float, page: Page | None = None
    ) -> ExecutionResult:
        """
        修改订单 size 后按市价执行

        这是 take_remaining_at_market 的便捷方法。

        Args:
            order: 原订单
            new_size: 新的 size
            page: Playwright 页面

        Returns:
            执行结果
        """
        return await self.take_remaining_at_market(order, page, new_size=new_size)

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
                        // 从 cookie 中提取 CSRF token
                        const cookies = document.cookie.split(';');
                        let csrfToken = '';
                        for (const cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'CSRF-TOKEN') {
                                csrfToken = decodeURIComponent(value);
                                break;
                            }
                        }

                        const response = await fetch('/customer/api/currentBets', {
                            method: 'GET',
                            headers: {
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken,
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
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
