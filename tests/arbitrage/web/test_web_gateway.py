"""WebGatewayActor 控制台(web §8):TradingState 启停 + 配置编辑 + /ws。

纯只读监控 endpoint(余额/matched_pairs/way_rebate)已移除(2026-06-21,用户裁定)。
两层覆盖:Actor 层(广播/队列/控制方法,`__new__` 绕过 NT init)+ App 层(FastAPI TestClient)。
"""

import asyncio
import json as _json
import socket

import pytest
from fastapi.testclient import TestClient

from nautilus_trader.model.enums import TradingState

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import TOPIC_RISK_PARAMS
from src.arbitrage.common.control import TOPIC_TRADING_STATE
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import SetTradingStateCommand
from src.arbitrage.common.params import ArbitrageParams
from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.web.actor import WebGatewayActor
from src.arbitrage.web.actor import _port_bindable
from src.arbitrage.web.app import build_app


# ── 端口预检 + WS 广播/队列(Actor 纯逻辑)────────────────────────────
def test_port_bindable_detects_free_and_occupied():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
    except PermissionError as exc:
        sock.close()
        pytest.skip(f"local socket bind not permitted in this environment: {exc}")
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
    actor._pair_registry = None
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


def test_on_matched_pair_stores_venue_instrument_ids():
    actor = _bare_actor()
    actor._matched_pairs = {}
    data = MatchedPair(
        ts_event=1,
        ts_init=1,
        pair_id="ATP|a|b|SHARPEXCH",
        sport="Tennis",
        competition="ATP",
        tradable_instrument_ids=[
            "pm-home.POLYMARKET",
            "pm-away.POLYMARKET",
            "se-home.SHARPEXCH",
            "se-away.SHARPEXCH",
        ],
        venue_instrument_ids={
            "POLYMARKET": ["pm-home.POLYMARKET", "pm-away.POLYMARKET"],
            "SHARPEXCH": ["se-home.SHARPEXCH", "se-away.SHARPEXCH"],
        },
        confidence=0.9,
    )

    actor._on_matched_pair(data)

    row = actor._matched_pairs["ATP|a|b|SHARPEXCH"]
    assert row["venue_instrument_ids"] == {
        "POLYMARKET": ["pm-home.POLYMARKET", "pm-away.POLYMARKET"],
        "SHARPEXCH": ["se-home.SHARPEXCH", "se-away.SHARPEXCH"],
    }
    assert row["tradable_instrument_ids"] == data.tradable_instrument_ids
    # 不再有 external_* 或 pm_*/oe_* 旧字段
    assert "external_venue" not in row
    assert "pm_instrument_ids" not in row
    assert "oe_instrument_ids" not in row


def test_on_matched_pair_handles_empty_venue_map():
    actor = _bare_actor()
    actor._matched_pairs = {}
    data = MatchedPair(
        ts_event=1,
        ts_init=1,
        pair_id="legacy",
        sport="Tennis",
        competition="ATP",
        confidence=0.9,
    )

    actor._on_matched_pair(data)

    row = actor._matched_pairs["legacy"]
    assert row["venue_instrument_ids"] == {}
    assert row["tradable_instrument_ids"] == data.tradable_instrument_ids
    assert "external_venue" not in row
    assert "external_instrument_ids" not in row


def test_on_matched_pair_uses_explicit_venue_map_only():
    """venue_instrument_ids 来自 MatchedPair.venue_instrument_ids,不从 instrument suffix 推断。"""
    actor = _bare_actor()
    actor._matched_pairs = {}
    data = MatchedPair(
        ts_event=1,
        ts_init=1,
        pair_id="suffix-only",
        sport="Tennis",
        competition="ATP",
        tradable_instrument_ids=["se-home.SHARPEXCH"],
        confidence=0.9,
    )

    actor._on_matched_pair(data)

    row = actor._matched_pairs["suffix-only"]
    # 无 venue_instrument_ids 传入则为空,不从 instrument suffix 推断
    assert row["venue_instrument_ids"] == {}
    assert "external_venue" not in row


def test_matched_pairs_exposes_all_venue_teams():
    """matched_pairs() 从 PairRegistry 当前态输出 venue_teams 字典。"""
    actor = _bare_actor()
    actor._matched_pairs = {}
    registry = PairRegistry()
    actor._pair_registry = registry
    actor._venue_teams = lambda iids: f"{iids[0]} teams" if iids else ""
    data = MatchedPair(
        ts_event=1,
        ts_init=1,
        pair_id="ATP|a|b",
        sport="Tennis",
        competition="ATP",
        anchor_instrument_ids=["anchor.PMSPORTS"],
        tradable_instrument_ids=[
            "pm-home.POLYMARKET",
            "pm-away.POLYMARKET",
            "oe-home.ORBITEXCH",
            "oe-away.ORBITEXCH",
            "se-home.SHARPEXCH",
            "se-away.SHARPEXCH",
        ],
        venue_instrument_ids={
            "POLYMARKET": ["pm-home.POLYMARKET", "pm-away.POLYMARKET"],
            "ORBITEXCH": ["oe-home.ORBITEXCH", "oe-away.ORBITEXCH"],
            "SHARPEXCH": ["se-home.SHARPEXCH", "se-away.SHARPEXCH"],
        },
        confidence=0.9,
    )

    actor._on_matched_pair(data)
    registry.register(
        data.pair_id,
        data.tradable_instrument_ids,
        anchor_instrument_ids=data.anchor_instrument_ids,
    )
    row = actor.matched_pairs()[0]

    # 每个 venue 都在 venue_teams 字典里;registry 内部是 set,不要求 home/away 顺序。
    assert set(row["venue_teams"]) == {"POLYMARKET", "ORBITEXCH", "SHARPEXCH"}
    assert row["venue_teams"]["POLYMARKET"].endswith("POLYMARKET teams")
    assert row["venue_teams"]["ORBITEXCH"].endswith("ORBITEXCH teams")
    assert row["venue_teams"]["SHARPEXCH"].endswith("SHARPEXCH teams")
    # 不再有 external_* 或 pm_*/oe_* 旧字段
    assert "external_venues" not in row
    assert "external_venue" not in row
    assert "external_teams" not in row
    assert "pm_teams" not in row
    assert "oe_teams" not in row


