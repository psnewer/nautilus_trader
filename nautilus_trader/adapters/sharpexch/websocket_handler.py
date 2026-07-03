"""SharpExch WebSocket handler.

只负责 Playwright WebSocket 事件与 SockJS frame 解包;page 生命周期由
DataClient/ExecutionClient 管理。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from nautilus_trader.core.datetime import secs_to_nanos


class SharpExchWebSocketHandler:
    """SE WS frame dispatcher."""

    def __init__(
        self,
        page,
        logger: Any | None = None,
        *,
        clock: Any | None = None,
        liveness_timeout_secs: float | None = None,
        liveness_name: str | None = None,
        liveness_ws_type: str | None = None,
    ):
        self.page = page
        self._log = logger
        self._price_callbacks: list[Callable] = []
        self._order_callbacks: list[Callable] = []
        self._frame_callbacks: list[Callable] = []
        self._disconnect_callbacks: list[Callable] = []
        self._websockets: dict[str, dict] = {}
        self._frame_counts: dict[str, int] = {}
        self._running = False
        self._clock = clock
        self._liveness_timeout_secs = liveness_timeout_secs
        self._liveness_name = liveness_name or f"se_ws_liveness:{id(self)}"
        self._liveness_ws_type = liveness_ws_type
        self._liveness_enabled = clock is not None and liveness_timeout_secs is not None
        self._last_frame_ns = 0

    async def start(self) -> None:
        self._log_info("SE WS listener starting")
        self._running = True
        self.page.on("websocket", self._on_websocket)
        if self._liveness_enabled:
            self._last_frame_ns = self._clock.timestamp_ns()
            self._schedule_liveness()
        self._log_info("SE WS listener started")

    async def stop(self) -> None:
        self._log_info("SE WS listener stopping")
        self._running = False
        self.page.remove_listener("websocket", self._on_websocket)
        if self._liveness_enabled:
            self._cancel_liveness()
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

    def _fire_disconnect(self, reason: str) -> None:
        for callback in self._disconnect_callbacks:
            try:
                callback(reason)
            except Exception as exc:  # noqa: BLE001
                self._log_debug(f"SE WS disconnect callback error: {exc}")

    def _schedule_liveness(self) -> None:
        self._cancel_liveness()
        self._clock.set_time_alert_ns(
            name=self._liveness_name,
            alert_time_ns=self._last_frame_ns + secs_to_nanos(self._liveness_timeout_secs),
            callback=self._on_liveness_alert,
        )

    def _cancel_liveness(self) -> None:
        try:
            self._clock.cancel_timer(self._liveness_name)
        except (KeyError, ValueError):
            pass

    def _on_liveness_alert(self, event) -> None:
        if not self._running:
            return
        now = self._clock.timestamp_ns()
        timeout_ns = secs_to_nanos(self._liveness_timeout_secs)
        if (now - self._last_frame_ns) >= timeout_ns:
            self._log_info(f"SE WS liveness timeout (no frame {self._liveness_timeout_secs}s, 含心跳) → disconnect")
            self._fire_disconnect("liveness_timeout")
            next_at = now + timeout_ns
        else:
            next_at = self._last_frame_ns + timeout_ns
        self._cancel_liveness()
        self._clock.set_time_alert_ns(
            name=self._liveness_name,
            alert_time_ns=next_at,
            callback=self._on_liveness_alert,
        )

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

            if self._liveness_enabled and (
                self._liveness_ws_type is None or ws_type == self._liveness_ws_type
            ):
                self._last_frame_ns = self._clock.timestamp_ns()

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
        self._fire_disconnect(f"close:{ws_type}")

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
