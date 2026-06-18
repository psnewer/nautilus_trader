"""OE `general` 频道帧解析(oe-adapter-5.ws.{1,2})。

样本来源:2026-05-22 用户登录刷新页面 copy 的真实 WS 帧。
覆盖:SockJS `a[...]` 解包 → 顶层 key 分型 → BALANCE/CURRENT_BETS/未知。
"""

from nautilus_trader.common.component import TestClock
from nautilus_trader.core.datetime import secs_to_nanos

from nautilus_trader.adapters.orbitexch.message_parser import OrbitExchMessageParser
from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler


# 真实抓帧(下行 a[...];上行订阅请求 ["..."] 不带 a、无数据)
RAW_BALANCE = r'a["{\"BALANCE\":{\"balance\":\"37.49\",\"avBalance\":null}}"]'
RAW_CURRENT_BETS = r'a["{\"CURRENT_BETS\":[]}"]'
RAW_SUBSCRIBE_UP = r'["{\"BALANCE\":{\"subscribe\":true,\"applicationType\":\"WEB\"}}"]'


# ── 解析器分型(oe-adapter-5.ws.1)─────────────────────────────────
def test_parse_balance_frame():
    p = OrbitExchMessageParser()
    out = p.parse_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
    assert out == {"type": "balance", "balance": 37.49, "av_balance": None}


def test_parse_current_bets_empty():
    p = OrbitExchMessageParser()
    out = p.parse_general_frame({"CURRENT_BETS": []})
    assert out == {"type": "current_bets", "bets": []}


def test_parse_current_bets_with_items_passthrough():
    # item schema 工作假设:与 REST /customer/api/currentBets 同源;此处只验证透传
    p = OrbitExchMessageParser()
    bet = {"marketId": "1-123", "selectionId": 2, "sizeMatched": 50.0, "averagePrice": 2.5}
    out = p.parse_general_frame({"CURRENT_BETS": [bet]})
    assert out == {"type": "current_bets", "bets": [bet]}


def test_unknown_frame_ignored():
    p = OrbitExchMessageParser()
    assert p.parse_general_frame({"SOMETHING_ELSE": 1}) is None
    assert p.parse_general_frame(["not a dict"]) is None


def test_balance_string_or_null_robust():
    p = OrbitExchMessageParser()
    assert p.parse_general_frame({"BALANCE": {"balance": None}})["balance"] is None
    assert p.parse_general_frame({"BALANCE": {"balance": "bad"}})["balance"] is None
    assert p.parse_general_frame({"BALANCE": {"balance": "0"}})["balance"] == 0.0


def test_balance_nested_json_string_payload():
    p = OrbitExchMessageParser()
    out = p.parse_general_frame({"BALANCE": "{\"balance\":\"37.49\",\"avBalance\":null}"})
    assert out == {"type": "balance", "balance": 37.49, "av_balance": None}


def test_balance_non_dict_payload_ignored():
    p = OrbitExchMessageParser()
    assert p.parse_general_frame({"BALANCE": "not-json"}) is None


def test_current_bets_nested_json_string_payload_filters_non_dict_items():
    p = OrbitExchMessageParser()
    bet = {"offerId": "abc", "sizeMatched": 0}
    out = p.parse_general_frame({"CURRENT_BETS": "[{\"offerId\":\"abc\",\"sizeMatched\":0},\"bad\"]"})
    assert out == {"type": "current_bets", "bets": [bet]}


# ── 全链路:SockJS a[...] 解包 → callback → 解析(oe-adapter-5.ws.2)──
def test_sockjs_unwrap_to_parsed_balance():
    handler = OrbitExchWebSocketHandler(page=None)
    parser = OrbitExchMessageParser()
    captured = []
    handler.on_order_update(lambda msg: captured.append(parser.parse_general_frame(msg)))

    handler._on_frame_received("orders", RAW_BALANCE)

    assert captured == [{"type": "balance", "balance": 37.49, "av_balance": None}]


