"""OrbitExchDataClient(Step 2 整体重写)—— 离线可测部分。

完整集成(真 browser/page/WS、健康检查页面 reload)经 /live-test 验。
"""

import asyncio
from types import SimpleNamespace

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient
from nautilus_trader.adapters.orbitexch.data import oe_runner_to_book_deltas


# ── 纯映射:WS runner → OrderBookDeltas(快照 CLEAR + ADDs)──────
def _iid(): return InstrumentId(Symbol("1-123-2-None"), Venue("ORBITEXCH"))


def test_runner_to_book_deltas_clears_then_adds_both_sides():
    """data-2.snap.1: snapshot 帧 → 1 CLEAR + N BACK(BUY) + M LAY(SELL),包成 OrderBookDeltas。"""
    runner = {
        "selection_id": "2",
        "back": [{"price": 2.26, "size": 80.59}, {"price": 2.24, "size": 50.0}],
        "lay":  [{"price": 2.36, "size": 66.46}],
    }
    out = oe_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000)
    assert isinstance(out, OrderBookDeltas)
    deltas = list(out.deltas)
    assert len(deltas) == 4
    assert deltas[0].action == BookAction.CLEAR
    # back 档 → BUY
    assert deltas[1].order.side == OrderSide.BUY and float(deltas[1].order.price) == pytest.approx(2.26)
    assert deltas[2].order.side == OrderSide.BUY and float(deltas[2].order.price) == pytest.approx(2.24)
    # lay 档 → SELL
    assert deltas[3].order.side == OrderSide.SELL and float(deltas[3].order.price) == pytest.approx(2.36)


def test_runner_to_book_deltas_returns_none_when_empty():
    """data-2.snap.2: back + lay 都空 → 返 None(调用方 skip,避免空簿噪音)。"""
    assert oe_runner_to_book_deltas(_iid(), {"back": [], "lay": []}, ts_init_ns=1) is None
    assert oe_runner_to_book_deltas(_iid(), {}, ts_init_ns=1) is None


def test_runner_to_book_deltas_skips_zero_or_invalid_sizes():
    """data-2.snap.3: size<=0 或缺字段的档跳过,只 CLEAR 也返 None(无实际档不噪)。"""
    runner = {
        "back": [{"price": 2.26, "size": 0}, {"price": 0, "size": 50}],
        "lay":  [{"price": 2.36, "size": -1}],
    }
    assert oe_runner_to_book_deltas(_iid(), runner, ts_init_ns=1) is None


def test_runner_to_book_deltas_only_back_or_only_lay():
    """data-2.snap.4: 单边有档也产 deltas(1 CLEAR + N ADDs)。"""
    only_back = {"back": [{"price": 2.0, "size": 10}], "lay": []}
    out = oe_runner_to_book_deltas(_iid(), only_back, ts_init_ns=1)
    assert out is not None and len(list(out.deltas)) == 2
    assert list(out.deltas)[1].order.side == OrderSide.BUY


# ── DataClient 构造 + 路由(离线)─────────────────────────────────
def _client():
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    return OrbitExchDataClient(
        loop=asyncio.new_event_loop(),
        browser_manager=None,                            # _connect 才用,离线测不到
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=OrbitExchDataClientConfig(username="u", password="p"),
    )


def test_client_constructs_offline():
    """data-2.client.1: 离线可构造(browser 仅 _connect 用)。"""
    c = _client()
    assert str(c.venue) == "ORBITEXCH"
    assert c._market_to_instruments == {}


def test_register_routing_reads_market_and_selection_from_instrument():
    """data-2.client.2: 订阅时从 instrument(BettingInstrument)读 market_id/selection_id 建路由。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client()
    inst = oe_instrument("EPL", "home", selection_id=42)
    c._cache.add_instrument(inst)
    c._register_instrument_routing(inst.id)
    # 路由:market_id "1-123"(factories 默认) → selection_id "42" → inst.id
    assert c._market_to_instruments["1-123"]["42"] == inst.id


def test_register_routing_skips_unknown_instrument():
    """data-2.client.3: cache 没该 instrument → 不崩,routing 不变。"""
    c = _client()
    c._register_instrument_routing(InstrumentId(Symbol("nonexistent"), Venue("ORBITEXCH")))
    assert c._market_to_instruments == {}


def test_unregister_clears_routing():
    """data-2.client.4: 解订 → routing 中该 instrument 项被清。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client()
    inst = oe_instrument("EPL", "home", selection_id=42)
    c._cache.add_instrument(inst)
    c._register_instrument_routing(inst.id)
    c._unregister_instrument_routing(inst.id)
    assert all(inst.id not in m.values() for m in c._market_to_instruments.values())


