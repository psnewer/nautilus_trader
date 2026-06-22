"""WebGatewayActor 只读监控 MVP(web-7.1 / 7.3 / 7.6 / 7.7)。

两层覆盖:
- Actor 层:广播 / 队列背压 / 事件回调 / 快照(actor 经 `__new__` 绕过 NT init,只测纯 Python 逻辑)。
- App 层:FastAPI `TestClient` 测 HTTP 路由 + WS 发送路径(用 stub actor 替身)。
"""

import asyncio
import socket

from fastapi.testclient import TestClient

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money

from src.arbitrage.matching.events import MatchedPair
from src.arbitrage.risk.portfolio import OutcomeExposure
from src.arbitrage.web.actor import WebGatewayActor
from src.arbitrage.web.actor import _account_state_to_json
from src.arbitrage.web.actor import _port_bindable
from src.arbitrage.web.app import build_app


_USDC = Currency.from_str("USDC")


def _account_state() -> AccountState:
    bal = AccountBalance(Money(100, _USDC), Money(0, _USDC), Money(100, _USDC))
    return AccountState(
        AccountId("POLYMARKET-001"), AccountType.CASH, None, True, [bal], [], {}, UUID4(), 0, 0,
    )


def _matched_pair(pair_id="ATP|a|b") -> MatchedPair:
    return MatchedPair(
        ts_event=1, ts_init=1, pair_id=pair_id, sport="atp", competition="ATP",
        pm_instrument_ids=["pm1", "pm2"], oe_instrument_ids=["oe1", "oe2"], confidence=0.9,
    )


def _bare_actor() -> WebGatewayActor:
    """绕过 NT Actor init,只立纯 Python 字段,测广播/队列逻辑。"""
    actor = WebGatewayActor.__new__(WebGatewayActor)
    actor._matched_pairs = {}
    actor._ws_clients = set()
    return actor


# ── Actor 层:广播 / 缓存 / 队列 ──────────────────────────────────────
def test_on_matched_pair_caches_and_broadcasts():
    actor = _bare_actor()
    q = actor.register_ws()
    actor._on_matched_pair(_matched_pair())
    assert actor.matched_pairs()[0]["pair_id"] == "ATP|a|b"   # web-7.1 缓存
    msg = q.get_nowait()
    assert msg["type"] == "matched_pair" and msg["data"]["confidence"] == 0.9


def test_on_account_state_broadcasts():
    actor = _bare_actor()
    q = actor.register_ws()
    actor._on_account_state(_account_state())                  # web-7.3 推送
    msg = q.get_nowait()
    assert msg["type"] == "account" and msg["data"]["account_id"] == "POLYMARKET-001"


def test_non_matching_event_ignored():
    actor = _bare_actor()
    q = actor.register_ws()
    actor._on_matched_pair(object())       # 非 MatchedPair → no-op
    actor._on_account_state(object())      # 非 AccountState → no-op
    assert q.empty() and actor.matched_pairs() == []


def test_enqueue_drops_oldest_when_full():
    actor = _bare_actor()
    q = asyncio.Queue(maxsize=2)
    actor._ws_clients.add(q)
    for i in range(5):
        actor._broadcast({"n": i})
    # 满则丢最旧;队列保留最近 2 条
    assert [q.get_nowait()["n"] for _ in range(2)] == [3, 4]


def test_unregister_stops_broadcast():
    actor = _bare_actor()
    q = actor.register_ws()
    actor.unregister_ws(q)
    actor._on_matched_pair(_matched_pair())
    assert q.empty()


def test_account_state_serialization_roundtrip():
    d = _account_state_to_json(_account_state())
    assert d["account_id"] == "POLYMARKET-001" and "balances" in d


def test_port_bindable_detects_free_and_occupied():
    # 占一个端口,_port_bindable 应判其不可绑;关闭后恢复可绑(端口被占预检,避免静默 bind 失败)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert _port_bindable("127.0.0.1", port) is False
    finally:
        sock.close()
    assert _port_bindable("127.0.0.1", port) is True


# ── App 层:HTTP 路由(stub actor)────────────────────────────────────
class _StubPortfolio:
    def way_rebate(self, pair_id, account_id=None):
        return {"home": 0.1, "away": -0.02}

    def way_rebates_by_venue(self, pair_id, account_id=None):
        return {"home": {"POLYMARKET": 0.05, "ORBITEXCH": 0.05}}

    def outcome_exposures(self, pair_id, account_id=None):
        return {"home": OutcomeExposure(net_profit=1.5, liability=2.0)}

    def global_min_rebate_sum(self, account_id=None):
        return -0.03


