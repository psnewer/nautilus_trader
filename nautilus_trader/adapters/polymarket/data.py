# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
import asyncio
from typing import Any

import msgspec
from py_clob_client_v2 import ClobClient

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_MAX_PRICE
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_MIN_PRICE
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.common.deltas import compute_effective_deltas
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.parsing import update_instrument
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_condition_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketBookLevel
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketBookSnapshot
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuote
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuotes
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketTickSizeChange
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketTrade
from nautilus_trader.adapters.polymarket.websocket.client import PolymarketWebSocketChannel
from nautilus_trader.adapters.polymarket.websocket.client import PolymarketWebSocketClient
from nautilus_trader.adapters.polymarket.websocket.types import MARKET_WS_MESSAGE
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.datetime import millis_to_nanos
from nautilus_trader.data.messages import RequestBars
from nautilus_trader.data.messages import RequestInstrument
from nautilus_trader.data.messages import RequestInstruments
from nautilus_trader.data.messages import RequestQuoteTicks
from nautilus_trader.data.messages import RequestTradeTicks
from nautilus_trader.data.messages import SubscribeBars
from nautilus_trader.data.messages import SubscribeOrderBook
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import SubscribeTradeTicks
from nautilus_trader.data.messages import UnsubscribeBars
from nautilus_trader.data.messages import UnsubscribeOrderBook
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeTradeTicks
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.market_frame import MarketFrameConflater
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.market_order_book import MarketOrderBookDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameProcessed
from nautilus_trader.model.market_order_book import order_book_frame_processed_topic


