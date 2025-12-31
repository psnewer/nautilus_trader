# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

"""OrbitExch live data client."""

import asyncio
from typing import Optional
from decimal import Decimal

from nautilus_trader.adapters.orbitexch.browser_manager import PlaywrightBrowserManager
from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.scraper import OrbitExchScraper
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler
from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.core.datetime import millis_to_nanos
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class OrbitExchDataClient(LiveDataClient):
    """
    Provides a data client for OrbitExch betting exchange.
    
    Uses WebSocket connections to receive real-time market data.
    """
    
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: PlaywrightBrowserManager,
        msgbus,
        cache: Cache,
        clock: LiveClock,
        config: OrbitExchDataClientConfig,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId('ORBITEXCH'),
            venue=Venue('ORBITEXCH'),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        
        self._config = config
        self._browser_manager = client
        self._scraper: Optional[OrbitExchScraper] = None
        self._ws_handler: Optional[OrbitExchWebSocketHandler] = None
        self._parser = OrbitExchMessageParser()
        self._page = None
        
        # Subscriptions: market_id -> InstrumentId
        self._subscribed_instruments = {}
        
        self._log = Logger(name=type(self).__name__)
    
    # ========== Connection Management ==========
    
    async def _connect(self) -> None:
        """Connect to OrbitExch."""
        self._log.info('Connecting to OrbitExch...')
        
        # Start browser
        await self._browser_manager.start()
        
        # Create page
        self._page = await self._browser_manager.create_page('data_client')
        
        # Create scraper
        self._scraper = OrbitExchScraper(
            page=self._page,
            base_url=self._config.base_url,
        )
        
        # Login
        success = await self._scraper.login(
            username=self._config.username,
            password=self._config.password,
        )
        
        if not success:
            raise RuntimeError('OrbitExch login failed')
        
        # Navigate to in-play page to establish WebSocket
        await self._page.goto(f'{self._config.base_url}/customer/inplay/highlights')
        await asyncio.sleep(2)
        
        # Setup WebSocket handler
        self._ws_handler = OrbitExchWebSocketHandler(self._page)
        self._ws_handler.on_price_update(self._on_price_update)
        self._ws_handler.on_order_update(self._on_order_update)
        await self._ws_handler.start()
        
        self._log.info('✅ Connected to OrbitExch')
        
        # Log active WebSockets
        active_ws = self._ws_handler.get_active_websockets()
        for ws in active_ws:
            self._log.info(f'   WebSocket: {ws["type"]}')
    
    async def _disconnect(self) -> None:
        """Disconnect from OrbitExch."""
        self._log.info('Disconnecting from OrbitExch...')
        
        # Stop WebSocket handler
        if self._ws_handler:
            await self._ws_handler.stop()
        
        # Close browser
        if self._browser_manager:
            await self._browser_manager.close()
        
        self._log.info('✅ Disconnected from OrbitExch')
    
    # ========== WebSocket Callbacks ==========
    
    def _on_price_update(self, message: dict) -> None:
        """Handle price update from WebSocket."""
        # Parse message
        parsed = self._parser.parse_price_message(message)
        if not parsed:
            return
        
        market_id = parsed['market_id']
        
        # Check if we're subscribed to this market
        instrument_id = self._subscribed_instruments.get(market_id)
        if not instrument_id:
            return
        
        # Log price update
        self._log.info(f"📊 Price update: {parsed['event_name']} - {parsed['market_name']}")
        
        # Convert to QuoteTicks for each runner
        for runner in parsed['runners']:
            quote_tick = self._create_quote_tick(
                instrument_id=instrument_id,
                runner=runner,
                timestamp_ms=parsed.get('timestamp'),
            )
            
            if quote_tick:
                # Log the quote
                self._log.info(f"   Selection {runner['selection_id']}: Bid={quote_tick.bid_price} Ask={quote_tick.ask_price}")
                
                # Publish to message bus
                self._handle_data(quote_tick)
    
    def _on_order_update(self, message: dict) -> None:
        """Handle order update from WebSocket."""
        self._log.debug(f'Order update: {message}')
    
    # ========== Data Conversion ==========
    
    def _create_quote_tick(
        self,
        instrument_id: InstrumentId,
        runner: dict,
        timestamp_ms: Optional[int],
    ) -> Optional[QuoteTick]:
        """Create QuoteTick from runner data."""
        try:
            # Get best back and lay prices
            best_back = self._parser.get_best_back_price(runner)
            best_lay = self._parser.get_best_lay_price(runner)
            
            if not best_back or not best_lay:
                return None
            
            # Get sizes
            back_size = runner['back'][0]['size'] if runner['back'] else 0
            lay_size = runner['lay'][0]['size'] if runner['lay'] else 0
            
            # Create QuoteTick
            ts_init = millis_to_nanos(timestamp_ms) if timestamp_ms else self._clock.timestamp_ns()
            
            return QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price(best_back, precision=2),
                ask_price=Price(best_lay, precision=2),
                bid_size=Quantity(back_size, precision=2),
                ask_size=Quantity(lay_size, precision=2),
                ts_event=ts_init,
                ts_init=ts_init,
            )
        
        except Exception as e:
            self._log.error(f'Error creating QuoteTick: {e}')
            return None
    
    # ========== Subscriptions ==========
    
    async def _subscribe(self, data_type) -> None:
        """Subscribe to data."""
        self._log.info(f'Subscribe: {data_type}')
    
    async def _unsubscribe(self, data_type) -> None:
        """Unsubscribe from data."""
        self._log.info(f'Unsubscribe: {data_type}')
    
    # ========== Requests ==========
    
    async def _request(self, data_type, correlation_id):
        """Request data."""
        self._log.info(f'Request: {data_type}')
    
    # ========== Helper Methods ==========
    
    async def subscribe_market(self, market_id: str, selection_id: str) -> None:
        """Subscribe to a specific market/selection."""
        # Create instrument ID
        symbol = Symbol(f'{market_id}_{selection_id}')
        instrument_id = InstrumentId(symbol, self.venue)
        
        # Store subscription
        self._subscribed_instruments[market_id] = instrument_id
        
        self._log.info(f'Subscribed to {instrument_id}')
    
    async def navigate_to_event(self, event_id: str) -> None:
        """Navigate to a specific event page."""
        url = f'{self._config.base_url}/customer/event/{event_id}'
        await self._page.goto(url)
        self._log.info(f'Navigated to event: {event_id}')