class _StubActor:
    def __init__(self):
        self._portfolio = _StubPortfolio()
        self._accounts = [{"account_id": "POLYMARKET-001"}]
        self._pairs = [{"pair_id": "ATP|a|b"}]
        self._ws_queue = None

    def accounts_snapshot(self):
        return self._accounts

    def matched_pairs(self):
        return self._pairs

    def register_ws(self):
        return self._ws_queue

    def unregister_ws(self, q):
        pass


def _client(actor=None) -> TestClient:
    return TestClient(build_app(actor or _StubActor()))


def test_health():
    assert _client().get("/health").json() == {"status": "ok"}


def test_get_accounts():
    assert _client().get("/accounts").json() == [{"account_id": "POLYMARKET-001"}]   # web-7.3 快照


def test_get_matched_pairs():
    assert _client().get("/matched_pairs").json() == [{"pair_id": "ATP|a|b"}]        # web-7.1


def test_get_positions_pair():
    r = _client().get("/positions/ATP%7Ca%7Cb").json()                              # web-7.6
    assert r["pair_id"] == "ATP|a|b"
    assert r["way_rebate"]["home"] == 0.1
    assert r["way_rebates_by_venue"]["home"]["POLYMARKET"] == 0.05
    assert r["outcome_exposures"]["home"] == {"net_profit": 1.5, "liability": 2.0}


def test_get_global_min_rebate_sum_route_not_shadowed():
    # web-7.7:此路由必须先于 /positions/{pair_id},否则 "global_min_rebate_sum" 被当 pair_id
    r = _client().get("/positions/global_min_rebate_sum").json()
    assert r == {"global_min_rebate_sum": -0.03}


def test_ws_sends_queued_message_then_closes_on_poison():
    actor = _StubActor()
    q = asyncio.Queue()
    q.put_nowait({"type": "matched_pair", "data": {"pair_id": "x"}})
    q.put_nowait(None)   # 毒丸 → 优雅关闭
    actor._ws_queue = q
    with _client(actor).websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "matched_pair", "data": {"pair_id": "x"}}


# ── 控制台:TradingState 启停 + 配置编辑(#119)────────────────────────
import json as _json

from nautilus_trader.model.enums import TradingState

from src.arbitrage.common.control import TOPIC_REFRESH_INTERVAL
from src.arbitrage.common.control import TOPIC_RISK_PARAMS
from src.arbitrage.common.control import TOPIC_TRADING_STATE
from src.arbitrage.common.control import SetTradingStateCommand
from src.arbitrage.risk.config import ArbRiskParams


class _StubRisk:
    def __init__(self, state):
        self.trading_state = state
        self._params = ArbRiskParams(share=22.5, match_tp=0.05, match_sl=-0.05, max_leg_share=None)


def _control_actor(tmp_path, *, risk_state=TradingState.HALTED):
    actor = WebGatewayActor.__new__(WebGatewayActor)
    actor._matched_pairs = {}
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
    _json_path = tmp_path / "arb_config.json"
    _json_path.write_text(_json.dumps({"risk": {"share": 22.5}}))
    result = actor.update_config_section("risk", {"share": 50.0, "match_tp": 0.1})
    assert result["applied"] == "live"
    # 写回文件
    assert _json.loads(_json_path.read_text())["risk"]["share"] == 50.0
    # 发热改命令
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
class _ControlStubActor(_StubActor):
    def __init__(self):
        super().__init__()
        self.set_calls = []
        self.put_calls = []

    def trading_state(self):
        return "HALTED"

    def set_trading_state(self, state):
        self.set_calls.append(state)

    def config_snapshot(self):
        return {"file": {}, "live": {"trading_state": "HALTED", "risk": {}}}

    def update_config_section(self, section, fields):
        self.put_calls.append((section, fields))
        return {"status": "ok", "section": section, "applied": "live"}


def test_post_trading_state_ok():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).post("/control/trading_state", json={"state": "active"})
    assert r.status_code == 200 and actor.set_calls == ["ACTIVE"]


def test_post_trading_state_invalid_400():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).post("/control/trading_state", json={"state": "REDUCING"})
    assert r.status_code == 400 and actor.set_calls == []


def test_get_trading_state():
    assert _client(_ControlStubActor()).get("/control/trading_state").json() == {"trading_state": "HALTED"}


def test_get_config():
    assert _client(_ControlStubActor()).get("/config").json()["live"]["trading_state"] == "HALTED"


def test_put_config_section():
    actor = _ControlStubActor()
    r = TestClient(build_app(actor)).put("/config/risk", json={"share": 30.0})
    assert r.status_code == 200 and actor.put_calls == [("risk", {"share": 30.0})]
