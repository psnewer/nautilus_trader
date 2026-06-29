"""WebGatewayActor 控制台(web §8):TradingState 启停 + 配置编辑 + /ws。

纯只读监控 endpoint(余额/matched_pairs/way_rebate)已移除(2026-06-21,用户裁定)。
两层覆盖:Actor 层(广播/队列/控制方法,`__new__` 绕过 NT init)+ App 层(FastAPI TestClient)。
"""

import asyncio
import json as _json
import socket

from fastapi.testclient import TestClient

from nautilus_trader.model.enums import TradingState

from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import TOPIC_RISK_PARAMS
from src.arbitrage.common.control import TOPIC_TRADING_STATE
from src.arbitrage.common.control import SetTradingStateCommand
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.web.actor import WebGatewayActor
from src.arbitrage.web.actor import _port_bindable
from src.arbitrage.web.app import build_app


# ── 端口预检 + WS 广播/队列(Actor 纯逻辑)────────────────────────────
def test_port_bindable_detects_free_and_occupied():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert _port_bindable("127.0.0.1", port) is False
    finally:
        sock.close()
    assert _port_bindable("127.0.0.1", port) is True


def _bare_actor() -> WebGatewayActor:
    actor = WebGatewayActor.__new__(WebGatewayActor)
    actor._ws_clients = set()
    return actor


def test_enqueue_drops_oldest_when_full():
    actor = _bare_actor()
    q = asyncio.Queue(maxsize=2)
    actor._ws_clients.add(q)
    for i in range(5):
        actor._broadcast({"n": i})
    assert [q.get_nowait()["n"] for _ in range(2)] == [3, 4]   # 满丢最旧


def test_unregister_stops_broadcast():
    actor = _bare_actor()
    q = actor.register_ws()
    actor.unregister_ws(q)
    actor._broadcast({"n": 1})
    assert q.empty()


def test_on_risk_event_broadcasts_trading_state():
    actor = _bare_actor()
    q = actor.register_ws()

    class _Evt:
        trading_state = TradingState.HALTED

    actor._on_risk_event(_Evt())
    msg = q.get_nowait()
    assert msg["type"] == "trading_state" and msg["data"]["state"] == "HALTED"


# ── 控制台:TradingState 启停 + 配置编辑(Actor 方法)──────────────────
class _StubRisk:
    def __init__(self, state):
        self.trading_state = state
        self._params = ArbRiskParams(share=22.5, match_tp=0.05, match_sl=-0.05)


def _control_actor(tmp_path, *, risk_state=TradingState.HALTED):
    actor = WebGatewayActor.__new__(WebGatewayActor)
    actor._ws_clients = set()
    actor._risk_engine = _StubRisk(risk_state)
    actor._config_path = str(tmp_path / "arb_config.json")
    # `_msgbus` 是只读 cdef 属性,改覆盖 `_publish` 间接层记录 publish。
    actor.published = []
    actor._publish = lambda topic, msg: actor.published.append((topic, msg))
    return actor


def test_set_trading_state_publishes_command(tmp_path):
    actor = _control_actor(tmp_path)
    actor.set_trading_state("ACTIVE")
    assert actor.published == [(TOPIC_TRADING_STATE, SetTradingStateCommand("ACTIVE"))]


def test_trading_state_reads_risk_engine(tmp_path):
    actor = _control_actor(tmp_path, risk_state=TradingState.HALTED)
    assert actor.trading_state() == "HALTED"


def test_update_risk_config_writes_file_and_publishes_command(tmp_path):
    actor = _control_actor(tmp_path)
    path = tmp_path / "arb_config.json"
    path.write_text(_json.dumps({"risk": {"share": 22.5}}))
    result = actor.update_config_section("risk", {"share": 50.0, "match_tp": 0.1})
    assert result["applied"] == "live"
    assert _json.loads(path.read_text())["risk"]["share"] == 50.0
    topic, cmd = actor.published[-1]
    assert topic == TOPIC_RISK_PARAMS and cmd.share == 50.0 and cmd.match_tp == 0.1


def test_update_restart_section_writes_file_no_command(tmp_path):
    actor = _control_actor(tmp_path)
    (tmp_path / "arb_config.json").write_text(_json.dumps({"venues": {}}))
    result = actor.update_config_section("venues", {"polymarket": {"funder": "0xabc"}})
    assert result["applied"] == "on_restart"
    assert actor.published == []   # 重启段不发命令


