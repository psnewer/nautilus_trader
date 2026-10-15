"""Polymarket WebSocket 连接参数测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nautilus_trader.adapters.polymarket.websocket import client as websocket_module
from nautilus_trader.adapters.polymarket.websocket.client import PolymarketWebSocketChannel
from nautilus_trader.adapters.polymarket.websocket.client import PolymarketWebSocketClient
from nautilus_trader.common.component import LiveClock


def test_user_channel_can_disable_idle_timeout_for_local_paper(monkeypatch):
    async def scenario() -> None:
        captured = {}

        def fake_config(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

        class FakeClient:
            async def send_text(self, _payload):
                return None

        class FakeWebSocketClient:
            @staticmethod
            async def connect(**_kwargs):
                return FakeClient()

        monkeypatch.setattr(websocket_module, "WebSocketConfig", fake_config)
        monkeypatch.setattr(websocket_module, "WebSocketClient", FakeWebSocketClient)
        loop = asyncio.get_running_loop()
        client = PolymarketWebSocketClient(
            clock=LiveClock(),
            base_url="ws://127.0.0.1:8765/ws/",
            channel=PolymarketWebSocketChannel.USER,
            handler=lambda _payload: None,
            handler_reconnect=None,
            loop=loop,
            enable_idle_timeout=False,
        )
        client.add_subscription("condition-1")

        await client.connect()

        assert captured["idle_timeout_ms"] is None

    asyncio.run(scenario())


def test_user_channel_keeps_default_idle_timeout_for_live(monkeypatch):
    async def scenario() -> None:
        captured = {}

        def fake_config(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

        class FakeClient:
            async def send_text(self, _payload):
                return None

        class FakeWebSocketClient:
            @staticmethod
            async def connect(**_kwargs):
                return FakeClient()

        monkeypatch.setattr(websocket_module, "WebSocketConfig", fake_config)
        monkeypatch.setattr(websocket_module, "WebSocketClient", FakeWebSocketClient)
        client = PolymarketWebSocketClient(
            clock=LiveClock(),
            base_url="wss://ws-subscriptions-clob.polymarket.com/ws/",
            channel=PolymarketWebSocketChannel.USER,
            handler=lambda _payload: None,
            handler_reconnect=None,
            loop=asyncio.get_running_loop(),
        )
        client.add_subscription("condition-1")

        await client.connect()

        assert captured["idle_timeout_ms"] == 300_000

    asyncio.run(scenario())
