# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

"""OrbitExch WebSocket handler."""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional


class OrbitExchWebSocketHandler:
    """
    Handles WebSocket connections for OrbitExch.
    
    Intercepts and parses WebSocket messages for:
    - Market prices (赔率数据)
    - Order updates (订单数据)
    
    Parameters
    ----------
    page : Page
        Playwright page instance
    """
    
    def __init__(self, page):
        self.page = page
        self._log = logging.getLogger(self.__class__.__name__)
        
        # Callbacks
        self._price_callbacks: List[Callable] = []
        self._order_callbacks: List[Callable] = []
        
        # WebSocket tracking
        self._websockets: Dict[str, Any] = {}
        self._running = False
    
    async def start(self) -> None:
        """Start listening to WebSocket messages."""
        self._log.info('🔌 Starting WebSocket listener...')
        self._running = True
        
        # Listen for WebSocket connections
        self.page.on('websocket', self._on_websocket)
        
        self._log.info('✅ WebSocket listener started')
    
    async def stop(self) -> None:
        """Stop listening to WebSocket messages."""
        self._log.info('🛑 Stopping WebSocket listener...')
        self._running = False
        
        # Remove listener
        self.page.remove_listener('websocket', self._on_websocket)
        
        self._log.info('✅ WebSocket listener stopped')
    
    def _on_websocket(self, ws) -> None:
        """Handle new WebSocket connection."""
        url = ws.url
        self._log.info(f'🔌 WebSocket connected: {url}')
        
        # Determine WebSocket type
        if 'multiple-market-prices' in url:
            ws_type = 'prices'
            self._log.info('   📊 Type: Market Prices')
        elif 'general' in url:
            ws_type = 'orders'
            self._log.info('   📋 Type: Orders')
        else:
            ws_type = 'unknown'
            self._log.info(f'   ❓ Type: Unknown')
        
        # Store WebSocket
        self._websockets[url] = {
            'ws': ws,
            'type': ws_type,
            'url': url,
        }
        
        # Listen for messages - Playwright passes data directly as string
        ws.on('framereceived', lambda data: self._on_frame_received(ws_type, data))
        ws.on('framesent', lambda data: self._on_frame_sent(ws_type, data))
        ws.on('close', lambda: self._on_websocket_close(url))
    
    def _on_frame_received(self, ws_type: str, data: str) -> None:
        """Handle received WebSocket frame."""
        try:
            # Skip empty data
            if not data:
                return
            
            # Skip connection/heartbeat messages
            if data in ['o', 'h'] or data.startswith('['):
                return
            
            # Parse JSON array format: a["..."]
            if data.startswith('a['):
                # Extract JSON from array format
                json_str = data[2:-1]  # Remove 'a[' and ']'
                if json_str.startswith('"') and json_str.endswith('"'):
                    json_str = json_str[1:-1]  # Remove quotes
                    # Unescape
                    json_str = json_str.replace('\\\\', '\\').replace('\\"', '"')
                
                try:
                    message = json.loads(json_str)
                    
                    if ws_type == 'prices':
                        self._handle_price_update(message)
                    elif ws_type == 'orders':
                        self._handle_order_update(message)
                    
                except json.JSONDecodeError as e:
                    self._log.debug(f'JSON decode error: {e}, data: {data[:100]}')
        
        except Exception as e:
            self._log.debug(f'Frame parsing error: {e}')
    
    def _on_frame_sent(self, ws_type: str, data: str) -> None:
        """Handle sent WebSocket frame."""
        # Log sent frames for debugging
        if data and not data.startswith('['):  # Skip heartbeats
            self._log.debug(f'📤 Sent ({ws_type}): {data[:100]}')
    
    def _on_websocket_close(self, url: str) -> None:
        """Handle WebSocket close."""
        self._log.info(f'🔌 WebSocket closed: {url}')
        if url in self._websockets:
            del self._websockets[url]
    
    def _handle_price_update(self, message: Any) -> None:
        """Handle market price update."""
        self._log.debug(f'📊 Price update: {str(message)[:200]}')
        
        # Call registered callbacks
        for callback in self._price_callbacks:
            try:
                callback(message)
            except Exception as e:
                self._log.error(f'Price callback error: {e}')
    
    def _handle_order_update(self, message: Any) -> None:
        """Handle order update."""
        self._log.debug(f'📋 Order update: {str(message)[:200]}')
        
        # Call registered callbacks
        for callback in self._order_callbacks:
            try:
                callback(message)
            except Exception as e:
                self._log.error(f'Order callback error: {e}')
    
    def on_price_update(self, callback: Callable) -> None:
        """
        Register callback for price updates.
        
        Parameters
        ----------
        callback : callable
            Function to call with price data
        """
        self._price_callbacks.append(callback)
        self._log.info(f'✅ Registered price callback: {callback.__name__}')
    
    def on_order_update(self, callback: Callable) -> None:
        """
        Register callback for order updates.
        
        Parameters
        ----------
        callback : callable
            Function to call with order data
        """
        self._order_callbacks.append(callback)
        self._log.info(f'✅ Registered order callback: {callback.__name__}')
    
    def get_active_websockets(self) -> List[Dict[str, str]]:
        """
        Get list of active WebSocket connections.
        
        Returns
        -------
        List[Dict]
            List of WebSocket info
        """
        return [
            {
                'url': ws_info['url'],
                'type': ws_info['type'],
            }
            for ws_info in self._websockets.values()
        ]
