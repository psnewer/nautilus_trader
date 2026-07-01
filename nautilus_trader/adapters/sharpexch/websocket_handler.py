"""SharpExch WebSocket handler.

只负责 Playwright WebSocket 事件与 SockJS frame 解包;真实 page 生命周期由后续
DataClient/ExecutionClient 管理。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class SharpExchWebSocketHandler:
    """SE WS frame dispatcher."""

    def __init__(self, page, logger: Any | None = None):
        self.page = page
        self._log = logger
        self._price_callbacks: list[Callable] = []
        self._order_callbacks: list[Callable] = []
        self._frame_callbacks: list[Callable] = []
        self._disconnect_callbacks: list[Callable] = []
        self._websockets: dict[str, dict] = {}
        self._frame_counts: dict[str, int] = {}
        self._running = False

    async def start(self) -> None:
        self._log_info("SE WS listener starting")
        self._running = True
        self.page.on("websocket", self._on_websocket)
        self._log_info("SE WS listener started")

    async def stop(self) -> None:
        self._log_info("SE WS listener stopping")
        self._running = False
        self.page.remove_listener("websocket", self._on_websocket)
        self._log_info("SE WS listener stopped")

    def on_price_update(self, callback: Callable) -> None:
        self._price_callbacks.append(callback)
        self._log_info(f"SE WS registered price callback: {getattr(callback, '__name__', repr(callback))}")

    def on_order_update(self, callback: Callable) -> None:
        self._order_callbacks.append(callback)
        self._log_info(f"SE WS registered order callback: {getattr(callback, '__name__', repr(callback))}")

    def on_frame(self, callback: Callable) -> None:
        self._frame_callbacks.append(callback)

    def on_disconnect(self, callback: Callable) -> None:
        self._disconnect_callbacks.append(callback)

    def get_active_websockets(self) -> list[dict[str, str]]:
        return [
            {"url": ws_info["url"], "type": ws_info["type"]}
            for ws_info in self._websockets.values()
        ]

    def get_frame_counts(self) -> dict[str, int]:
        return dict(self._frame_counts)

    def _on_websocket(self, ws) -> None:
        url = ws.url
        if "multiple-market-prices" in url:
            ws_type = "prices"
        elif "general" in url:
            ws_type = "orders"
        else:
            ws_type = "unknown"
        self._log_info(f"SE WS connected: type={ws_type}, url={url}")
        self._websockets[url] = {"ws": ws, "type": ws_type, "url": url}
        ws.on("framereceived", lambda data: self._on_frame_received(ws_type, data))
        ws.on("close", lambda: self._on_websocket_close(url))

    def _on_frame_received(self, ws_type: str, data: str) -> None:
        try:
            if not data:
                return

            for callback in self._frame_callbacks:
                try:
                    callback()
                except Exception as exc:  # noqa: BLE001
                    self._log_debug(f"SE WS frame callback error: {exc}")

            self._frame_counts[ws_type] = self._frame_counts.get(ws_type, 0) + 1
            if self._frame_counts[ws_type] == 1:
                self._log_info(
                    "SE WS first frame received: "
                    f"type={ws_type}, kind={self._frame_kind(data)}, bytes={len(data)}",
                )

            if data in ["o", "h"] or data.startswith("["):
                return
            if not data.startswith("a["):
                return

            message = self._parse_sockjs_message(data)
            if message is None:
                return
            if ws_type == "prices":
                self._handle_price_update(message)
            elif ws_type == "orders":
                self._handle_order_update(message)
        except Exception as exc:  # noqa: BLE001
            self._log_debug(f"SE WS frame parsing error: {exc}")

    def _on_websocket_close(self, url: str) -> None:
        ws_info = self._websockets.get(url)
        ws_type = ws_info["type"] if ws_info is not None else "unknown"
        self._log_info(f"SE WS closed: type={ws_type}, url={url}")
        self._websockets.pop(url, None)
        for callback in self._disconnect_callbacks:
            try:
                callback(f"close:{ws_type}")
            except Exception as exc:  # noqa: BLE001
                self._log_debug(f"SE WS disconnect callback error: {exc}")

    def _handle_price_update(self, message: Any) -> None:
        self._log_debug(f"SE WS price message parsed: {str(message)[:200]}")
        for callback in self._price_callbacks:
            try:
                callback(message)
            except Exception as exc:  # noqa: BLE001
                self._log_error(f"SE WS price callback error: {exc}")

    def _handle_order_update(self, message: Any) -> None:
        self._log_debug(f"SE WS order message parsed: {str(message)[:200]}")
        for callback in self._order_callbacks:
            try:
                callback(message)
            except Exception as exc:  # noqa: BLE001
                self._log_error(f"SE WS order callback error: {exc}")

    @staticmethod
    def _parse_sockjs_message(data: str) -> Any | None:
        json_str = data[2:-1]
        if json_str.startswith('"') and json_str.endswith('"'):
            json_str = json_str[1:-1]
            json_str = json_str.replace("\\\\", "\\").replace('\\"', '"')
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

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
