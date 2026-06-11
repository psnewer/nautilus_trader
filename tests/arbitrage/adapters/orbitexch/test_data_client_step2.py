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
    assert c._price_frames_seen == 1
    assert c._price_deltas_published == 1


def test_on_price_frame_unsubscribed_market_dropped():
    """data-2.client.6: 帧 market_id 不在 routing → 静默丢弃,不 publish。"""
    c = _client()
    captured = []
    c._handle_data = lambda d: captured.append(d)
    c._on_price_frame({"id": "unsubscribed", "rc": [], "marketDefinition": {}})
    assert captured == []
    assert c._price_frames_seen == 0
    assert c._price_deltas_published == 0


# ── #68 每 competition 一页:新开/刷新统一 ────────────────────────
class _FakeWebSocket:
    def __init__(self, url):
        self.url = url
    def on(self, event, cb): pass


class _FakeCdpSession:
    def __init__(self):
        self.callbacks = {}
        self.sent = []
        self.detached = 0

    def on(self, event, cb):
        self.callbacks[event] = cb

    async def send(self, command):
        self.sent.append(command)

    async def detach(self):
        self.detached += 1


class _FakeContext:
    def __init__(self):
        self.sessions = []

    async def new_cdp_session(self, page):
        session = _FakeCdpSession()
        self.sessions.append(session)
        return session


class _FakePage:
    def __init__(self):
        self.goto_calls = []
        self.reload_calls = 0
        self.bring_to_front_calls = 0
        self._callbacks = {}
        self.context = _FakeContext()
    def on(self, event, cb): self._callbacks.setdefault(event, []).append(cb)
    def remove_listener(self, event, cb): pass
    async def bring_to_front(self): self.bring_to_front_calls += 1
    async def goto(self, url, **kw):
        self.goto_calls.append(url)
        self._emit_websocket("wss://oe.test/customer/ws/general/websocket")
        self._emit_websocket("wss://oe.test/customer/ws/multiple-market-prices/websocket")
    async def reload(self, **kw):
        self.reload_calls += 1
        self._emit_websocket(f"wss://oe.test/customer/ws/multiple-market-prices/reload-{self.reload_calls}/websocket")
    def _emit_websocket(self, url):
        for cb in self._callbacks.get("websocket", []):
            cb(_FakeWebSocket(url))


class _FailingGotoPage(_FakePage):
    async def goto(self, url, **kw):
        self.goto_calls.append(url)
        raise TimeoutError("goto timeout")


class _FakeBM:
    """fake BrowserManager:记录 create_page;page 复用按 name。"""
    def __init__(self): self.created = []; self.closed = []; self._pages = {}
    async def start(self): pass
    async def create_page(self, name):
        self.created.append(name)
        p = self._pages.setdefault(name, _FakePage())
        return p
    async def get_page(self, name):
        return self._pages.get(name)
    async def close_page(self, name):
        self.closed.append(name)
        self._pages.pop(name, None)


class _SlowFakeBM(_FakeBM):
    async def create_page(self, name):
        await asyncio.sleep(0)
        return await super().create_page(name)


class _FailingBM(_FakeBM):
    async def create_page(self, name):
        self.created.append(name)
        p = self._pages.setdefault(name, _FailingGotoPage())
        return p