def test_sockjs_unwrap_to_parsed_current_bets():
    handler = OrbitExchWebSocketHandler(page=None)
    parser = OrbitExchMessageParser()
    captured = []
    handler.on_order_update(lambda msg: captured.append(parser.parse_general_frame(msg)))

    handler._on_frame_received("orders", RAW_CURRENT_BETS)

    assert captured == [{"type": "current_bets", "bets": []}]


def test_upstream_subscribe_frame_not_data():
    # 上行订阅请求 ["..."] 以 '[' 开头 → handler 视为非数据帧,跳过(不进 callback)
    handler = OrbitExchWebSocketHandler(page=None)
    captured = []
    handler.on_order_update(captured.append)

    handler._on_frame_received("orders", RAW_SUBSCRIBE_UP)

    assert captured == []


def test_first_frame_logs_ws_type_kind_and_size():
    logger = _CapturingLogger()
    handler = OrbitExchWebSocketHandler(page=None, logger=logger)

    handler._on_frame_received("prices", 'a["{}"]')
    handler._on_frame_received("prices", 'a["{}"]')

    assert logger.info_messages == [
        "OE WS first frame received: type=prices, kind=sockjs_message, bytes=7",
    ]


# ── #109:handler 内部存活封装(被动心跳超时 + close → on_disconnect)──────────
def _liveness_handler(clock, timeout=30.0):
    h = OrbitExchWebSocketHandler(page=None, clock=clock, liveness_timeout_secs=timeout, liveness_name="t")
    fired = []
    h.on_disconnect(lambda reason: fired.append(reason))
    h._running = True
    h._last_frame_ns = clock.timestamp_ns()
    h._schedule_liveness()
    return h, fired


def test_liveness_disabled_without_clock():
    """oe-ws-liveness.1:不传 clock/timeout → 内部存活关闭,帧不更新锚、不 fire(执行页 general WS 行为不变)。"""
    h = OrbitExchWebSocketHandler(page=None)
    assert h._liveness_enabled is False
    h._on_frame_received("prices", 'a["{}"]')   # 不崩、不动 _last_frame_ns
    assert h._last_frame_ns == 0


def test_liveness_fires_disconnect_on_frame_gap():
    """oe-ws-liveness.2:无任何帧超 timeout(心跳停=静默死亡)→ fire on_disconnect("liveness_timeout")。"""
    clock = TestClock()
    h, fired = _liveness_handler(clock, timeout=30.0)
    for handler in clock.advance_time(secs_to_nanos(31.0)):
        handler.handle()
    assert fired == ["liveness_timeout"]


def test_liveness_frame_resets_no_disconnect():
    """oe-ws-liveness.3:timeout 内有帧(含心跳)→ 重置存活,不 fire(安静市场靠心跳保活)。"""
    clock = TestClock()
    h, fired = _liveness_handler(clock, timeout=30.0)
    for handler in clock.advance_time(secs_to_nanos(20.0)):
        handler.handle()                          # → t=20s(绝对),alert(30s)未到
    h._on_frame_received("prices", 'a["{}"]')     # 帧到达 → _last_frame_ns=20s
    for handler in clock.advance_time(secs_to_nanos(31.0)):
        handler.handle()                          # → t=31s(绝对),alert 触发但 now-last=11<30 → 活,重排不 fire
    assert fired == []


def test_close_prices_fires_disconnect():
    """oe-ws-liveness.4:prices WS close → fire on_disconnect("close:prices")(干净关闭快路)。"""
    h = OrbitExchWebSocketHandler(page=None)
    fired = []
    h.on_disconnect(lambda reason: fired.append(reason))
    h._websockets["url1"] = {"ws": None, "type": "prices", "url": "url1"}
    h._on_websocket_close("url1")
    assert fired == ["close:prices"]


class _CapturingLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def debug(self, message):
        pass

    def error(self, message):
        pass
