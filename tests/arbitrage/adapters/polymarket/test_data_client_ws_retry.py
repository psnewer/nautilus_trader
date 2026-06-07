"""PM DataClient WS 连接调度测试。"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class _FailingWsClient:
    has_subscriptions = True

    def __init__(self) -> None:
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        raise RuntimeError("network timeout")


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _client(loop: asyncio.AbstractEventLoop) -> PolymarketDataClient:
    return PolymarketDataClient(
        loop=loop,
        http_client=MagicMock(),
        msgbus=MessageBus(trader_id=TraderId("TESTER-001"), clock=LiveClock()),
        cache=Cache(),
        clock=LiveClock(),
        instrument_provider=MagicMock(spec=PolymarketInstrumentProvider),
        config=PolymarketDataClientConfig(ws_connection_initial_delay_secs=5),
        name="TEST-POLYMARKET",
    )


def _instrument() -> BinaryOption:
    instrument_id = InstrumentId.from_str("0xCONDITION-0xTOKEN.POLYMARKET")
    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=instrument_id.symbol,
        outcome="YES",
        description="Test Polymarket Instrument",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        size_precision=2,
        size_increment=Quantity.from_str("0.01"),
        activation_ns=0,
        expiration_ns=0,
        max_quantity=None,
        min_quantity=Quantity.from_int(1),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


def _deltas(instrument: BinaryOption) -> OrderBookDeltas:
    order = BookOrder(
        side=OrderSide.BUY,
        price=instrument.make_price(0.5),
        size=instrument.make_qty(10),
        order_id=0,
    )
    return OrderBookDeltas(
        instrument.id,
        [
            OrderBookDelta(
                instrument_id=instrument.id,
                action=BookAction.ADD,
                order=order,
                flags=RecordFlag.F_LAST,
                sequence=0,
                ts_event=0,
                ts_init=0,
            ),
        ],
    )


def test_delayed_connect_reschedules_after_failure() -> None:
    _run(_assert_delayed_connect_reschedules_after_failure())


async def _assert_delayed_connect_reschedules_after_failure() -> None:
    c = _client(asyncio.get_running_loop())
    ws = _FailingWsClient()
    c._ws_client = ws  # type: ignore[assignment]

    await c._delayed_connect(0)

    assert ws.connect_calls == 1
    assert c._ws_connect_task is not None
    assert not c._ws_connect_task.done()
    c._ws_connect_task.cancel()


def test_delayed_connect_does_not_retry_during_disconnect() -> None:
    _run(_assert_delayed_connect_does_not_retry_during_disconnect())


async def _assert_delayed_connect_does_not_retry_during_disconnect() -> None:
    c = _client(asyncio.get_running_loop())
    ws = _FailingWsClient()
    c._ws_client = ws  # type: ignore[assignment]
    c._disconnecting = True

    await c._delayed_connect(0)

    assert ws.connect_calls == 1
    assert c._ws_connect_task is None


def test_publish_deltas_records_first_pm_obd() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        emitted = []
        c._handle_data = lambda data: emitted.append(data)  # type: ignore[method-assign]
        instrument = _instrument()
        deltas = _deltas(instrument)

        c._publish_deltas(deltas)
        c._publish_deltas(deltas)

        assert c._book_deltas_published == 2
        assert emitted == [deltas, deltas]
    finally:
        loop.close()
