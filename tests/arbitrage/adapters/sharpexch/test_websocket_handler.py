"""SharpExch WebSocket handler 离线测试。"""

import asyncio

from nautilus_trader.adapters.sharpexch.message_parser import SharpExchMessageParser
from nautilus_trader.adapters.sharpexch.websocket_handler import SharpExchWebSocketHandler


RAW_BALANCE = r'a["{\"BALANCE\":{\"balance\":\"37.49\",\"avBalance\":null}}"]'
RAW_CURRENT_BETS = r'a["{\"CURRENT_BETS\":[]}"]'
RAW_PRICE = r'a["{\"id\":\"1.259502313\",\"rc\":[{\"id\":111,\"bdatb\":{\"0\":[2.1,7.0]}}]}"]'
RAW_SUBSCRIBE_UP = r'["{\"BALANCE\":{\"subscribe\":true,\"applicationType\":\"WEB\"}}"]'


class _FakePage:
    def __init__(self):
        self.listeners = {}
        self.removed = []

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        self.removed.append((event, callback))


class _FakeWebSocket:
    def __init__(self, url):
        self.url = url
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback


class _CapturingLogger:
    def __init__(self):
        self.info_messages = []
        self.debug_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def debug(self, message):
        self.debug_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


class _FakeClock:
    def __init__(self):
        self.now_ns = 1_000
        self.alerts = {}
        self.canceled = []

    def timestamp_ns(self):
        return self.now_ns

    def set_time_alert_ns(self, *, name, alert_time_ns, callback):
        self.alerts[name] = (alert_time_ns, callback)

    def cancel_timer(self, name):
        self.canceled.append(name)
        if name not in self.alerts:
            raise KeyError(name)
        del self.alerts[name]


def test_start_and_stop_register_page_listener():
    page = _FakePage()
    handler = SharpExchWebSocketHandler(page)

    asyncio.run(handler.start())
    assert "websocket" in page.listeners

    asyncio.run(handler.stop())
    assert page.removed == [("websocket", handler._on_websocket)]


def test_sockjs_unwrap_to_parsed_balance():
    handler = SharpExchWebSocketHandler(page=None)
    parser = SharpExchMessageParser()
    captured = []
    handler.on_order_update(lambda msg: captured.append(parser.parse_general_frame(msg)))

    handler._on_frame_received("orders", RAW_BALANCE)

    assert captured == [{"type": "balance", "balance": 37.49, "av_balance": None}]


def test_frame_callback_fires_for_heartbeat_before_business_filter():
    handler = SharpExchWebSocketHandler(page=None)
    calls = []
    handler.on_frame(lambda: calls.append("frame"))

    handler._on_frame_received("orders", "h")

    assert calls == ["frame"]


def test_liveness_uses_only_configured_ws_type_frames():
    clock = _FakeClock()
    handler = SharpExchWebSocketHandler(
        _FakePage(),
        clock=clock,
        liveness_timeout_secs=1.0,
        liveness_name="se-liveness",
        liveness_ws_type="prices",
    )
    disconnected = []
    handler.on_disconnect(disconnected.append)

    asyncio.run(handler.start())
    assert clock.alerts["se-liveness"][0] == 1_000_001_000

    clock.now_ns += 500_000_000
    handler._on_frame_received("orders", "h")
    assert handler._last_frame_ns == 1_000

    handler._on_frame_received("prices", "h")
    assert handler._last_frame_ns == 500_001_000

    clock.now_ns = 1_500_001_000
    clock.alerts["se-liveness"][1](None)
    assert disconnected == ["liveness_timeout"]
    assert clock.alerts["se-liveness"][0] == 2_500_001_000


def test_sockjs_unwrap_to_parsed_current_bets():
    handler = SharpExchWebSocketHandler(page=None)
    parser = SharpExchMessageParser()
    captured = []
    handler.on_order_update(lambda msg: captured.append(parser.parse_general_frame(msg)))

    handler._on_frame_received("orders", RAW_CURRENT_BETS)

    assert captured == [{"type": "current_bets", "bets": []}]


def test_sockjs_unwrap_to_price_callback():
    handler = SharpExchWebSocketHandler(page=None)
    captured = []
    handler.on_price_update(captured.append)

    handler._on_frame_received("prices", RAW_PRICE)

    assert captured == [{"id": "1.259502313", "rc": [{"id": 111, "bdatb": {"0": [2.1, 7.0]}}]}]


def test_client_subscribe_frame_not_data():
    handler = SharpExchWebSocketHandler(page=None)
    captured = []
    handler.on_order_update(captured.append)

    handler._on_frame_received("orders", RAW_SUBSCRIBE_UP)

    assert captured == []


def test_websocket_type_detection_and_close_callback():
    logger = _CapturingLogger()
    handler = SharpExchWebSocketHandler(page=None, logger=logger)
    disconnected = []
    handler.on_disconnect(disconnected.append)
    ws = _FakeWebSocket("wss://se.test/customer/ws/multiple-market-prices/websocket")

    handler._on_websocket(ws)
    assert handler.get_active_websockets() == [
        {"url": ws.url, "type": "prices"},
    ]

    ws.listeners["close"]()
    assert handler.get_active_websockets() == []
    assert disconnected == ["close:prices"]
    assert logger.info_messages[-1] == f"SE WS closed: type=prices, url={ws.url}"


def test_first_frame_logs_once():
    logger = _CapturingLogger()
    handler = SharpExchWebSocketHandler(page=None, logger=logger)

    handler._on_frame_received("prices", 'a["{}"]')
    handler._on_frame_received("prices", 'a["{}"]')

    assert handler.get_frame_counts() == {"prices": 2}
    assert logger.info_messages == [
        "SE WS first frame received: type=prices, kind=sockjs_message, bytes=7",
    ]


def test_bad_json_does_not_call_callbacks():
    handler = SharpExchWebSocketHandler(page=None)
    captured = []
    handler.on_price_update(captured.append)

    handler._on_frame_received("prices", 'a["{bad"]')

    assert captured == []