def test_on_price_frame_routes_to_handle_data():
    """data-2.client.5: parsed 帧的 runner 命中 routing → 经 _handle_data 入 DataEngine。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client()
    inst = oe_instrument("EPL", "home", selection_id=42)
    c._cache.add_instrument(inst)
    c._register_instrument_routing(inst.id)
    captured = []
    c._handle_data = lambda d: captured.append(d)

    # 模拟一个 parsed 后的 message(直接喂 message_parser 的输出形态)
    message = {
        "id": "1-123",
        "mainEventId": "evt-1", "mainEventName": "A v B", "marketNameWithParents": "Match Odds",
        "rc": [{"id": 42, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10}],
                "bdatl": [{"index": 0, "odds": 2.1, "amount": 5}], "tv": 100, "locked": False}],
        "marketDefinition": {"marketType": "MATCH_ODDS", "status": "OPEN", "inPlay": False},
    }
    c._on_price_frame(message)
    assert len(captured) == 1
    assert isinstance(captured[0], OrderBookDeltas)


def test_on_price_frame_unsubscribed_market_dropped():
    """data-2.client.6: 帧 market_id 不在 routing → 静默丢弃,不 publish。"""
    c = _client()
    captured = []
    c._handle_data = lambda d: captured.append(d)
    c._on_price_frame({"id": "unsubscribed", "rc": [], "marketDefinition": {}})
    assert captured == []


# ── #68 每 competition 一页:新开/刷新统一 ────────────────────────
class _FakePage:
    def __init__(self): self.goto_calls = []; self.reload_calls = 0
    def on(self, event, cb): pass          # WS handler 挂 page.on('websocket')
    def remove_listener(self, event, cb): pass
    async def goto(self, url, **kw): self.goto_calls.append(url)
    async def reload(self, **kw): self.reload_calls += 1


class _FakeBM:
    """fake BrowserManager:记录 create_page;page 复用按 name。"""
    def __init__(self): self.created = []; self._pages = {}
    async def start(self): pass
    async def create_page(self, name):
        self.created.append(name)
        p = self._pages.setdefault(name, _FakePage())
        return p


def _client_with_bm(bm):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    c = OrbitExchDataClient(
        loop=asyncio.new_event_loop(), browser_manager=bm, msgbus=msgbus,
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(),
        config=OrbitExchDataClientConfig(username="u", password="p", base_url="https://oe.test"),
    )
    return c


def test_subscribe_opens_competition_page_eager():
    """data-2.page.1(#68): 订阅即开 competition 页,page_key=f'{sport_id}_{competition_id}'。"""
    from tests.arbitrage.risk._factories import oe_instrument
    bm = _FakeBM()
    c = _client_with_bm(bm)
    inst = oe_instrument("EPL", "home", selection_id=42)   # competition_id=1, event_type_id=1
    c._cache.add_instrument(inst)
    cmd = SimpleNamespace(instrument_id=inst.id)
    asyncio.get_event_loop().run_until_complete(c._subscribe_order_book_deltas(cmd))
    assert "comp-1_1" in bm.created
    assert "1_1" in c._comp_pages
    assert c._comp_pages["1_1"].goto_calls == ["https://oe.test/customer/sport/1/competition/1"]


def test_subscribe_same_competition_dedups_page():
    """data-2.page.2(#68): 同 competition 多腿 → 只开一页(去重)。"""
    from tests.arbitrage.risk._factories import oe_instrument
    bm = _FakeBM()
    c = _client_with_bm(bm)
    home = oe_instrument("EPL", "home", selection_id=42)
    away = oe_instrument("EPL", "away", selection_id=43)   # 同 market/competition
    c._cache.add_instrument(home); c._cache.add_instrument(away)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c._subscribe_order_book_deltas(SimpleNamespace(instrument_id=home.id)))
    loop.run_until_complete(c._subscribe_order_book_deltas(SimpleNamespace(instrument_id=away.id)))
    assert bm.created.count("comp-1_1") == 1     # 只 create 一次
    assert len(c._comp_pages) == 1


def test_open_or_reload_reloads_existing_page():
    """data-2.page.3(#68): 已存在的 page_key 再调 → reload(新开/刷新同一套)。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))
    loop.run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))
    page = c._comp_pages["2_999"]
    assert len(page.goto_calls) == 1 and page.reload_calls == 1   # 首次 goto,二次 reload


def test_ensure_page_skips_when_instrument_not_in_cache():
    """data-2.page.4(#68): instrument 不在 cache → 不开页,不崩(routing 注册时已 warn)。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)
    asyncio.get_event_loop().run_until_complete(
        c._ensure_competition_page(InstrumentId(Symbol("nonexistent"), Venue("ORBITEXCH"))))
    assert bm.created == []
