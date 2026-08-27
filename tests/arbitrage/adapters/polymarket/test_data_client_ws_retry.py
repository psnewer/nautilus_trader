"""PM DataClient WS 连接调度测试。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuote
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuotes
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import CustomData
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.market_order_book import MarketOrderBookDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameProcessed
from nautilus_trader.model.market_order_book import market_order_book_data_type
from nautilus_trader.model.market_order_book import order_book_frame_processed_topic
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


def _instrument(*, token_id: str = "0xTOKEN", outcome: str = "YES") -> BinaryOption:
    instrument_id = InstrumentId.from_str(f"0xCONDITION-{token_id}.POLYMARKET")
    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=instrument_id.symbol,
        outcome=outcome,
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


def test_quotes_publish_one_market_batch_for_all_changed_assets() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        yes = _instrument(token_id="0xYES", outcome="YES")
        no = _instrument(token_id="0xNO", outcome="NO")
        for instrument in (yes, no):
            c._cache.add_instrument(instrument)
            c._create_local_book(instrument.id)

        market_id = "0xCONDITION"
        data_type = market_order_book_data_type(Venue("POLYMARKET"), market_id)
        c._add_subscription(data_type)
        c._market_order_book_members[market_id] = (yes.id, no.id)
        c._market_books_bootstrapped.add(market_id)
        captured = []
        c._handle_data = captured.append  # type: ignore[method-assign]

        c._handle_quotes(PolymarketQuotes(
            market=market_id,
            price_changes=[
                PolymarketQuote("0xYES", "0.45", PolymarketOrderSide.BUY, "10", "h1"),
                PolymarketQuote("0xNO", "0.53", PolymarketOrderSide.BUY, "8", "h2"),
            ],
            timestamp="1000",
        ))

        assert len(captured) == 1
        assert isinstance(captured[0], CustomData)
        assert isinstance(captured[0].data, OrderBookFrameDeltas)
        assert captured[0].data.markets[0].instrument_ids == (yes.id, no.id)
    finally:
        loop.close()


def test_market_snapshots_wait_for_all_members_before_publish() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        yes = _instrument(token_id="0xYES", outcome="YES")
        no = _instrument(token_id="0xNO", outcome="NO")
        market_id = "0xCONDITION"
        data_type = market_order_book_data_type(Venue("POLYMARKET"), market_id)
        c._add_subscription(data_type)
        c._market_order_book_members[market_id] = (yes.id, no.id)
        c._cache.add_instrument(yes)
        c._cache.add_instrument(no)
        captured = []
        c._handle_data = captured.append  # type: ignore[method-assign]

        c._handle_deltas(yes, _deltas(yes))
        assert captured == []

        c._handle_deltas(no, _deltas(no))
        assert len(captured) == 1
        assert captured[0].data.markets[0].instrument_ids == (yes.id, no.id)
        assert market_id in c._market_books_bootstrapped

        c._handle_deltas(yes, _deltas(yes))
        assert len(captured) == 1
        first = captured[0].data
        c._msgbus.publish(
            topic=order_book_frame_processed_topic(first.venue),
            msg=OrderBookFrameProcessed(first.venue, first.source_market_id, first.frame_id, True),
        )
        assert len(captured) == 2
        assert captured[1].data.markets[0].instrument_ids == (yes.id,)
    finally:
        loop.close()


def test_market_bootstrap_uses_latest_local_book_after_interleaved_quote() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        yes = _instrument(token_id="0xYES", outcome="YES")
        no = _instrument(token_id="0xNO", outcome="NO")
        for instrument in (yes, no):
            c._cache.add_instrument(instrument)
        market_id = "0xCONDITION"
        data_type = market_order_book_data_type(Venue("POLYMARKET"), market_id)
        c._add_subscription(data_type)
        c._market_order_book_members[market_id] = (yes.id, no.id)
        captured = []
        c._handle_data = captured.append  # type: ignore[method-assign]

        c._handle_deltas(yes, _deltas(yes))
        c._handle_quotes(PolymarketQuotes(
            market=market_id,
            price_changes=[
                PolymarketQuote("0xYES", "0.60", PolymarketOrderSide.BUY, "10", "h1"),
            ],
            timestamp="1000",
        ))
        c._handle_deltas(no, _deltas(no))

        assert len(captured) == 1
        yes_deltas = next(
            item
            for item in captured[0].data.markets[0].deltas
            if item.instrument_id == yes.id
        )
        book = OrderBook(yes.id, BookType.L2_MBP)
        book.apply_deltas(yes_deltas)
        assert float(book.best_bid_price()) == 0.60
    finally:
        loop.close()


def test_ws_disconnect_clears_only_disconnected_shard_books() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        yes = _instrument(token_id="0xYES", outcome="YES")
        no = _instrument(token_id="0xNO", outcome="NO")
        for instrument in (yes, no):
            c._cache.add_instrument(instrument)
            c._handle_deltas(instrument, _deltas(instrument))

        market_id = "0xCONDITION"
        data_type = market_order_book_data_type(Venue("POLYMARKET"), market_id)
        c._add_subscription(data_type)
        c._market_order_book_members[market_id] = (yes.id, no.id)
        c._market_books_bootstrapped.add(market_id)
        captured = []
        c._handle_data = captured.append  # type: ignore[method-assign]

        c._handle_ws_disconnect(("0xYES",))

        assert c._local_books[yes.id].best_bid_price() is None
        assert float(c._local_books[no.id].best_bid_price()) == 0.5
        assert len(captured) == 1
        assert isinstance(captured[0], CustomData)
        assert captured[0].data.markets[0].instrument_ids == (yes.id,)
        clear = captured[0].data.markets[0].deltas[0].deltas[0]
        assert clear.action == BookAction.CLEAR
    finally:
        loop.close()


def test_ws_disconnect_does_not_clear_during_client_shutdown() -> None:
    loop = asyncio.new_event_loop()
    try:
        c = _client(loop)
        instrument = _instrument()
        c._cache.add_instrument(instrument)
        c._handle_deltas(instrument, _deltas(instrument))
        c._disconnecting = True
        captured = []
        c._handle_data = captured.append  # type: ignore[method-assign]

        c._handle_ws_disconnect(("0xTOKEN",))

        assert float(c._local_books[instrument.id].best_bid_price()) == 0.5
        assert captured == []
    finally:
        loop.close()


def test_update_instruments_continues_after_provider_error(monkeypatch) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        c = _client(loop)
        calls = {"sleep": 0, "init": 0, "send": 0}

        async def fake_sleep(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 3:
                raise asyncio.CancelledError

        class Provider:
            async def initialize(self, reload: bool = False):
                assert reload is True
                calls["init"] += 1
                if calls["init"] == 1:
                    raise RuntimeError("temporary network outage")

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        c._instrument_provider = Provider()  # type: ignore[assignment]
        c._send_all_instruments_to_data_engine = lambda: calls.__setitem__("send", calls["send"] + 1)  # type: ignore[method-assign]

        loop.run_until_complete(c._update_instruments(1))

        assert calls["init"] == 2
        assert calls["send"] == 1
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
