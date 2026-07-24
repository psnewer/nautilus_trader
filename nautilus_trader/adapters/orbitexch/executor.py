"""
OrbitExch 订单执行器

使用 Playwright 与网页交互执行订单。

实现逻辑:
1. 下单:通过 HTTP POST 请求到 `/customer/api/placeBets`
2. 撤单:通过 OE API 撤单
"""

import logging
import time
from dataclasses import dataclass
from typing import Callable

from playwright.async_api import Page

from nautilus_trader.adapters.orbitexch.web import oe_csrf_token

from src.arbitrage.common.execution_config import ExecutionConfig


@dataclass(frozen=True)
class OrbitExchOrderRequest:
    """OE executor 的无状态下单请求。订单生命周期只由 NT Cache 管理。"""

    client_order_id: str
    market_id: str
    selection_id: str
    handicap: float
    side: str
    price: float
    size: float
    order_type: str = "GTC"


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
        fx_getter: Callable[[], float] | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)
        self._fx_getter = fx_getter

        # 页面引用 (从 OrbitExchOddsClient 获取)
        self._pages: dict[str, Page] = {}  # competition_id -> Page

    def set_page(self, competition_id: str, page: Page) -> None:
        """
        设置页面引用

        Args:
            competition_id: 联赛 ID
            page: Playwright 页面
        """
        self._pages[competition_id] = page
        self._log.debug(f"Page set for competition: {competition_id}")

    async def place_order(
        self,
        order: OrbitExchOrderRequest,
        page: Page | None = None,
    ) -> dict:
        """
        下单

        通过 HTTP POST 请求执行下单。

        Args:
            order: 冻结的 OE 下单请求
            page: Playwright 页面 (可选，用于获取 cookies/csrf)

        Returns:
            执行结果
        """
        if not page and not self._pages:
            self._log.error(
                f"Order {order.client_order_id} failed: no OrbitExch execution page",
            )
            return {"success": False, "message": "No page available for execution"}

        # 获取可用页面
        if not page:
            page = next(iter(self._pages.values()))

        # 验证订单数据
        if not order.market_id or not order.selection_id:
            self._log.error(
                f"Order {order.client_order_id} failed: missing market_id={order.market_id} "
                f"or selection_id={order.selection_id}",
            )
            return {"success": False, "message": "Missing market_id or selection_id"}

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
            side = "BACK" if order.side == "BACK" else "LAY"

            # 生成唯一的 bet UUID
            bet_uuid = f"{order.market_id}_{order.selection_id}_{int(order.handicap)}__{int(time.time() * 1000)}"

            fx = self._fx_getter() if self._fx_getter is not None else 1.0
            if fx <= 0:
                self._log.error(f"Order {order.client_order_id} failed: invalid fx={fx}")
                return {"success": False, "message": f"Invalid fx: {fx}"}
            gbp_size = order.size / fx

            # 构建请求数据。adapter 外部 order.size 为 USD 口径,OE payload 需要 GBP stake。
            bet_data = {
                "selectionId": int(order.selection_id),
                "handicap": order.handicap,
                "price": odds_price,
                "size": round(gbp_size, 2),
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
                "fillOrKill": order.order_type == "FOK",
            }

            payload = {order.market_id: [bet_data]}

            self._log.info(
                f"Placing OrbitExch order: market={order.market_id}, "
                f"selection={order.selection_id}, side={side}, "
                f"price={odds_price}, size_usd={order.size}, fx={fx}, "
                f"size_gbp={gbp_size}, payload={payload}"
            )

            # 通过页面上下文发送请求，包含 CSRF token
            csrf_token = await oe_csrf_token(page)
            if not csrf_token:
                return {"success": False, "message": "CSRF token not found"}

            response = await page.evaluate(
                """async ({payload, csrfToken}) => {
                    try {
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
                        return { error: error.message, _transport_error: true };
                    }
                }""",
                {"payload": payload, "csrfToken": csrf_token},
            )

            # 记录原始响应以便调试
            self._log.info(f"OrbitExch API response: {response}")

            # 解析响应
            # OrbitExch 返回格式:
            # - 成功: {market_id: {"status": "OK", "betDelay": 1, "offerIds": {bet_uuid: offer_id}}}
            # - 错误: {"code": 405, "message": "..."} 或 {"error": "..."}

            # 检查全局错误
            if not response:
                return {
                    "success": False,
                    "message": "No response",
                    "venue_response": {"_transport_error": True},
                }

            if response.get("error"):
                message = str(response.get("error"))
                self._log.warning(f"Order rejected: {message}")
                return {
                    "success": False,
                    "message": message,
                    "venue_response": response,
                }

            error_code = response.get("code")
            if error_code and error_code != 200:
                message = str(response.get("message") or f"code={error_code}")
                self._log.warning(f"Order rejected: {message}")
                return {
                    "success": False,
                    "message": message,
                    "venue_response": response,
                }

            # 检查市场级别响应
            market_response = response.get(order.market_id)
            if market_response and market_response.get("status") == "OK":
                # 成功！从 offerIds 中获取订单 ID
                offer_ids = market_response.get("offerIds", {})
                venue_order_id = offer_ids.get(bet_uuid)

                if venue_order_id:
                    venue_order_id = str(venue_order_id)
                else:
                    # 如果找不到特定 bet_uuid，取第一个
                    venue_order_id = (
                        str(list(offer_ids.values())[0])
                        if offer_ids
                        else bet_uuid
                    )

                self._log.info(f"Order placed: venue_order_id={venue_order_id}")
                return {
                    "success": True,
                    "venue_order_id": venue_order_id,
                    "message": "Order placed successfully",
                    "venue_response": response,
                    "venue_payload": payload,
                    "bet_uuid": bet_uuid,
                }

            # 市场级别错误
            if market_response:
                error_msg = market_response.get("message") or market_response.get("status") or "Unknown market error"
            else:
                error_msg = f"No response for market {order.market_id}"

            self._log.warning(f"Order rejected: {error_msg}, response={response}")
            return {
                "success": False,
                "message": error_msg,
                "venue_response": (
                    response
                    if market_response is not None
                    else {**response, "_transport_error": True}
                ),
            }

        except Exception as e:
            self._log.error(f"Failed to place order: {e}")
            return {
                "success": False,
                "message": str(e),
                "venue_response": {"_transport_error": True},
            }

    async def cancel_order(
        self,
        market_id: str,
        venue_order_id: str,
        page: Page | None = None,
    ) -> dict:
        """
        撤销订单

        通过 API 撤单执行撤单。

        Args:
            market_id: OE market ID
            venue_order_id: OE offer ID
            page: Playwright 页面

        Returns:
            撤单结果
        """
        if not page and not self._pages:
            self._log.error(f"Cancel failed: no page available (order={venue_order_id})")
            return {"success": False, "message": "No page available"}

        if not page:
            page = next(iter(self._pages.values()))

        if not venue_order_id:
            self._log.error("Cancel failed: no venue_order_id")
            return {"success": False, "message": "No venue order ID"}

        try:
            self._log.info(f"Cancelling order: {venue_order_id}")

            if not market_id:
                self._log.error(
                    f"Cancel failed: missing market_id (venue_order_id={venue_order_id})",
                )
                return {"success": False, "message": "Missing market_id for cancel"}

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
                self._log.error("Cancel failed: CSRF token not found in cookies")
                return {"success": False, "message": "CSRF token not found"}
            response = await page.evaluate(
                """async (payload) => {
                            const trace = [];
                            trace.push({s: 'start', t: Date.now()});
                            try {
                                trace.push({s: 'before-fetch', t: Date.now()});
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
                                trace.push({
                                    s: 'after-fetch',
                                    status: response.status,
                                    ct: response.headers.get('content-type'),
                                    t: Date.now(),
                                });
                                const text = await response.text();
                                trace.push({s: 'after-text', len: text.length, t: Date.now()});
                                try {
                                    const json = JSON.parse(text);
                                    trace.push({s: 'json-ok', t: Date.now()});
                                    return Object.assign({}, json, {_trace: trace});
                                } catch (e) {
                                    trace.push({s: 'json-fail', err: e.message, t: Date.now()});
                                    return {
                                        error: 'json_parse_failed: ' + e.message,
                                        _transport_error: true,
                                        _trace: trace,
                                        _raw_sample: text.slice(0, 500),
                                    };
                                }
                            } catch (error) {
                                trace.push({s: 'fetch-error', err: error.message, t: Date.now()});
                                return {error: error.message, _transport_error: true, _trace: trace};
                            }
                }""",
                {
                    "csrfToken": csrf_token,
                    "cookieHeader": cookie_header,
                    "body": {
                        market_id: [
                            {
                                "offerId": venue_order_id,
                                "betType": "EXCHANGE",
                            },
                        ],
                    },
                },
            )

            if response and isinstance(response, dict) and "_trace" in response:
                self._log.debug(f"Cancel trace: {response.get('_trace')}")
                if response.get("_raw_sample"):
                    self._log.warning(
                        f"Cancel response not JSON, raw sample: {response.get('_raw_sample')!r}"
                    )

            if response and not response.get("error"):
                self._log.info(f"Order cancelled via API: {venue_order_id}")
                return {
                    "success": True,
                    "message": "Order cancelled via API",
                    "venue_response": response,
                }

            return {
                "success": False,
                "message": response.get("error", "Cancel failed") if response else "Cancel failed",
                "venue_response": response if response else {"_transport_error": True},
            }

        except Exception as e:
            self._log.error(f"Failed to cancel order: {e}")
            return {
                "success": False,
                "message": str(e),
                "venue_response": {"_transport_error": True},
            }

    async def cancel_all_unmatched(self, page: Page | None = None) -> dict:
        """通过 OE API 撤销所有未成交订单。

        Args:
            page: Playwright 页面

        Returns:
            撤单结果
        """
        if not page and not self._pages:
            return {"success": False, "message": "No page available"}

        if not page:
            page = next(iter(self._pages.values()))

        try:
            self._log.info("Cancelling all unmatched orders")

            csrf_token = await oe_csrf_token(page)
            response = await page.evaluate(
                """async (csrfToken) => {
                    try {
                        const response = await fetch('/customer/api/cancelAllUnmatchedBets', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken || '',
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
                            },
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }""",
                csrf_token,
            )

            if response and not response.get("error"):
                self._log.info("All unmatched orders cancelled via API")
                return {
                    "success": True,
                    "message": "All unmatched orders cancelled via API",
                    "venue_response": response,
                }

            return {
                "success": False,
                "message": (response or {}).get("error", "Cancel all unmatched failed"),
                "venue_response": response if response else {},
            }

        except Exception as e:
            self._log.error(f"Failed to cancel all unmatched: {e}")
            return {"success": False, "message": str(e)}

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
            csrf_token = await oe_csrf_token(page)
            response = await page.evaluate(
                """async (csrfToken) => {
                    try {
                        const response = await fetch('/customer/api/currentBets', {
                            method: 'GET',
                            headers: {
                                'Accept': 'application/json, text/plain, */*',
                                'x-csrf-token': csrfToken || '',
                                'Origin': window.location.origin,
                                'Referer': window.location.href,
                            },
                            credentials: 'include',
                        });
                        return await response.json();
                    } catch (error) {
                        return { error: error.message };
                    }
                }""",
                csrf_token,
            )

            if response and not response.get("error"):
                return response.get("bets", [])

            return []

        except Exception as e:
            self._log.error(f"Failed to get current bets: {e}")
            return []