def _client_with_bm(bm, *, leg_settled=None, exec_reload_enabled=False):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    c = OrbitExchDataClient(
        loop=asyncio.new_event_loop(), browser_manager=bm, msgbus=msgbus,
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(),
        config=OrbitExchDataClientConfig(
            username="u", password="p", base_url="https://oe.test",
            health_check_exec_reload_enabled=exec_reload_enabled),
        leg_settled=leg_settled,
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
    assert c._comp_pages["1_1"].bring_to_front_calls == 1


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


def test_concurrent_subscribe_same_competition_dedups_page():
    """data-2.page.5(#68): 同 competition 双腿并发订阅也只开一页(防 create_page 前竞态)。"""
    from tests.arbitrage.risk._factories import oe_instrument
    bm = _SlowFakeBM()
    c = _client_with_bm(bm)
    home = oe_instrument("EPL", "home", selection_id=42)
    away = oe_instrument("EPL", "away", selection_id=43)
    c._cache.add_instrument(home); c._cache.add_instrument(away)

    async def subscribe_both():
        await asyncio.gather(
            c._subscribe_order_book_deltas(SimpleNamespace(instrument_id=home.id)),
            c._subscribe_order_book_deltas(SimpleNamespace(instrument_id=away.id)),
        )

    asyncio.get_event_loop().run_until_complete(subscribe_both())
    assert bm.created.count("comp-1_1") == 1
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
    assert page.bring_to_front_calls == 2


def test_open_page_failure_does_not_cache_stale_page():
    """data-2.page.6(#68): competition goto 失败 → 清理 page/handler,不污染已开页状态。"""
    bm = _FailingBM()
    c = _client_with_bm(bm)
    with pytest.raises(TimeoutError):
        asyncio.get_event_loop().run_until_complete(
            c._open_or_reload_competition_page("2_999", "2", "999"),
    )
    assert "2_999" not in c._comp_pages
    assert "2_999" not in c._comp_handlers
    assert bm.closed == ["comp-2_999"]


def test_open_page_registers_playwright_ws_handler_before_navigation():
    """data-2.page.7(#100): 新开 competition 页只保留 Playwright WS handler 锚点。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)

    asyncio.get_event_loop().run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))

    page = c._comp_pages["2_999"]
    assert "websocket" in page._callbacks
    assert page.goto_calls == ["https://oe.test/customer/sport/2/competition/999"]
    assert page.context.sessions == []


def test_ensure_page_skips_when_instrument_not_in_cache():
    """data-2.page.4(#68): instrument 不在 cache → 不开页,不崩(routing 注册时已 warn)。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)
    asyncio.get_event_loop().run_until_complete(
        c._ensure_competition_page(InstrumentId(Symbol("nonexistent"), Venue("ORBITEXCH"))))
    assert bm.created == []


# ── §6.8.3 健康检查 Phase 1(时间维度:competition 页赔率防冻 reload)──────
from nautilus_trader.core.datetime import secs_to_nanos  # noqa: E402


def _open_page(c, page_key="2_999", sport="2", comp="999"):
    asyncio.get_event_loop().run_until_complete(
        c._open_or_reload_competition_page(page_key, sport, comp))
    return c._comp_pages[page_key]


def test_health_check_reloads_stale_page():
    """data-2.health.1: page `now-last_update>staleness_timeout`(默认 30s)→ reload。"""
    c = _client_with_bm(_FakeBM())
    page = _open_page(c)
    assert len(page.goto_calls) == 1 and page.reload_calls == 0
    c._comp_last_update_ns["2_999"] = c._clock.timestamp_ns() - secs_to_nanos(40)  # stale
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert page.reload_calls == 1


def test_health_check_skips_fresh_page():
    """data-2.health.2: 刚收帧(last_update≈now)→ 不 reload。"""
    c = _client_with_bm(_FakeBM())
    page = _open_page(c)
    c._comp_last_update_ns["2_999"] = c._clock.timestamp_ns()
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert page.reload_calls == 0


def test_health_check_skips_page_without_update_yet():
    """data-2.health.3: 刚开页未收帧(无 last_update)→ 不 reload(避免开页即刷)。"""
    c = _client_with_bm(_FakeBM())
    page = _open_page(c)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())  # 不设 last_update
    assert page.reload_calls == 0


def test_exec_active_refcount_only_counts_oe_legs():
    """data-2.health.4(#89 per-venue):OE 健康检查只数 **OE 自己的腿**,PM 腿不计入;ref-count 不负。

    互斥是 venue-local:OE 健检 reload OE 页面只跟 OE 下单冲突,不跟 PM。`execution.*` 是全局 topic,
    按 msg 的 instrument venue 过滤。"""
    from nautilus_trader.model.identifiers import InstrumentId

    c = _client_with_bm(_FakeBM())
    oe = {"instrument_id": InstrumentId.from_str("1-1-1-None.ORBITEXCH"), "pair_id": "P"}
    pm = {"instrument_id": InstrumentId.from_str("tok.POLYMARKET"), "pair_id": "P"}

    assert c._is_execution_active() is False
    c._on_execution_started(pm)                  # PM 腿:OE 健检不数
    assert c._is_execution_active() is False
    c._on_execution_started(oe)
    assert c._is_execution_active() is True
    c._on_execution_started(oe)
    c._on_execution_finished(oe)
    assert c._is_execution_active() is True       # 还有一个 OE 在飞
    c._on_execution_finished(pm)                 # PM finished:不影响 OE 计数
    assert c._is_execution_active() is True
    c._on_execution_finished(oe)
    assert c._is_execution_active() is False
    c._on_execution_finished(oe)
    assert c._is_execution_active() is False       # 不负


def test_price_frame_stamps_last_update_ns():
    """data-2.health.5: 收价格帧 → 写该 competition 页 last_update_ns(时间维度判定依据)。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client_with_bm(_FakeBM())
    inst = oe_instrument("EPL", "home", selection_id=42)   # competition_id=1, event_type_id=1, market "1-123"
    c._cache.add_instrument(inst)
    c._register_instrument_routing(inst.id)
    assert c._market_to_page_key["1-123"] == "1_1"
    c._handle_data = lambda d: None
    c._on_price_frame({
        "id": "1-123",
        "mainEventId": "evt-1", "mainEventName": "A v B", "marketNameWithParents": "Match Odds",
        "rc": [{"id": 42, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10}],
                "bdatl": [{"index": 0, "odds": 2.1, "amount": 5}], "tv": 100, "locked": False}],
        "marketDefinition": {"marketType": "MATCH_ODDS", "status": "OPEN", "inPlay": False},
    })
    assert "1_1" in c._comp_last_update_ns


# ── §6.8.3 健康检查 Phase 2(状态维度:leg_settled=false → reload execution 页;A 方案,安全闸)──
from src.arbitrage.common.leg_settled import LegSettledRegistry  # noqa: E402


def _reg_with_unsettled():
    reg = LegSettledRegistry()
    reg.arm("pair-1", "inst-A")   # arm → false(未结算)
    return reg


def _bm_with_exec_page():
    bm = _FakeBM()
    bm._pages["execution"] = _FakePage()   # ExecClient 的页(共享 bm,DataClient 经 get_page 取)
    return bm


def test_health_state_dim_reloads_exec_page_when_enabled():
    """data-2.health.6: leg_settled 有未结算腿 + 安全闸开 → reload execution 页。"""
    bm = _bm_with_exec_page()
    c = _client_with_bm(bm, leg_settled=_reg_with_unsettled(), exec_reload_enabled=True)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert bm._pages["execution"].reload_calls == 1


def test_health_state_dim_gated_off_by_default():
    """data-2.health.7: 安全闸默认关 → 即使有未结算腿也不 reload 交易页(默认安全)。"""
    bm = _bm_with_exec_page()
    c = _client_with_bm(bm, leg_settled=_reg_with_unsettled(), exec_reload_enabled=False)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert bm._pages["execution"].reload_calls == 0


def test_health_state_dim_skips_when_all_settled():
    """data-2.health.8: 全部已结算 → 不 reload(即使闸开)。"""
    reg = LegSettledRegistry(); reg.arm("pair-1", "inst-A"); reg.mark("pair-1", "inst-A")
    bm = _bm_with_exec_page()
    c = _client_with_bm(bm, leg_settled=reg, exec_reload_enabled=True)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert bm._pages["execution"].reload_calls == 0


def test_health_state_dim_no_legsettled_no_crash():
    """data-2.health.9: leg_settled=None(data-only 上下文)→ 跳过状态维度,不崩。"""
    bm = _bm_with_exec_page()
    c = _client_with_bm(bm, leg_settled=None, exec_reload_enabled=True)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert bm._pages["execution"].reload_calls == 0


def test_health_state_dim_exec_page_missing_no_crash():
    """data-2.health.10: execution 页未开(get_page None)→ warn,不崩。"""
    bm = _FakeBM()   # 无 execution 页
    c = _client_with_bm(bm, leg_settled=_reg_with_unsettled(), exec_reload_enabled=True)
    asyncio.get_event_loop().run_until_complete(c._run_health_check())   # 不抛


# ── 连接重试维度:已订阅但未开的 competition 页 → 健康检查补开(对齐 PM `_delayed_connect`)──
def test_health_check_reopens_missing_subscribed_page():
    """data-2.health.11: 已订阅(`_market_to_page_key`)但未开 → 健康检查补开。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)
    c._market_to_page_key["mkt-1"] = "2_999"   # 订阅了该 competition,但页没开(初次失败/未开)
    assert "2_999" not in c._comp_pages
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert "comp-2_999" in bm.created and "2_999" in c._comp_pages


def test_health_check_reopen_failure_swallowed_retries_next_tick():
    """data-2.health.12: 补开失败不抛、不入册,留到下一次健康检查重试(每 tick 再试一次)。"""
    bm = _FailingBM()
    c = _client_with_bm(bm)
    c._market_to_page_key["mkt-1"] = "2_999"
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c._run_health_check())   # tick1:goto 失败 → 吞掉
    assert "2_999" not in c._comp_pages
    assert bm.created.count("comp-2_999") == 1
    loop.run_until_complete(c._run_health_check())   # tick2:留到下次再试(不本轮重试)
    assert bm.created.count("comp-2_999") == 2


def test_health_check_no_reopen_when_already_open():
    """data-2.health.13: 已开且新鲜 → 补开维度不重复开(去重),时间维度也不刷。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)
    _open_page(c, "2_999", "2", "999")               # 已开
    c._market_to_page_key["mkt-1"] = "2_999"
    c._comp_last_update_ns["2_999"] = c._clock.timestamp_ns()  # 新鲜
    asyncio.get_event_loop().run_until_complete(c._run_health_check())
    assert bm.created.count("comp-2_999") == 1        # 仅首次 _open_page;health 不再开
    assert c._comp_pages["2_999"].reload_calls == 0