def test_update_matching_refresh_interval_publishes(tmp_path):
    actor = _control_actor(tmp_path)
    (tmp_path / "arb_config.json").write_text(_json.dumps({"matching": {}}))
    result = actor.update_config_section("matching", {"refresh_interval_secs": 15.0})
    assert result["applied"] == "live"
    topic, cmd = actor.published[-1]
    assert topic == TOPIC_REFRESH_INTERVAL and cmd.secs == 15.0


def test_config_snapshot_returns_file_and_live(tmp_path):
    actor = _control_actor(tmp_path, risk_state=TradingState.HALTED)
    (tmp_path / "arb_config.json").write_text(_json.dumps({"risk": {"share": 22.5}}))
    snap = actor.config_snapshot()
    assert snap["file"]["risk"]["share"] == 22.5
    assert snap["live"]["trading_state"] == "HALTED"
    assert snap["live"]["risk"]["share"] == 22.5


# ── App 层控制路由(stub actor)────────────────────────────────────────
class _ControlStubActor:
    def __init__(self):
        self.set_calls = []
        self.put_calls = []
        self._ws_queue = None

    def trading_state(self):
        return "HALTED"

    def set_trading_state(self, state):
        self.set_calls.append(state)

    def config_snapshot(self):
        return {"file": {}, "live": {"trading_state": "HALTED", "risk": {}}}

    def update_config_section(self, section, fields):
        self.put_calls.append((section, fields))
        return {"status": "ok", "section": section, "applied": "live"}

    def register_ws(self):
        return self._ws_queue

    def unregister_ws(self, q):
        pass

    def accounts_snapshot(self):
        return [{"account_id": "POLYMARKET-001", "balances": [{"total": "65.0", "currency": "USDC.e"}]}]

    def odds_snapshot(self):
        return [{"pair_id": "ATP|a|b", "legs": [{"venue": "POLYMARKET", "role": "home", "bid": 0.4, "ask": 0.42}]}]

    def matched_pairs(self):
        return [{"pair_id": "ATP|a|b", "sport": "Tennis", "competition": "ATP",
                 "pm_instrument_ids": ["p1", "p2"], "oe_instrument_ids": ["o1", "o2"], "confidence": 0.9}]

    def instruments_snapshot(self):
        return [{"venue": "POLYMARKET", "sport": "Tennis", "competition": "ATP", "home": "A", "away": "B"}]


def _client(actor=None) -> TestClient:
    return TestClient(build_app(actor or _ControlStubActor()))


def test_health():
    assert _client().get("/health").json() == {"status": "ok"}


def test_post_trading_state_ok():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).post("/control/trading_state", json={"state": "active"})
    assert r.status_code == 200 and actor.set_calls == ["ACTIVE"]


def test_post_trading_state_invalid_400():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).post("/control/trading_state", json={"state": "REDUCING"})
    assert r.status_code == 400 and actor.set_calls == []


def test_get_trading_state():
    assert _client().get("/control/trading_state").json() == {"trading_state": "HALTED"}


def test_get_config():
    assert _client().get("/config").json()["live"]["trading_state"] == "HALTED"


def test_put_config_section():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).put("/config/risk", json={"share": 30.0})
    assert r.status_code == 200 and actor.put_calls == [("risk", {"share": 30.0})]


def test_index_serves_html():
    r = _client().get("/")
    assert r.status_code == 200 and "Arbitrage Dashboard" in r.text and "text/html" in r.headers["content-type"]


def test_get_accounts():
    assert _client().get("/accounts").json()[0]["account_id"] == "POLYMARKET-001"


def test_get_odds():
    odds = _client().get("/odds").json()
    assert odds[0]["pair_id"] == "ATP|a|b" and odds[0]["legs"][0]["ask"] == 0.42


def test_get_matched_pairs():
    mp = _client().get("/matched_pairs").json()
    assert mp[0]["competition"] == "ATP" and mp[0]["confidence"] == 0.9


def test_get_instruments():
    inst = _client().get("/instruments").json()
    assert inst[0]["venue"] == "POLYMARKET" and inst[0]["home"] == "A"


def test_ws_sends_queued_message_then_closes_on_poison():
    actor = _ControlStubActor()
    q = asyncio.Queue()
    q.put_nowait({"type": "trading_state", "data": {"state": "ACTIVE"}})
    q.put_nowait(None)   # 毒丸 → 优雅关闭
    actor._ws_queue = q
    with _client(actor).websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "trading_state", "data": {"state": "ACTIVE"}}