def test_matched_pairs_hides_unregistered_pair():
    """Matching 页面 membership 以 PairRegistry 为准,不会展示已 eviction 的旧 MatchedPair 事件。"""
    actor = _bare_actor()
    actor._matched_pairs = {}
    registry = PairRegistry()
    actor._pair_registry = registry
    actor._venue_teams = lambda iids: ""
    data = MatchedPair(
        ts_event=1,
        ts_init=1,
        pair_id="ATP|a|b",
        sport="Tennis",
        competition="ATP",
        tradable_instrument_ids=["pm-home.POLYMARKET", "oe-home.ORBITEXCH"],
        venue_instrument_ids={
            "POLYMARKET": ["pm-home.POLYMARKET"],
            "ORBITEXCH": ["oe-home.ORBITEXCH"],
        },
        confidence=0.9,
    )

    actor._on_matched_pair(data)
    assert actor.matched_pairs() == []

    registry.register(data.pair_id, data.tradable_instrument_ids)
    assert [row["pair_id"] for row in actor.matched_pairs()] == ["ATP|a|b"]

    registry.unregister_pair(data.pair_id)
    assert actor.matched_pairs() == []


# ── 控制台:TradingState 启停 + 配置编辑(Actor 方法)──────────────────
class _StubRisk:
    def __init__(self, state):
        self.trading_state = state
        self._params = ArbRiskParams(
            match_tp=0.05,
            match_sl=-0.05,
            min_probability=0.03,
            max_probability=0.97,
        )


def _control_actor(tmp_path, *, risk_state=TradingState.HALTED):
    actor = WebGatewayActor.__new__(WebGatewayActor)
    actor._ws_clients = set()
    actor._risk_engine = _StubRisk(risk_state)
    actor._arbitrage_params = ArbitrageParams(share=22.5, max_leg_share=100.0, fx=1.33)
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
    path.write_text(_json.dumps({"risk": {"match_tp": 0.05}}))
    result = actor.update_config_section(
        "risk",
        {
            "match_tp": 0.1,
            "min_probability": 0.04,
            "max_probability": 0.96,
        },
    )
    assert result["applied"] == "live"
    assert _json.loads(path.read_text())["risk"]["match_tp"] == 0.1
    topic, cmd = actor.published[-1]
    assert topic == TOPIC_RISK_PARAMS
    assert cmd.match_tp == 0.1
    assert cmd.min_probability == 0.04 and cmd.max_probability == 0.96


def test_update_arbitrage_config_writes_file_and_publishes_command(tmp_path):
    actor = _control_actor(tmp_path)
    path = tmp_path / "arb_config.json"
    path.write_text(_json.dumps({"arbitrage": {"share": 22.5}}))
    result = actor.update_config_section(
        "arbitrage",
        {"share": 50.0, "max_leg_share": 100.0, "fx": 1.33},
    )
    assert result["applied"] == "live"
    assert _json.loads(path.read_text())["arbitrage"]["share"] == 50.0
    topic, cmd = actor.published[-1]
    assert topic == TOPIC_ARBITRAGE_PARAMS
    assert cmd == SetArbitrageParamsCommand(share=50.0, max_leg_share=100.0, fx=1.33)
    assert actor.live_arbitrage_params() == {"share": 50.0, "max_leg_share": 100.0, "fx": 1.33}


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
    (tmp_path / "arb_config.json").write_text(_json.dumps({"arbitrage": {"share": 22.5}, "risk": {}}))
    snap = actor.config_snapshot()
    assert snap["file"]["arbitrage"]["share"] == 22.5
    assert snap["live"]["trading_state"] == "HALTED"
    assert snap["live"]["risk"]["min_probability"] == 0.03
    assert snap["live"]["arbitrage"]["max_leg_share"] == 100.0


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
        return {"file": {}, "live": {"trading_state": "HALTED", "risk": {}, "arbitrage": {}}}

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
                 "venue_instrument_ids": {"POLYMARKET": ["p1", "p2"], "ORBITEXCH": ["o1", "o2"]},
                 "tradable_instrument_ids": ["p1", "p2", "o1", "o2"],
                 "confidence": 0.9}]

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
    r = TestClient(build_app(actor)).put("/config/arbitrage", json={"share": 30.0})
    assert r.status_code == 200 and actor.put_calls == [("arbitrage", {"share": 30.0})]


def test_index_serves_html():
    r = _client().get("/")
    assert r.status_code == 200 and "Arbitrage Dashboard" in r.text and "text/html" in r.headers["content-type"]
    assert "SharpExch" in r.text and "enabledVenueIds" in r.text and "odds-data-head" in r.text
    assert "matching-results-head" in r.text and "PMSPORTS" not in r.text and "Confidence" not in r.text
    assert 'id="d-sharp"' in r.text and "browser discovery" in r.text


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
