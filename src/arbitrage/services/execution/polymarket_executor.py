"""
Polymarket 订单执行器

使用 py-clob-client 执行订单。

参考文档:
- https://docs.polymarket.com/developers/CLOB/orders/create-order
- https://github.com/Polymarket/py-clob-client
"""

import asyncio
import logging
import time
from typing import Any

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

# 尝试导入 py-clob-client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    ClobClient = None


class PolymarketExecutor:
    """
    Polymarket 订单执行器

    使用 py-clob-client 官方客户端执行订单。
    所有订单强制使用 FOK (Fill-Or-Kill) 类型，确保全部成交或全部取消，
    避免部分成交后需要撤单的情况。
    """

    def __init__(
        self,
        config: ExecutionConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

        # CLOB 客户端
        self._client: ClobClient | None = None

        # 检查依赖
        if not HAS_CLOB_CLIENT:
            self._log.warning(
                "py-clob-client not installed. "
                "Install with: pip install py-clob-client"
            )

    async def initialize(self) -> bool:
        """
        初始化客户端

        Returns:
            是否初始化成功
        """
        if not HAS_CLOB_CLIENT:
            self._log.error("py-clob-client not installed")
            return False

        if not self.config.polymarket_private_key:
            self._log.error("Polymarket private key not configured")
            return False

        try:
            # 初始化 CLOB 客户端
            # 参考: https://github.com/Polymarket/py-clob-client
            self._client = ClobClient(
                host=self.config.polymarket_clob_url,
                key=self.config.polymarket_private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=1,  # 1 for email/Magic wallet signatures
                funder=self.config.polymarket_funder or None,
            )

            # 设置 API credentials（使用线程池避免阻塞）
            api_creds = await asyncio.to_thread(
                self._client.create_or_derive_api_creds
            )
            self._client.set_api_creds(api_creds)

            self._log.info("Polymarket executor initialized")
            return True

        except Exception as e:
            self._log.error(f"Failed to initialize Polymarket client: {e}")
            return False

    async def place_order(self, order: Order) -> ExecutionResult:
        """
        下单

        Args:
            order: 订单

        Returns:
            执行结果
        """
        if not self._client:
            self._log.error(f"Order {order.order_id} failed: Polymarket client not initialized")
            return ExecutionResult(
                success=False,
                order=order,
                message="Client not initialized",
            )

        if order.venue != Venue.POLYMARKET:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Invalid venue: {order.venue}",
            )

        # 验证订单数据
        if not order.token_id:
            self._log.error(f"Order {order.order_id} failed: missing token_id")
            return ExecutionResult(
                success=False,
                order=order,
                message="Missing token_id",
            )

        try:
            # 市价单使用 FOK；限价单使用订单自身的类型（默认 GTC）
            if order.metadata.get("market_order"):
                clob_order_type = self._convert_order_type(OrderType.FOK)
                self._log.info("Using FOK order type (market order)")
            else:
                clob_order_type = self._convert_order_type(order.order_type)
                self._log.info(f"Using {order.order_type.value} order type (limit order)")

            # 转换订单方向
            side = "BUY" if order.side in (OrderSide.BUY, OrderSide.BACK) else "SELL"

            if order.metadata.get("market_order"):
                # 市价单：BUY 用金额，SELL 用份额，price 作为最差成交价限制
                amount = order.size
                if side == "BUY":
                    amount = order.size * order.price

                self._log.info(
                    f"Placing Polymarket market order: token={order.token_id[:20]}..., "
                    f"side={side}, amount={amount:.4f}, price={order.price}"
                )

                # 创建并签名市价订单（使用线程池避免阻塞 event loop）
                signed_order = await asyncio.to_thread(
                    self._client.create_market_order,
                    token_id=order.token_id,
                    side=side,
                    amount=amount,
                    price=order.price,
                )
            else:
                # 创建订单参数
                order_args = OrderArgs(
                    token_id=order.token_id,
                    price=order.price,
                    size=order.size,
                    side=side,
                )

                self._log.info(
                    f"Placing Polymarket order: token={order.token_id[:20]}..., "
                    f"side={side}, price={order.price}, size={order.size}"
                )

                # 创建并签名订单（使用线程池避免阻塞 event loop）
                signed_order = await asyncio.to_thread(
                    self._client.create_order, order_args
                )

            # 提交订单（使用线程池避免阻塞 event loop）
            response = await asyncio.to_thread(
                self._client.post_order, signed_order, clob_order_type
            )

            # 更新订单状态
            order.submitted_at = time.time()
            order.updated_at = time.time()

            if response.get("success"):
                order.venue_order_id = response.get("orderID", "")
                order.status = OrderStatus.LIVE

                # 检查是否立即成交
                if response.get("status") == "MATCHED":
                    order.status = OrderStatus.FILLED
                    order.filled_size = order.size

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
                order.error_message = response.get("errorMsg", "Unknown error")

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

    async def cancel_order(self, order: Order) -> CancelResult:
        """
        撤销订单

        Args:
            order: 订单

        Returns:
            撤单结果
        """
        if not self._client:
            return CancelResult(
                success=False,
                order_id=order.order_id,
                message="Client not initialized",
            )

        if not order.venue_order_id:
            return CancelResult(
                success=False,
                order_id=order.order_id,
                message="No venue order ID",
            )

        try:
            self._log.info(f"Cancelling order: {order.venue_order_id}")

            response = await asyncio.to_thread(
                self._client.cancel, order.venue_order_id
            )

            if response.get("success") or response.get("canceled"):
                order.status = OrderStatus.CANCELLED
                order.updated_at = time.time()

                self._log.info(f"Order cancelled: {order.venue_order_id}")

                return CancelResult(
                    success=True,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message="Order cancelled",
                    venue_response=response,
                )
            else:
                return CancelResult(
                    success=False,
                    order_id=order.order_id,
                    venue_order_id=order.venue_order_id,
                    message=response.get("errorMsg", "Cancel failed"),
                    venue_response=response,
                )

        except Exception as e:
            self._log.error(f"Failed to cancel order: {e}")

            return CancelResult(
                success=False,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                message=str(e),
            )

    async def take_remaining_at_market(self, order: Order) -> ExecutionResult:
        """
        将未成交部分按市价立即执行

        使用 FOK 订单类型重新下单。

        Args:
            order: 原订单

        Returns:
            执行结果
        """
        if order.remaining_size <= 0:
            return ExecutionResult(
                success=True,
                order=order,
                message="No remaining size",
            )

        # 先撤销原订单
        cancel_result = await self.cancel_order(order)
        if not cancel_result.success:
            self._log.warning(f"Failed to cancel original order: {cancel_result.message}")

        # 创建市价订单 (FOK)
        market_order = Order(
            venue=Venue.POLYMARKET,
            pair_id=order.pair_id,
            market_type=order.market_type,
            token_id=order.token_id,
            condition_id=order.condition_id,
            side=order.side,
            price=order.price,  # FOK 会按最优价格成交
            size=order.remaining_size,
            order_type=OrderType.FOK,
            metadata={"original_order_id": order.order_id},
        )

        return await self.place_order(market_order)

    def _convert_order_type(self, order_type: OrderType) -> Any:
        """转换订单类型为 CLOB 客户端类型"""
        if not HAS_CLOB_CLIENT:
            return None

        mapping = {
            OrderType.GTC: ClobOrderType.GTC,
            OrderType.FOK: ClobOrderType.FOK,
            OrderType.FAK: ClobOrderType.FAK,
        }
        return mapping.get(order_type, ClobOrderType.GTC)
