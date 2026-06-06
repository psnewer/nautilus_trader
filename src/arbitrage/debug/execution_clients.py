"""
Debug ExecutionClient 子类 —— Q11.3 跳真执行 + mock 全成交(子类化 + 工厂选择,P10)。

设计见 `architectures/_cross-cutting/debug-injection.md`。

行为:覆盖 `_submit_order(command)` —— 当 `DebugConfig.is_override_active("skip_execution")`
真时,跳过 `_begin_session` + 真 venue 上送,直接 `generate_order_accepted` + `generate_order_filled`
mock 全成交事件;未激活时透传 `super()._submit_order(command)`(走真路径)。

**不实现订单状态时序**(Q11.4 `timeline.py`)—— 当前版本是"立即全成",足够覆盖大多数链路测试
(risk → submit → fill → portfolio 更新)。需要部分填 / 拒单 / 撤单时序的场景再加 timeline。

PM / OE 两子类除 quote_currency / 基类不同外完全对称。
"""

from __future__ import annotations

from decimal import Decimal

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


class SkipExecutionPolymarketClient(ArbPolymarketExecutionClient):
    """PM 执行客户端 Debug 子类(quote=USDC)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    async def _submit_order(self, command) -> None:
        if self._debug.is_override_active(_SKIP_KEY):
            _mock_fill(self, command, USDC_POS)
            return
        await super()._submit_order(command)


class SkipExecutionOrbitExchClient(OrbitExchExecutionClient):
    """OE 执行客户端 Debug 子类(quote=GBP)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    async def _connect(self) -> None:
        """skip_execution=true 时不真出单,**_connect no-op 安全**(BrowserManager 由 DataClient
        起共享 context;OE Exec 在 skip 模式下不需独立 page / WS)。非 skip 时透传 base 真 `_connect`
        (#63 Gap C:login + executor + general WS,2026-06-06 live 验过,[[gap_c_oe_exec_live_validated]])。"""
        if self._debug.is_override_active(_SKIP_KEY):
            return  # no-op,connect 状态自动 transition 成功
        await super()._connect()  # 非 skip:走 Gap C 真接线

    async def _disconnect(self) -> None:
        """同上:skip 模式 no-op。"""
        if self._debug.is_override_active(_SKIP_KEY):
            return
        await super()._disconnect()

    async def _submit_order(self, command) -> None:
        if self._debug.is_override_active(_SKIP_KEY):
            _mock_fill(self, command, GBP)
            return
        await super()._submit_order(command)
