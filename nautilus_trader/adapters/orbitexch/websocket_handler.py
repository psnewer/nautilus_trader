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
    
    def __init__(self, page, logger: Any | None = None):
        self.page = page
        self._log = logger
        
        # Callbacks
        self._price_callbacks: List[Callable] = []
        self._order_callbacks: List[Callable] = []
        
        # WebSocket tracking
        self._websockets: Dict[str, Any] = {}
        self._frame_counts: Dict[str, int] = {}
        self._running = False
    
    async def start(self) -> None:
        """Start listening to WebSocket messages."""
        self._log_info('OE WS listener starting')
        self._running = True
        
        # Listen for WebSocket connections
        self.page.on('websocket', self._on_websocket)
        
        self._log_info('OE WS listener started')
    
    async def stop(self) -> None:
        """Stop listening to WebSocket messages."""
        self._log_info('OE WS listener stopping')
        self._running = False
        
        # Remove listener
        self.page.remove_listener('websocket', self._on_websocket)
        
        self._log_info('OE WS listener stopped')
    
    def _on_websocket(self, ws) -> None:
        """Handle new WebSocket connection."""
        url = ws.url

        # Determine WebSocket type
        if 'multiple-market-prices' in url:
            ws_type = 'prices'
        elif 'general' in url:
            ws_type = 'orders'
        else:
            ws_type = 'unknown'
        self._log_info(f"OE WS connected: type={ws_type}, url={url}")
        
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

            self._frame_counts[ws_type] = self._frame_counts.get(ws_type, 0) + 1
            if self._frame_counts[ws_type] == 1:
                self._log_info(
                    "OE WS first frame received: "
                    f"type={ws_type}, kind={self._frame_kind(data)}, bytes={len(data)}",
                )
            
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
                    self._log_debug(f"OE WS JSON decode error: {e}, data={data[:100]}")
        
        except Exception as e:
            self._log_debug(f"OE WS frame parsing error: {e}")
    
    def _on_frame_sent(self, ws_type: str, data: str) -> None:
        """Handle sent WebSocket frame."""
        # Log sent frames for debugging
        if data and not data.startswith('['):  # Skip heartbeats
            self._log_debug(f"OE WS sent frame: type={ws_type}, data={data[:100]}")
    
    def _on_websocket_close(self, url: str) -> None:
        """Handle WebSocket close."""
        ws_info = self._websockets.get(url)
        ws_type = ws_info["type"] if ws_info is not None else "unknown"
        self._log_info(f"OE WS closed: type={ws_type}, url={url}")
        if url in self._websockets:
            del self._websockets[url]
    
    def _handle_price_update(self, message: Any) -> None:
        """Handle market price update."""
        self._log_debug(f"OE WS price message parsed: {str(message)[:200]}")
        
        # Call registered callbacks
        for callback in self._price_callbacks:
            try:
                callback(message)
            except Exception as e:
                self._log_error(f"OE WS price callback error: {e}")
    
    def _handle_order_update(self, message: Any) -> None:
        """Handle order update."""
        self._log_debug(f"OE WS order message parsed: {str(message)[:200]}")
        
        # Call registered callbacks
        for callback in self._order_callbacks:
            try:
                callback(message)
            except Exception as e:
                self._log_error(f"OE WS order callback error: {e}")
    
    def on_price_update(self, callback: Callable) -> None:
        """
        Register callback for price updates.
        
        Parameters
        ----------
        callback : callable
            Function to call with price data
        """
        self._price_callbacks.append(callback)
        self._log_info(f"OE WS registered price callback: {callback.__name__}")
    
    def on_order_update(self, callback: Callable) -> None:
        """
        Register callback for order updates.
        
        Parameters
        ----------
        callback : callable
            Function to call with order data
        """
        self._order_callbacks.append(callback)
        self._log_info(f"OE WS registered order callback: {callback.__name__}")
    
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

    @staticmethod
    def _frame_kind(data: str) -> str:
        if data == "o":
            return "sockjs_open"
        if data == "h":
            return "sockjs_heartbeat"
        if data.startswith("a["):
            return "sockjs_message"
        if data.startswith("["):
            return "client_message"
        return "other"

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)

    def _log_debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(message)

    def _log_error(self, message: str) -> None:
        if self._log is not None:
            self._log.error(message)
