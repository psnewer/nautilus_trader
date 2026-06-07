"""OE `general` 频道帧解析(oe-adapter-5.ws.{1,2})。

样本来源:2026-05-22 用户登录刷新页面 copy 的真实 WS 帧。
覆盖:SockJS `a[...]` 解包 → 顶层 key 分型 → BALANCE/CURRENT_BETS/未知。
"""

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
