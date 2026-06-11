"""
Debug ExecutionClient 子类 —— Q11.3 跳真执行 + mock 全成交(子类化 + 工厂选择,P10)。

设计见 `architectures/_cross-cutting/debug-injection.md`。

行为:覆盖 `_submit_order(command)` —— 当 `DebugConfig.is_override_active("skip_execution")`
真时,只跳过真 venue 上送;仍先进入 `_begin_session`,再 `generate_order_accepted` +
`generate_order_filled` mock 全成交事件;未激活时透传 `super()._submit_order(command)`(走真路径)。

**不实现订单状态时序**(Q11.4 `timeline.py`)—— 当前版本是"立即全成",足够覆盖大多数链路测试
(risk → submit → fill → portfolio 更新)。需要部分填 / 拒单 / 撤单时序的场景再加 timeline。

PM / OE 两子类除 quote_currency / 基类不同外完全对称。
"""

from __future__ import annotations

from decimal import Decimal

from py_clob_client_v2.exceptions import PolyApiException

from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient

from src.arbitrage.debug.config import DebugConfig

_SKIP_KEY = "skip_execution"


def _mock_fill(client, command, quote_currency) -> None:
    """共享 mock 全成交流程:Accepted → Filled,price 取 order.price(限价)或 0.5 兜底(测试)。"""
    order = command.order
    ts = client._clock.timestamp_ns()
    venue_order_id = VenueOrderId(f"MOCK-{order.client_order_id.value}")

    client.generate_order_accepted(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
        ts_event=ts,
    )

    last_qty = order.quantity if isinstance(order.quantity, Quantity) else Quantity.from_str(str(order.quantity))
    raw_price = getattr(order, "price", None)
    if raw_price is None:
        last_px = Price.from_str("0.5")  # market 单 / 无 price 时兜底
    else:
        last_px = raw_price if isinstance(raw_price, Price) else Price.from_str(str(raw_price))

    client.generate_order_filled(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
        venue_position_id=None,
        trade_id=TradeId(f"MOCK-{order.client_order_id.value}-1"),
        order_side=order.side,
        order_type=order.order_type,
        last_qty=last_qty,
        last_px=last_px,
        quote_currency=quote_currency,
        commission=Money(Decimal("0"), quote_currency),
        liquidity_side=LiquiditySide.TAKER,
        ts_event=ts,
    )


def _mock_submit(client, command, quote_currency) -> None:
    """skip_execution submit:保留 session/gate 生命周期,只替换 venue IO 为 mock fill。"""
    if not client._begin_session(command):
        return
    _mock_fill(client, command, quote_currency)


class SkipExecutionPolymarketClient(ArbPolymarketExecutionClient):
    """PM 执行客户端 Debug 子类(quote=USDC)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _mock_orders(self) -> bool:
        """skip_execution 真 → mock 订单 IO(submit/cancel 不碰真 venue)。连接照常真跑(#66)。"""
        return self._debug.is_override_active(_SKIP_KEY)

    async def _connect(self) -> None:
        if not self._mock_orders():
            await super()._connect()
            return
        try:
            await super()._connect()
        except (PolyApiException, RuntimeError) as e:
            if isinstance(e, RuntimeError):
                self._log.warning(
                    f"skip_execution: PM execution connect tolerated preflight failure: {e!r}; "
                    "mock order IO remains enabled, true PM orders are not allowed in this mode",
                )
                self._health.start()
                return
            if getattr(e, "status_code", None) is not None:
                raise
            self._log.warning(
                f"skip_execution: PM execution connect tolerated transport failure: {e!r}; "
                "mock order IO remains enabled, true PM orders are not allowed in this mode",
            )
            self._health.start()

    async def _submit_order(self, command) -> None:
        if self._mock_orders():
            _mock_submit(self, command, USDC_POS)
            return
        await super()._submit_order(command)

    async def _cancel_order(self, command) -> None:
        if self._mock_orders():
            return  # mock 模式无真单可撤
        await super()._cancel_order(command)

    async def _cancel_all_orders(self, command) -> None:
        if self._mock_orders():
            return
        await super()._cancel_all_orders(command)


class SkipExecutionOrbitExchClient(OrbitExchExecutionClient):
    """OE 执行客户端 Debug 子类(quote=GBP)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _mock_orders(self) -> bool:
        """skip_execution 真 → mock 订单 IO(submit/cancel 不碰真 venue)。连接照常真跑(#66)。"""
        return self._debug.is_override_active(_SKIP_KEY)

    # #66:不再覆盖 _connect/_disconnect —— 继承 base 真连接(#63 Gap C:登录/page/general WS/账户状态),
    # 与 PM 对齐。skip 下仍真连接、只 mock 订单 IO,故 skip_execution=true 即「安全验连接路径」smoke。

    async def _submit_order(self, command) -> None:
        if self._mock_orders():
            _mock_submit(self, command, GBP)
            return
        await super()._submit_order(command)

    async def _cancel_order(self, command) -> None:
        if self._mock_orders():
            return  # mock 模式无真单可撤
        await super()._cancel_order(command)

    async def _cancel_all_orders(self, command) -> None:
        if self._mock_orders():
            return
        await super()._cancel_all_orders(command)

    async def _cancel_residual_one(self, order) -> None:
        if self._mock_orders():
            return
        await super()._cancel_residual_one(order)