class PolymarketDataClient(LiveMarketDataClient):
    """
    Provides a data client for Polymarket, a decentralized predication market.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    http_client : py_clob_client_v2.ClobClient
        The Polymarket HTTP client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : PolymarketInstrumentProvider
        The instrument provider.
    config : PolymarketDataClientConfig
        The configuration for the client.
    name : str, optional
        The custom client ID.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: ClobClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: PolymarketInstrumentProvider,
        config: PolymarketDataClientConfig,
        name: str | None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name or POLYMARKET_VENUE.value),
            venue=POLYMARKET_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )

        # Configuration
        self._config = config
        self._log.info(f"{config.signature_type=}", LogColor.BLUE)
        self._log.info(f"{config.funder=}", LogColor.BLUE)
        self._log.info(f"{config.ws_connection_initial_delay_secs=}", LogColor.BLUE)
        self._log.info(f"{config.ws_connection_delay_secs=}", LogColor.BLUE)
        self._log.info(f"{config.ws_max_subscriptions_per_connection=}", LogColor.BLUE)
        self._log.info(f"{config.update_instruments_interval_mins=}", LogColor.BLUE)
        self._log.info(f"{config.compute_effective_deltas=}", LogColor.BLUE)
        self._log.info(f"{config.auto_load_missing_instruments=}", LogColor.BLUE)
        self._log.info(f"{config.auto_load_debounce_ms=}", LogColor.BLUE)

        # HTTP API
        self._http_client = http_client

        # WebSocket API
        self._ws_client: PolymarketWebSocketClient = PolymarketWebSocketClient(
            self._clock,
            base_url=self._config.base_url_ws,
            channel=PolymarketWebSocketChannel.MARKET,
            handler=self._handle_raw_ws_message,
            handler_reconnect=None,
            loop=self._loop,
            handler_disconnect=self._handle_ws_disconnect,
            max_subscriptions_per_connection=self._config.ws_max_subscriptions_per_connection,
            proxy_url=self._config.proxy_url,
        )
        self._decoder_market_msg = msgspec.json.Decoder(MARKET_WS_MESSAGE)

        # Tasks
        self._update_instruments_task: asyncio.Task | None = None
        self._ws_connect_task: asyncio.Task | None = None
        self._auto_load_task: asyncio.Task | None = None
        self._auto_load_tasks: set[asyncio.Task] = set()

        # Hot caches
        self._last_quotes: dict[InstrumentId, QuoteTick] = {}
        self._local_books: dict[InstrumentId, OrderBook] = {}
        self._market_order_book_members: dict[str, tuple[InstrumentId, ...]] = {}
        self._market_snapshot_buffer: dict[str, dict[InstrumentId, OrderBookDeltas]] = {}
        self._market_books_bootstrapped: set[str] = set()
        self._market_frame_conflater = MarketFrameConflater(
            POLYMARKET_VENUE,
            lambda data: self._handle_data(data),
        )
        self._msgbus.subscribe(
            topic=order_book_frame_processed_topic(POLYMARKET_VENUE),
            handler=self._handle_market_frame_processed,
        )
        self._book_deltas_published = 0

        # Auto-load coordination
        self._pending_instrument_loads: dict[InstrumentId, asyncio.Future[None]] = {}
        self._disconnecting: bool = False

    async def _connect(self) -> None:
        self._disconnecting = False

        self._log.info("Initializing instruments...")
        await self._instrument_provider.initialize()
        self._send_all_instruments_to_data_engine()

        if self._config.update_instruments_interval_mins:
            self._update_instruments_task = self.create_task(
                self._update_instruments(self._config.update_instruments_interval_mins),
            )

    async def _disconnect(self) -> None:
        self._disconnecting = True

        if self._update_instruments_task:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None

        if self._ws_connect_task:
            self._ws_connect_task.cancel()
            self._ws_connect_task = None

        # Cancel every spawned flush task, not just the most recent one; a
        # previous iteration may still be awaiting `load_ids_async` and could
        # otherwise reopen WS subscriptions during shutdown.
        self._auto_load_task = None
        for task in list(self._auto_load_tasks):
            task.cancel()
        self._auto_load_tasks.clear()

        for future in self._pending_instrument_loads.values():
            if not future.done():
                future.cancel()
        self._pending_instrument_loads.clear()

        await self._ws_client.disconnect()
        self._cleanup_expired_books()

    def _schedule_delayed_connect(self) -> None:
        if self._ws_connect_task is not None:
            return

        delay_secs = (
            self._config.ws_connection_initial_delay_secs
            if not self._ws_client.is_connected()
            else self._config.ws_connection_delay_secs
        )
        self._ws_connect_task = self.create_task(self._delayed_connect(delay_secs))

    async def _delayed_connect(self, delay_secs: float) -> None:
        self._log.info(f"Delaying websocket connections start for {delay_secs}s...")
        await asyncio.sleep(delay_secs)
        self._ws_connect_task = None
        try:
            await self._ws_client.connect()
        except Exception as e:
            if self._disconnecting or not self._ws_client.has_subscriptions:
                self._log.warning(f"WebSocket connection failed during shutdown/no subscriptions: {e!r}")
                return
            retry_delay_secs = max(float(self._config.ws_connection_initial_delay_secs), 5.0)
            self._log.warning(
                f"WebSocket connection failed: {e!r}; retrying in {retry_delay_secs:g}s",
            )
            self._ws_connect_task = self.create_task(self._delayed_connect(retry_delay_secs))

    def _create_local_book(self, instrument_id: InstrumentId) -> OrderBook:
        local_book = OrderBook(instrument_id, book_type=BookType.L2_MBP)
        self._local_books[instrument_id] = local_book
        return local_book

    def _handle_ws_disconnect(self, token_ids: tuple[str, ...]) -> None:
        """清空断线 WS 分片负责的盘口,避免自动重连期间继续使用旧赔率。"""
        if self._disconnecting:
            return

        disconnected_tokens = set(token_ids)
        instrument_ids = {
            instrument_id
            for instrument_id in self._local_books
            if get_polymarket_token_id(instrument_id) in disconnected_tokens
        }
        for members in self._market_order_book_members.values():
            instrument_ids.update(
                instrument_id
                for instrument_id in members
                if get_polymarket_token_id(instrument_id) in disconnected_tokens
            )
        if not instrument_ids:
            return

        ts = self._clock.timestamp_ns()
        clears = {
            instrument_id: OrderBookDeltas(
                instrument_id,
                [OrderBookDelta.clear(instrument_id, 0, ts, ts)],
            )
            for instrument_id in instrument_ids
        }

        for instrument_id, deltas in clears.items():
            self._create_local_book(instrument_id)
            self._last_quotes.pop(instrument_id, None)
            if instrument_id in self.subscribed_order_book_deltas():
                self._publish_deltas(deltas)

        cleared_markets = 0
        for market_id, members in self._market_order_book_members.items():
            market_clears = tuple(clears[member] for member in members if member in clears)
            if not market_clears:
                continue
            pending = self._market_snapshot_buffer.get(market_id)
            if pending is not None:
                for member in instrument_ids:
                    pending.pop(member, None)
            self._publish_market_deltas(market_id, market_clears, barrier=True)
            cleared_markets += 1

        self._log.warning(
            f"PM market WS disconnected: cleared {len(instrument_ids)} book(s) "
            f"across {cleared_markets} market(s) before reconnect",
        )

    def _cleanup_expired_books(self) -> None:
        now_ns = self._clock.timestamp_ns()
        expired_instruments = []

        for instrument_id in list(self._local_books.keys()):
            instrument = self._cache.instrument(instrument_id)
            if instrument and instrument.expiration_ns < now_ns:
                expired_instruments.append(instrument_id)

        if expired_instruments:
            for instrument_id in expired_instruments:
                self._local_books.pop(instrument_id, None)
                self._last_quotes.pop(instrument_id, None)
            self._log.info(f"Cleaned up {len(expired_instruments)} expired book(s)")

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

    async def _ensure_instrument_loaded(self, instrument_id: InstrumentId) -> bool:
        if self._cache.instrument(instrument_id) is not None:
            return True

        if not self._config.auto_load_missing_instruments:
            self._log.error(
                f"Cannot find instrument for {instrument_id}, "
                "and `auto_load_missing_instruments` is disabled",
            )
            return False

        if self._disconnecting:
            return False

        future = self._pending_instrument_loads.get(instrument_id)
        if future is None:
            future = self._loop.create_future()
            self._pending_instrument_loads[instrument_id] = future

        if self._auto_load_task is None or self._auto_load_task.done():
            task = self.create_task(self._flush_pending_loads())
            if task is not None:
                self._auto_load_tasks.add(task)
                task.add_done_callback(self._auto_load_tasks.discard)
                self._auto_load_task = task

        try:
            await future
        except asyncio.CancelledError:
            return False
        except Exception as e:
            self._log.error(f"Auto-load failed for {instrument_id}: {e}")
            return False

        return self._cache.instrument(instrument_id) is not None

    async def _flush_pending_loads(self) -> None:
        await asyncio.sleep(self._config.auto_load_debounce_ms / 1000)

        pending = self._pending_instrument_loads
        self._pending_instrument_loads = {}
        # Clear the task handle so misses arriving during the async load below
        # can spawn a fresh flush rather than deadlock on the in-flight task.
        self._auto_load_task = None

        if not pending:
            return

        instrument_ids = list(pending.keys())
        self._log.info(
            f"Auto-loading {len(instrument_ids)} missing instrument(s): {instrument_ids}",
            LogColor.BLUE,
        )

        try:
            await self._instrument_provider.load_ids_async(instrument_ids)
        except Exception as e:
            self._log.error(f"Auto-load batch failed: {e}")

            for future in pending.values():
                if not future.done():
                    future.set_exception(e)
            return

        for instrument_id, future in pending.items():
            instrument = self._instrument_provider.find(instrument_id)
            if instrument is not None:
                self._handle_data(instrument)
            if not future.done():
                future.set_result(None)

    async def _update_instruments(self, interval_mins: int) -> None:
        try:
            while True:
                self._log.debug(
                    f"Scheduled task 'update_instruments' to run in {interval_mins} minutes",
                )
                await asyncio.sleep(interval_mins * 60)
                try:
                    await self._instrument_provider.initialize(reload=True)
                    self._send_all_instruments_to_data_engine()
                except Exception as e:
                    self._log.warning(
                        f"PM update_instruments failed: {e!r}; retrying next cycle",
                    )
        except asyncio.CancelledError:
            self._log.debug("Canceled task 'update_instruments'")

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        if command.book_type == BookType.L3_MBO:
            self._log.error(
                "Cannot subscribe to order book deltas: "
                "L3_MBO data is not published by Polymarket. "
                "Valid book types are L1_MBP, L2_MBP",
            )
            return

        if not await self._ensure_instrument_loaded(command.instrument_id):
            return

        if command.instrument_id not in self.subscribed_order_book_deltas():
            return

        if command.instrument_id not in self._local_books:
            self._create_local_book(command.instrument_id)

        token_id = get_polymarket_token_id(command.instrument_id)

        if self._ws_client.is_connected():
            await self._ws_client.subscribe(token_id)
        else:
            self._ws_client.add_subscription(token_id)
            self._schedule_delayed_connect()

    async def _subscribe(self, command) -> None:
        if command.data_type.type is not MarketOrderBookDeltas:
            raise NotImplementedError
        market_id = str((command.data_type.metadata or {}).get("market_id") or "")
        source_market_id = str(command.params.get("source_market_id") or "")
        instrument_ids = tuple(
            value if isinstance(value, InstrumentId) else InstrumentId.from_str(str(value))
            for value in command.params.get("instrument_ids") or ()
        )
        if market_id != source_market_id or not instrument_ids or any(
            get_polymarket_condition_id(instrument_id) != market_id
            for instrument_id in instrument_ids
        ):
            raise ValueError(f"Invalid Polymarket market members for {market_id}")
        for instrument_id in instrument_ids:
            if not await self._ensure_instrument_loaded(instrument_id):
                raise ValueError(f"Cannot load Polymarket instrument {instrument_id}")
            if instrument_id not in self._local_books:
                self._create_local_book(instrument_id)
        self._market_order_book_members[market_id] = instrument_ids
        self._market_frame_conflater.activate(market_id)
        self._market_snapshot_buffer.pop(market_id, None)
        self._market_books_bootstrapped.discard(market_id)
        for instrument_id in instrument_ids:
            token_id = get_polymarket_token_id(instrument_id)
            if self._ws_client.is_connected():
                await self._ws_client.subscribe(token_id)
            else:
                self._ws_client.add_subscription(token_id)
        if not self._ws_client.is_connected():
            self._schedule_delayed_connect()

    async def _unsubscribe(self, command) -> None:
        if command.data_type.type is not MarketOrderBookDeltas:
            raise NotImplementedError
        market_id = str((command.data_type.metadata or {}).get("market_id") or "")
        self._market_frame_conflater.deactivate(market_id)
        instrument_ids = self._market_order_book_members.pop(market_id, ())
        self._market_snapshot_buffer.pop(market_id, None)
        self._market_books_bootstrapped.discard(market_id)
        for instrument_id in instrument_ids:
            if (
                instrument_id in self.subscribed_order_book_deltas()
                or instrument_id in self.subscribed_quote_ticks()
                or any(instrument_id in members for members in self._market_order_book_members.values())
            ):
                continue
            await self._ws_client.unsubscribe(get_polymarket_token_id(instrument_id))

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        if not await self._ensure_instrument_loaded(command.instrument_id):
            return

        if command.instrument_id not in self.subscribed_quote_ticks():
            return

        if command.instrument_id not in self._local_books:
            self._create_local_book(command.instrument_id)

        token_id = get_polymarket_token_id(command.instrument_id)

        if self._ws_client.is_connected():
            await self._ws_client.subscribe(token_id)
        else:
            self._ws_client.add_subscription(token_id)
            self._schedule_delayed_connect()

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        if not await self._ensure_instrument_loaded(command.instrument_id):
            return

        if command.instrument_id not in self.subscribed_trade_ticks():
            return

        token_id = get_polymarket_token_id(command.instrument_id)

        if self._ws_client.is_connected():
            await self._ws_client.subscribe(token_id)
        else:
            self._ws_client.add_subscription(token_id)
            self._schedule_delayed_connect()

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        self._log.error(
            f"Cannot subscribe to {command.bar_type} bars: not implemented for Polymarket",
        )

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        token_id = get_polymarket_token_id(command.instrument_id)
        await self._ws_client.unsubscribe(token_id)

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        token_id = get_polymarket_token_id(command.instrument_id)
        await self._ws_client.unsubscribe(token_id)

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        token_id = get_polymarket_token_id(command.instrument_id)
        await self._ws_client.unsubscribe(token_id)

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        self._log.error(
            f"Cannot unsubscribe from {command.bar_type} bars: not implemented for Polymarket",
        )

    async def _request_instrument(self, request: RequestInstrument) -> None:
        if request.start is not None:
            self._log.warning(
                f"Requesting instrument {request.instrument_id} with specified `start` which has no effect",
            )

        if request.end is not None:
            self._log.warning(
                f"Requesting instrument {request.instrument_id} with specified `end` which has no effect",
            )

        instrument: BinaryOption | None = self._instrument_provider.find(request.instrument_id)

        if (
            instrument is None
            and self._config.auto_load_missing_instruments
            and await self._ensure_instrument_loaded(request.instrument_id)
        ):
            instrument = self._instrument_provider.find(request.instrument_id)

        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return

        self._handle_instrument(instrument, request.id, request.start, request.end, request.params)

    async def _request_instruments(self, request: RequestInstruments) -> None:
        if request.start is not None:
            self._log.warning(
                f"Requesting instruments for {request.venue} with specified `start` which has no effect",
            )

        if request.end is not None:
            self._log.warning(
                f"Requesting instruments for {request.venue} with specified `end` which has no effect",
            )

        all_instruments = self._instrument_provider.get_all()
        target_instruments = []

        for instrument in all_instruments.values():
            if instrument.venue == request.venue:
                target_instruments.append(instrument)

        self._handle_instruments(
            request.venue,
            target_instruments,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_quote_ticks(self, request: RequestQuoteTicks) -> None:
        self._log.error("Cannot request historical quotes: not published by Polymarket")

    async def _request_trade_ticks(self, request: RequestTradeTicks) -> None:
        self._log.error("Cannot request historical trades: not published by Polymarket")

    async def _request_bars(self, request: RequestBars) -> None:
        self._log.error("Cannot request historical bars: not published by Polymarket")

    def _handle_raw_ws_message(self, raw: bytes) -> None:
        # Uncomment for development
        # self._log.info(str(raw), LogColor.MAGENTA)
        try:
            msg = self._decoder_market_msg.decode(raw)

            if isinstance(msg, list):
                for item in msg:
                    self._handle_ws_message(item)
            else:
                self._handle_ws_message(msg)
        except Exception as e:
            self._log.exception(f"Failed to parse websocket message: {raw.decode()} with error", e)

    def _handle_ws_message(self, msg: Any) -> None:
        if isinstance(msg, PolymarketQuotes):
            self._handle_quotes(ws_message=msg)
        elif isinstance(msg, PolymarketBookSnapshot):
            instrument_id = get_polymarket_instrument_id(msg.market, msg.asset_id)
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                self._log.error(f"Cannot find instrument for {instrument_id}")
                return
            self._handle_book_snapshot(instrument=instrument, ws_message=msg)
        elif isinstance(msg, PolymarketTrade):
            instrument_id = get_polymarket_instrument_id(msg.market, msg.asset_id)
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                self._log.error(f"Cannot find instrument for {instrument_id}")
                return
            self._handle_trade(instrument=instrument, ws_message=msg)
        elif isinstance(msg, PolymarketTickSizeChange):
            instrument_id = get_polymarket_instrument_id(msg.market, msg.asset_id)
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                self._log.error(f"Cannot find instrument for {instrument_id}")
                return
            self._handle_instrument_update(instrument=instrument, ws_message=msg)
        else:
            self._log.error(f"Unknown websocket message topic: {msg}")

    def _handle_book_snapshot(
        self,
        instrument: BinaryOption,
        ws_message: PolymarketBookSnapshot,
    ) -> None:
        now_ns = self._clock.timestamp_ns()
        deltas = ws_message.parse_to_snapshot(instrument=instrument, ts_init=now_ns)

        if deltas is None:
            # Skip empty snapshots (can occur near market resolution)
            return

        self._handle_deltas(instrument, deltas)

        if instrument.id in self.subscribed_quote_ticks():
            quote = ws_message.parse_to_quote(
                instrument=instrument,
                ts_init=now_ns,
                drop_quotes_missing_side=self._config.drop_quotes_missing_side,
            )

            if quote is None:
                self._log.warning(
                    f"Dropping QuoteTick for {instrument.id}: missing bid or ask prices in snapshot",
                )
                return
            self._last_quotes[instrument.id] = quote
            self._handle_data(quote)

    def _handle_deltas(self, instrument: BinaryOption, deltas: OrderBookDeltas) -> None:
        # Always maintain local book for quote generation
        book_old = self._local_books.get(instrument.id)
        book_new = OrderBook(instrument.id, book_type=BookType.L2_MBP)
        book_new.apply_deltas(deltas)
        self._local_books[instrument.id] = book_new

        if self._config.compute_effective_deltas and book_old is not None:
            # Compute effective deltas (reduce snapshot based on old and new book states),
            # prioritizing a smaller data footprint over computational efficiency.
            t0 = self._clock.timestamp_ns()
            deltas = compute_effective_deltas(book_old, book_new, instrument)

            interval_ms = (self._clock.timestamp_ns() - t0) / 1_000_000
            self._log.debug(f"Computed effective deltas in {interval_ms:.3f}ms")
            # self._log.warning(book_new.pprint())  # Uncomment for development

        # Check if any effective deltas remain
        if deltas:
            if instrument.id in self.subscribed_order_book_deltas():
                self._publish_deltas(deltas)
            self._buffer_market_snapshot(instrument.id, deltas)

    def _buffer_market_snapshot(self, instrument_id: InstrumentId, deltas: OrderBookDeltas) -> None:
        market_id = get_polymarket_condition_id(instrument_id)
        members = self._market_order_book_members.get(market_id)
        if members is None:
            return
        if market_id in self._market_books_bootstrapped:
            self._publish_market_deltas(market_id, (deltas,))
            return
        pending = self._market_snapshot_buffer.setdefault(market_id, {})
        pending[instrument_id] = deltas
        if not all(member in pending for member in members):
            return
        now_ns = self._clock.timestamp_ns()
        # 某成员 snapshot 到齐前可能已收到 price_change；首批必须从当前 local book
        # 重建，不能把先到成员的旧 snapshot 回放进 DataEngine。
        complete = tuple(
            self._local_books[member].to_deltas_c(now_ns, now_ns)
            for member in members
        )
        pending.clear()
        self._market_books_bootstrapped.add(market_id)
        self._publish_market_deltas(market_id, complete)

    def _publish_market_deltas(
        self,
        market_id: str,
        deltas: tuple[OrderBookDeltas, ...],
        *,
        barrier: bool = False,
    ) -> None:
        if not deltas or market_id not in self._market_order_book_members:
            return
        now_ns = self._clock.timestamp_ns()
        if not barrier:
            # PM price_change 是档位增量；single-flight 覆盖必须使用已吸收全部
            # WS 变化的 local book 当前完整状态，不能只保留最后一条增量。
            deltas = tuple(
                self._local_books[item.instrument_id].to_deltas_c(item.ts_event, now_ns)
                for item in deltas
                if item.instrument_id in self._local_books
            )
            if not deltas:
                return
        batch = MarketOrderBookDeltas(
            venue=self.venue,
            market_id=market_id,
            deltas=deltas,
            ts_event=max(item.ts_event for item in deltas),
            ts_init=now_ns,
        )
        frame = OrderBookFrameDeltas(
            venue=self.venue,
            source_market_id=market_id,
            markets=(batch,),
            ts_event=batch.ts_event,
            ts_init=now_ns,
        )
        self._market_frame_conflater.offer(frame, barrier=barrier)

    def _handle_market_frame_processed(self, processed: OrderBookFrameProcessed) -> None:
        self._market_frame_conflater.on_processed(processed)

    def _publish_deltas(self, deltas: OrderBookDeltas) -> None:
        self._book_deltas_published += 1
        if self._book_deltas_published == 1:
            self._log.info(
                f"PM OrderBookDeltas published: instrument_id={deltas.instrument_id}, "
                f"deltas={len(deltas.deltas)}",
            )
        self._handle_data(deltas)

    def _handle_quotes(
        self,
        ws_message: PolymarketQuotes,
    ) -> None:
        grouped: dict[InstrumentId, list[OrderBookDelta]] = {}
        for price_change in ws_message.price_changes:
            instrument_id = get_polymarket_instrument_id(ws_message.market, price_change.asset_id)
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                self._log.error(f"Cannot find instrument for {instrument_id}")
                continue

            deltas = self._handle_quote(
                instrument=instrument,
                ws_message=ws_message,
                price_change=price_change,
            )
            if deltas is not None:
                grouped.setdefault(instrument.id, []).extend(deltas.deltas)
        market_id = str(ws_message.market)
        if (
            grouped
            and market_id in self._market_books_bootstrapped
        ):
            self._publish_market_deltas(
                market_id,
                tuple(
                    OrderBookDeltas(instrument_id, deltas)
                    for instrument_id, deltas in grouped.items()
                ),
            )

    def _handle_quote(
        self,
        instrument: BinaryOption,
        ws_message: PolymarketQuotes,
        price_change: PolymarketQuote,
    ) -> OrderBookDeltas | None:
        now_ns = self._clock.timestamp_ns()

        order = BookOrder(
            side=OrderSide.BUY if price_change.side == PolymarketOrderSide.BUY else OrderSide.SELL,
            price=instrument.make_price(float(price_change.price)),
            size=instrument.make_qty(float(price_change.size)),
            order_id=0,
        )
        delta = OrderBookDelta(
            instrument_id=instrument.id,
            action=BookAction.UPDATE if order.size > 0 else BookAction.DELETE,
            order=order,
            flags=RecordFlag.F_LAST,
            sequence=0,
            ts_event=millis_to_nanos(float(ws_message.timestamp)),
            ts_init=now_ns,
        )
        deltas = OrderBookDeltas(instrument.id, [delta])

        # Check if local book exists, create if needed
        if instrument.id not in self._local_books:
            # Skip this quote if we're not subscribed to anything for this instrument
            if (
                instrument.id not in self.subscribed_quote_ticks()
                and instrument.id not in self.subscribed_order_book_deltas()
                and not any(
                    instrument.id in members
                    for members in self._market_order_book_members.values()
                )
            ):
                return None
            self._create_local_book(instrument.id)

        local_book = self._local_books[instrument.id]
        local_book.apply(deltas)

        if instrument.id in self.subscribed_order_book_deltas():
            self._publish_deltas(deltas)

        if instrument.id in self.subscribed_quote_ticks():
            bid_price = local_book.best_bid_price()
            ask_price = local_book.best_ask_price()
            bid_size = local_book.best_bid_size()
            ask_size = local_book.best_ask_size()

            # Handle missing bid/ask prices (can occur near market resolution)
            if bid_price is None or ask_price is None:
                if self._config.drop_quotes_missing_side:
                    self._log.warning(
                        f"Dropping QuoteTick for {instrument.id}: "
                        f"bid_price={bid_price}, ask_price={ask_price}",
                    )
                    return deltas
                else:
                    # Use boundary prices with zero volume for missing sides
                    # POLYMARKET_MIN_PRICE = 0.001, POLYMARKET_MAX_PRICE = 0.999
                    if bid_price is None:
                        bid_price = instrument.make_price(POLYMARKET_MIN_PRICE)
                        bid_size = instrument.make_qty(0.0)
                    if ask_price is None:
                        ask_price = instrument.make_price(POLYMARKET_MAX_PRICE)
                        ask_size = instrument.make_qty(0.0)

            quote = QuoteTick(
                instrument_id=instrument.id,
                bid_price=bid_price,
                ask_price=ask_price,
                bid_size=bid_size,
                ask_size=ask_size,
                ts_event=millis_to_nanos(float(ws_message.timestamp)),
                ts_init=self._clock.timestamp_ns(),
            )

            last_quote = self._last_quotes.get(instrument.id)

            if last_quote is not None and (
                quote.bid_price == last_quote.bid_price
                and quote.ask_price == last_quote.ask_price
                and quote.bid_size == last_quote.bid_size
                and quote.ask_size == last_quote.ask_size
            ):
                return deltas  # No top-of-book change

            self._last_quotes[instrument.id] = quote
            self._handle_data(quote)
        return deltas

    def _handle_trade(
        self,
        instrument: BinaryOption,
        ws_message: PolymarketTrade,
    ) -> None:
        now_ns = self._clock.timestamp_ns()
        trade = ws_message.parse_to_trade_tick(instrument=instrument, ts_init=now_ns)
        self._handle_data(trade)

    def _handle_instrument_update(
        self,
        instrument: BinaryOption,
        ws_message: PolymarketTickSizeChange,
    ) -> None:
        now_ns = self._clock.timestamp_ns()

        old_book = self._local_books.get(instrument.id)
        old_quote = self._last_quotes.get(instrument.id)

        instrument = update_instrument(instrument, change=ws_message, ts_init=now_ns)

        # Update local sources immediately so subsequent quotes use the correct precision
        self._instrument_provider.add(instrument)
        self._cache.add_instrument(instrument)

        self._log.debug(
            f"Instrument tick size changed: instrument_id={instrument.id}, "
            f"old_tick_size={ws_message.old_tick_size}, new_tick_size={ws_message.new_tick_size}",
        )
        self._handle_data(instrument)

        if old_book is not None:
            self._reset_local_book_after_tick_size_change(
                instrument=instrument,
                change=ws_message,
                old_book=old_book,
                old_quote=old_quote,
                ts_init=now_ns,
            )

    def _reset_local_book_after_tick_size_change(
        self,
        instrument: BinaryOption,
        change: PolymarketTickSizeChange,
        old_book: OrderBook,
        old_quote: QuoteTick | None,
        ts_init: int,
    ) -> None:
        snapshot = self._build_snapshot_from_book(
            instrument=instrument,
            change=change,
            book=old_book,
        )

        deltas = snapshot.parse_to_snapshot(instrument=instrument, ts_init=ts_init)

        if deltas is None:
            self._local_books.pop(instrument.id, None)
            self._last_quotes.pop(instrument.id, None)
            return

        new_book = OrderBook(instrument.id, book_type=BookType.L2_MBP)
        new_book.apply_deltas(deltas)
        self._local_books[instrument.id] = new_book

        output = (
            compute_effective_deltas(old_book, new_book, instrument)
            if self._config.compute_effective_deltas
            else deltas
        )
        if output:
            if instrument.id in self.subscribed_order_book_deltas():
                self._publish_deltas(output)
            self._buffer_market_snapshot(instrument.id, output)

        if instrument.id in self.subscribed_quote_ticks():
            quote = snapshot.parse_to_quote(
                instrument=instrument,
                ts_init=ts_init,
                drop_quotes_missing_side=self._config.drop_quotes_missing_side,
            )

            if quote is not None:
                self._last_quotes[instrument.id] = quote
                self._handle_data(quote)
            elif old_quote is None:
                self._last_quotes.pop(instrument.id, None)

    def _build_snapshot_from_book(
        self,
        instrument: BinaryOption,
        change: PolymarketTickSizeChange,
        book: OrderBook,
    ) -> PolymarketBookSnapshot:
        bids_levels = [
            PolymarketBookLevel(
                price=str(instrument.make_price(float(level.price))),
                size=str(instrument.make_qty(level.size())),
            )
            for level in reversed(book.bids())
        ]

        asks_levels = [
            PolymarketBookLevel(
                price=str(instrument.make_price(float(level.price))),
                size=str(instrument.make_qty(level.size())),
            )
            for level in reversed(book.asks())
        ]

        return PolymarketBookSnapshot(
            market=change.market,
            asset_id=change.asset_id,
            bids=bids_levels,
            asks=asks_levels,
            timestamp=change.timestamp,
        )
