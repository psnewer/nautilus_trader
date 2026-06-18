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

from nautilus_trader.core.datetime import secs_to_nanos


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
    
    def __init__(
        self,
        page,
        logger: Any | None = None,
        *,
        clock: Any | None = None,
        liveness_timeout_secs: float | None = None,
        liveness_name: str | None = None,
    ):
        self.page = page
        self._log = logger

        # Callbacks
        self._price_callbacks: List[Callable] = []
        self._order_callbacks: List[Callable] = []
        self._frame_callbacks: List[Callable] = []        # #105:每帧(含 SockJS 心跳)→ 外部存活锚(exec 用)
        self._disconnect_callbacks: List[Callable] = []   # #109:close 或心跳超时 → 宿主 reload(对称 PM disconnect)

        # WebSocket tracking
        self._websockets: Dict[str, Any] = {}
        self._frame_counts: Dict[str, int] = {}
        self._running = False

        # #109:内部存活封装(传 clock+loop+timeout 才开;执行页 general WS 不传 → 行为不变)。
        # 被动盯入向帧(含心跳):每帧更新 `_last_frame_ns`;lazy self-rescheduling NT clock alert 到期读它,
        # 心跳停=静默死亡 → fire on_disconnect。等价 PM pyo3 client 内部 ping-timeout(只是被动而非主动 ping)。
        self._clock = clock
        self._liveness_timeout_secs = liveness_timeout_secs
        self._liveness_name = liveness_name or f"oe_ws_liveness:{id(self)}"
        self._liveness_enabled = clock is not None and liveness_timeout_secs is not None
        self._last_frame_ns = 0

    async def start(self) -> None:
        """Start listening to WebSocket messages."""
        self._log_info('OE WS listener starting')
        self._running = True

        # Listen for WebSocket connections
        self.page.on('websocket', self._on_websocket)

        if self._liveness_enabled:
            self._last_frame_ns = self._clock.timestamp_ns()  # 起始宽限:刚启动不算 stale
            self._schedule_liveness()

        self._log_info('OE WS listener started')

    async def stop(self) -> None:
        """Stop listening to WebSocket messages."""
        self._log_info('OE WS listener stopping')
        self._running = False

        # Remove listener
        self.page.remove_listener('websocket', self._on_websocket)

        if self._liveness_enabled:
            try:
                self._clock.cancel_timer(self._liveness_name)
            except (KeyError, ValueError):
                pass

        self._log_info('OE WS listener stopped')

    # ── #109:内部存活(被动心跳超时 → on_disconnect)──────────────────────
    def on_disconnect(self, callback: Callable) -> None:
        """注册"连接断开"回调(收 `reason`:`"close"` / `"liveness_timeout"`),供宿主 reload。
        对称 PM `_schedule_delayed_connect`:宿主只收事件,不自己监控。"""
        self._disconnect_callbacks.append(callback)

    def _fire_disconnect(self, reason: str) -> None:
        for cb in self._disconnect_callbacks:
            try:
                cb(reason)
            except Exception as e:  # noqa: BLE001 — disconnect 回调不得影响其它监听
                self._log_debug(f"OE WS disconnect callback error: {e}")

    def _schedule_liveness(self) -> None:
        self._clock.set_time_alert_ns(
            name=self._liveness_name,
            alert_time_ns=self._last_frame_ns + secs_to_nanos(self._liveness_timeout_secs),
            callback=self._on_liveness_alert,
        )

    def _on_liveness_alert(self, event) -> None:
        if not self._running:
            return
        now = self._clock.timestamp_ns()
        timeout_ns = secs_to_nanos(self._liveness_timeout_secs)
        if (now - self._last_frame_ns) >= timeout_ns:
            self._log_info(f"OE WS liveness timeout (no frame {self._liveness_timeout_secs}s, 含心跳) → disconnect")
            self._fire_disconnect("liveness_timeout")
            next_at = now + timeout_ns                    # 死:整 timeout 后再查(宿主已去重 reload),避免过去时间紧循环
        else:
            next_at = self._last_frame_ns + timeout_ns    # 活:重排到将 stale 的未来点
        self._clock.set_time_alert_ns(name=self._liveness_name, alert_time_ns=next_at, callback=self._on_liveness_alert)
    
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

            # #105/#109:任一非空帧(含心跳 'h')→ 刷存活(在心跳/业务分型前)。
            # 内部存活锚(#109,liveness 开时)+ 外部 on_frame 回调(exec 用)。
            if self._liveness_enabled:
                self._last_frame_ns = self._clock.timestamp_ns()
            for cb in self._frame_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — 存活回调不得影响帧处理
                    self._log_debug(f"OE WS frame callback error: {e}")

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
        # #109:WS close → fire on_disconnect(reason 带 ws_type,宿主按 feed 决定是否 reload)。
        # 干净关闭的快路;静默死亡(无 close 帧)由内部 liveness 心跳超时兜底。
        self._fire_disconnect(f"close:{ws_type}")
    
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

    def on_frame(self, callback: Callable) -> None:
        """#105:注册"每收到一帧(含 SockJS 心跳 `'h'`)"回调,供 **ExecClient** 刷存活锚 `_last_frame_ns`。
        callback 无参(只表示"有帧到达"),在 empty 检查后、心跳/业务分型前触发。
        注:competition 页用 `on_disconnect`(内部 liveness 封装),不用这个外部锚。"""
        self._frame_callbacks.append(callback)

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
