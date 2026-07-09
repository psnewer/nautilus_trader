"""
Debug ExecutionClient 子类 —— Q11.3 跳真执行 + mock 全成交(子类化 + 工厂选择,P10)。

设计见 `architectures/_cross-cutting/debug-injection.md`。

行为:覆盖 `_submit_order(command)` —— 当 `DebugConfig.is_override_active("skip_execution")`
真时,只跳过真 venue 上送;仍先进入 `_begin_session`,再 `generate_order_accepted` +
`generate_order_filled` mock 全成交事件;未激活时透传 `super()._submit_order(command)`(走真路径)。

**订单状态时序**(Q11.4 `timeline.py`)—— 支持通过 `mock_data.timeline` 配置部分成交、拒单、
撤单等时序场景。无 timeline 配置时走默认"立即全成"。

PM / OE / SE 三子类除 quote_currency / 基类不同外完全对称。
"""

from __future__ import annotations

from decimal import Decimal

from py_clob_client_v2.exceptions import PolyApiException

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.timeline import TimelineExecutor

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


def _mock_submit_with_timeline(client, command, quote_currency) -> None:
    """支持 timeline 的 mock submit:有 timeline 配置时按配置执行,否则立即全成。

    注:timeline 使用 NT Clock.set_time_alert_ns 调度,是同步函数。
    """
    debug = getattr(client, "_debug", None)
    if debug is not None:
        executor = TimelineExecutor(client, debug, quote_currency)
        if executor.has_timeline(command.order):
            executor.execute(command)  # 同步,内部用 Clock 调度
            return
    # 无 timeline 配置,走原有立即全成
    _mock_submit(client, command, quote_currency)


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
            # skip_execution 模式容忍 PM 连接瞬时失败:mock 订单 IO 不需要真连接,返回即视为已连接。
            # (#109/#110 退役 PM HealthCheckLoop 后,原 `self._health.start()` 是 stale 引用 → 删;
            #  否则瞬时 RuntimeError 进此分支会因 Attribute('_health') 反而让 _connect 真失败,卡住启动。)
            if isinstance(e, RuntimeError):
                self._log.warning(
                    f"skip_execution: PM execution connect tolerated preflight failure: {e!r}; "
                    "mock order IO remains enabled, true PM orders are not allowed in this mode",
                )
                return
            if getattr(e, "status_code", None) is not None:
                raise
            self._log.warning(
                f"skip_execution: PM execution connect tolerated transport failure: {e!r}; "
                "mock order IO remains enabled, true PM orders are not allowed in this mode",
            )

    async def _submit_order(self, command) -> None:
        if self._mock_orders():
            _mock_submit_with_timeline(self, command, USDC_POS)
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

    # 注(#122):PM report 失败容忍已下沉到 base `ArbPolymarketExecutionClient`(mark_dead + 返空,对齐 OE),
    # 故此处不再需要 skip-only 的 report override。`_connect` 的瞬时失败容忍仍保留(见上)。


class SkipExecutionOrbitExchClient(OrbitExchExecutionClient):
    """OE 执行客户端 Debug 子类(quote=USD)。"""

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
            _mock_submit_with_timeline(self, command, USD)
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


class SkipExecutionSharpExchClient(SharpExchExecutionClient):
    """SE 执行客户端 Debug 子类(quote=USD,对齐 OE)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug

    def _mock_orders(self) -> bool:
        """skip_execution 真 → mock 订单 IO(submit/cancel 不碰真 venue)。连接照常真跑。"""
        return self._debug.is_override_active(_SKIP_KEY)

    async def _submit_order(self, command) -> None:
        if self._mock_orders():
            _mock_submit_with_timeline(self, command, USD)
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
