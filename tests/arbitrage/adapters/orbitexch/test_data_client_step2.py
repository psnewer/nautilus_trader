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
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import price_from_probability
from src.arbitrage.common.venues import probability_from_price

from nautilus_trader.adapters.orbitexch.config import OrbitExchDataClientConfig
from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient
from nautilus_trader.adapters.orbitexch.data import oe_runner_to_book_deltas


@pytest.fixture(autouse=True)
def _fresh_ambient_loop():
    """测试隔离:本文件用 `asyncio.get_event_loop().run_until_complete`;别处用 `asyncio.run` 会清掉
    current loop(留下关闭/无 loop),跨文件污染本文件。每测设一个新 loop,使本文件自洽不依赖 ambient 状态。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


# ── 纯映射:WS runner → OrderBookDeltas(快照 CLEAR + ADDs)──────
def _iid(): return InstrumentId(Symbol("1-123-2-None"), Venue("ORBITEXCH"))


def test_runner_to_book_deltas_clears_then_adds_both_sides():
    """data-2.snap.1(#256 深度改造):snapshot 帧 → 1 CLEAR + 全部 BACK(SELL) + 全部
    LAY(BUY);存入值是 `probability_from_price` 换算后的隐含概率,不是原始赔率。"""
    runner = {
        "selection_id": "2",
        "back": [{"price": 2.26, "size": 80.59}, {"price": 2.24, "size": 50.0}],
        "lay":  [{"price": 2.36, "size": 66.46}, {"price": 2.34, "size": 40.0}],
    }
    out = oe_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000)
    assert isinstance(out, OrderBookDeltas)
    deltas = list(out.deltas)
    assert len(deltas) == 5  # 1 CLEAR + 2 back ADDs + 2 lay ADDs
    assert deltas[0].action == BookAction.CLEAR
    # back 全部档 → SELL(asks),存 probability_from_price(claim=yes)=1/price
    assert deltas[1].order.side == OrderSide.SELL
    assert float(deltas[1].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.26))
    assert deltas[2].order.side == OrderSide.SELL
    assert float(deltas[2].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.24))
    # lay 全部档 → BUY(bids)
    assert deltas[3].order.side == OrderSide.BUY
    assert float(deltas[3].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.36))
    assert deltas[4].order.side == OrderSide.BUY
    assert float(deltas[4].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.34))


def test_runner_to_book_deltas_no_claim_swaps_sides_with_raw_prices():
    """#228:claim=no(合成 no 腿)两侧换位,ask←LAY 列 / bid←BACK 列;#256 换位后仍用同一个
    claim="no" 做概率换算(1 - 1/price)。"""
    runner = {
        "selection_id": "2",
        "back": [{"price": 2.26, "size": 80.59}],
        "lay":  [{"price": 2.36, "size": 66.46}],
    }
    out = oe_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000, claim="no")
    deltas = list(out.deltas)
    assert deltas[0].action == BookAction.CLEAR
    # no 腿 ask ← LAY 列,claim="no" 换算
    assert deltas[1].order.side == OrderSide.SELL
    assert float(deltas[1].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.36, "no"))
    # no 腿 bid ← BACK 列
    assert deltas[2].order.side == OrderSide.BUY
    assert float(deltas[2].order.price) == pytest.approx(probability_from_price(ORBITEXCH, 2.26, "no"))


def test_runner_to_book_deltas_makes_nt_best_prices_match_back_and_lay_top():
    """decimal 深度归一(#256):NT best_ask 逆变换还原=最高 back,best_bid 逆变换还原=最低 lay
    (book 内部存概率,NT 原生排序 ask 取 min/bid 取 max 在概率编码下自洽识别 best)。"""
    runner = {
        "back": [{"price": 1.85, "size": 10}, {"price": 1.82, "size": 20}],
        "lay": [{"price": 1.90, "size": 30}, {"price": 1.88, "size": 40}],
    }
    deltas = oe_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000)
    book = OrderBook(_iid(), BookType.L2_MBP)
    book.apply_deltas(deltas)

    assert price_from_probability(ORBITEXCH, float(book.best_ask_price())) == pytest.approx(1.85)
    assert price_from_probability(ORBITEXCH, float(book.best_bid_price())) == pytest.approx(1.88)


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
    assert list(out.deltas)[1].order.side == OrderSide.SELL  # back → SELL (asks)


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


def test_connect_initial_load_failure_still_starts_periodic_retry():
    """与 SE 对齐:首轮 discovery 失败(如 CSRF 未出现)不杀 DataClient,仍启动周期重试。"""
    from types import SimpleNamespace

    class Browser:
        def __init__(self):
            self.started = 0

        async def start(self):
            self.started += 1

    class Provider:
        def __init__(self):
            self.loaded = 0

        async def load_all_async(self):
            self.loaded += 1
            raise RuntimeError("temporary csrf timeout")

    c = _client()
    c._browser_manager = Browser()
    c._instrument_provider = Provider()
    tasks = []

    def fake_create_task(coro):
        coro.close()
        tasks.append(coro)
        return SimpleNamespace(cancel=lambda: None)

    c.create_task = fake_create_task
    c._loop.run_until_complete(c._connect())

    assert c._browser_manager.started == 1
    assert c._instrument_provider.loaded == 1
    assert len(tasks) == 1
    assert c._disconnecting is False


def test_register_routing_reads_market_and_selection_from_instrument():
    """data-2.client.2: 订阅时从 instrument(BettingInstrument)读 market_id/selection_id 建路由。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client()
    inst = oe_instrument("EPL", "home", selection_id=42)
    c._cache.add_instrument(inst)
    c._register_instrument_routing(inst.id)
    # 路由(#228 多值):market_id "1-123"(factories 默认) → selection_id "42" → [(inst.id, claim)]
    assert c._market_to_instruments["1-123"]["42"] == [(inst.id, "yes")]


def test_register_synthetic_no_routing_uses_real_venue_selection_id():
    """合成 no 的负 selection 只用于缓存身份，WS 路由必须使用真实 selection。"""
    from tests.arbitrage.risk._factories import oe_instrument
    c = _client()
    inst = oe_instrument("EPL", "home", selection_id=-43)
    inst.info["venue_selection_id"] = 42
    inst.info["quote_claim"] = "no"
    c._cache.add_instrument(inst)

    c._register_instrument_routing(inst.id)

    assert c._market_to_instruments["1-123"]["42"] == [(inst.id, "no")]


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


def test_update_instruments_continues_after_provider_error(monkeypatch):
    """data-2.client.7: 周期发现单轮网络异常只跳过本轮,下一轮继续。"""
    _run_update_instruments_continues_after_provider_error(monkeypatch)


def _run_update_instruments_continues_after_provider_error(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        c = _client()
        c._loop = loop
        calls = {"sleep": 0, "load": 0, "send": 0}

        async def fake_sleep(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 3:
                raise asyncio.CancelledError

        class Provider:
            async def load_all_async(self):
                calls["load"] += 1
                if calls["load"] == 1:
                    raise RuntimeError("temporary network outage")

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        c._instrument_provider = Provider()
        c._send_all_instruments_to_data_engine = lambda: calls.__setitem__("send", calls["send"] + 1)

        loop.run_until_complete(c._update_instruments(1))

        assert calls["load"] == 2
        assert calls["send"] == 1
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


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
        self.closed = False
    def is_closed(self): return self.closed
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


def _client_with_bm(bm):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    c = OrbitExchDataClient(
        loop=asyncio.new_event_loop(), browser_manager=bm, msgbus=msgbus,
        cache=TestComponentStubs.cache(), clock=clock,
        instrument_provider=InstrumentProvider(),
        config=OrbitExchDataClientConfig(
            username="u", password="p", base_url="https://oe.test"),
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


def test_open_or_reload_discards_closed_page_and_reopens():
    """死页逃生口:reload 分支遇 `is_closed()` 的 page → 摘账 + close_page + 降级新建。

    没有这一步,死 page 永久占着 `_comp_pages`,本方法进不了新建分支,每轮 liveness_timeout
    都在对尸体 reload(必抛),盘口静默停更且 venue 仍 alive(data 侧不接 VenueExecutionLiveness)。
    """
    bm = _FakeBM()
    c = _client_with_bm(bm)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))
    dead_page = c._comp_pages["2_999"]
    dead_handler = c._comp_handlers["2_999"]
    dead_page.closed = True   # 模拟 tab 崩掉 / 被关

    loop.run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))

    assert dead_page.reload_calls == 0                  # 没有对尸体 reload
    assert bm.closed == ["comp-2_999"]                  # 摘账时关旧 tab / 清 registry
    assert bm.created == ["comp-2_999", "comp-2_999"]   # 随后新建同名页
    fresh_page = c._comp_pages["2_999"]
    assert fresh_page is not dead_page                  # 注册表换成新 page
    assert c._comp_handlers["2_999"] is not dead_handler
    assert fresh_page.goto_calls == ["https://oe.test/customer/sport/2/competition/999"]


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


def test_open_page_liveness_tracks_prices_feed_only():
    """data-2.page.8(#109):competition 页存活只盯 prices feed,orders 心跳不算盘口存活。"""
    bm = _FakeBM()
    c = _client_with_bm(bm)

    asyncio.get_event_loop().run_until_complete(c._open_or_reload_competition_page("2_999", "2", "999"))

    handler = c._comp_handlers["2_999"]
    assert handler._liveness_ws_type == "prices"


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


# ── #109:competition 页存活封装进 WS handler(心跳超时 + close → on_disconnect)。
#     handler 内部 liveness timer 的单测见 `test_ws_general_frames.py`;此处只测 DataClient 消费侧
#     (收 on_disconnect → reload)+ 事件化 connect-retry(`_delayed_reopen`)。旧 `_run_health_check`/
#     staleness poll / `_mark_comp_frame` / `_comp_last_frame_ns` 已退役删除(#109)。
class _RecordLoop:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        return coro


# ── 事件化 connect-retry(#109,对齐 PM `_delayed_connect`):开页失败 → _delayed_reopen 重试 ──
def test_delayed_reopen_succeeds_no_further_retry(monkeypatch):
    """data-2.health.11(#109):`_delayed_reopen` 开成功 → 入册、不再重排。"""
    import nautilus_trader.adapters.orbitexch.data as data_mod
    monkeypatch.setattr(data_mod, "_COMP_REOPEN_RETRY_SECS", 0.0)
    bm = _FakeBM()
    c = _client_with_bm(bm)
    c._market_to_page_key["mkt-1"] = "2_999"
    c._loop = _RecordLoop()
    asyncio.get_event_loop().run_until_complete(c._delayed_reopen("2_999", "2", "999"))
    assert "2_999" in c._comp_pages          # 开成功
    assert c._loop.tasks == []               # 不再重排


def test_delayed_reopen_failure_reschedules(monkeypatch):
    """data-2.health.12(#109):`_delayed_reopen` 仍失败 → 再排一次(直到成功),对齐 PM 重连重试。"""
    import nautilus_trader.adapters.orbitexch.data as data_mod
    monkeypatch.setattr(data_mod, "_COMP_REOPEN_RETRY_SECS", 0.0)
    bm = _FailingBM()
    c = _client_with_bm(bm)
    c._market_to_page_key["mkt-1"] = "2_999"
    c._loop = _RecordLoop()
    asyncio.get_event_loop().run_until_complete(c._delayed_reopen("2_999", "2", "999"))
    assert "2_999" not in c._comp_pages      # 开失败
    assert bm.created.count("comp-2_999") == 1  # 试开了一次
    assert len(c._loop.tasks) == 1           # 仍失败 → 再排一次
    for t in c._loop.tasks:
        t.close()


def test_delayed_reopen_aborts_when_disconnecting(monkeypatch):
    """data-2.health.13(#109):关停中 → `_delayed_reopen` 放弃重试(不试开)。"""
    import nautilus_trader.adapters.orbitexch.data as data_mod
    monkeypatch.setattr(data_mod, "_COMP_REOPEN_RETRY_SECS", 0.0)
    bm = _FailingBM()
    c = _client_with_bm(bm)
    c._market_to_page_key["mkt-1"] = "2_999"
    c._disconnecting = True
    asyncio.get_event_loop().run_until_complete(c._delayed_reopen("2_999", "2", "999"))
    assert bm.created.count("comp-2_999") == 0  # 放弃,不开


# ── on_disconnect(close 或心跳超时)→ reload(对称 PM `_schedule_delayed_connect`)──
def test_disconnect_prices_close_schedules_reload():
    """data-2.health.14:prices close → 即时调度 reload;跑完 task 页 reload。"""
    c = _client_with_bm(_FakeBM())
    page = _open_page(c)
    c._loop = _RecordLoop()
    c._on_comp_disconnect("2_999", "close:prices")
    assert len(c._loop.tasks) == 1
    asyncio.get_event_loop().run_until_complete(c._loop.tasks[0])
    assert page.reload_calls == 1


def test_disconnect_liveness_timeout_schedules_reload():
    """data-2.health.15:心跳超时(静默死亡)→ 调度 reload。"""
    c = _client_with_bm(_FakeBM())
    _open_page(c)
    c._loop = _RecordLoop()
    c._on_comp_disconnect("2_999", "liveness_timeout")
    assert len(c._loop.tasks) == 1
    for t in c._loop.tasks:
        t.close()


def test_disconnect_guards():
    """data-2.health.16:非 prices/非心跳 reason / 关停 / reload 中 / 页未开 → 不调度。"""
    c = _client_with_bm(_FakeBM())
    _open_page(c)
    c._loop = _RecordLoop()

    c._on_comp_disconnect("2_999", "close:orders")    # 非赔率 feed
    assert c._loop.tasks == []
    c._disconnecting = True
    c._on_comp_disconnect("2_999", "liveness_timeout")  # 关停中
    assert c._loop.tasks == []
    c._disconnecting = False
    c._comp_reloading.add("2_999")
    c._on_comp_disconnect("2_999", "close:prices")    # 本页正在 reload(自身关旧 WS 不自触发)
    assert c._loop.tasks == []
    c._comp_reloading.discard("2_999")
    c._on_comp_disconnect("nope", "close:prices")     # 页未开
    assert c._loop.tasks == []


def test_disconnect_cooldown_suppresses_storm():
    """data-2.health.17:冷却窗内重复 disconnect(venue 持续不可用)→ 抑制,防 reload 风暴。"""
    c = _client_with_bm(_FakeBM())
    _open_page(c)
    c._loop = _RecordLoop()
    c._on_comp_disconnect("2_999", "close:prices")    # 首次:即时
    assert len(c._loop.tasks) == 1
    c._on_comp_disconnect("2_999", "liveness_timeout")  # 冷却窗内:抑制
    assert len(c._loop.tasks) == 1
    for t in c._loop.tasks:
        t.close()


# ── #251:退订归零关 competition 页(#68"保持打开"废除)────────────────────
def test_unregister_routing_returns_orphaned_page_key():
    """同页两 market:退订第一个不孤儿;退订最后一个返回该 page_key,且路由/映射清空。"""
    from nautilus_trader.model.identifiers import InstrumentId

    c = _client()
    iid1 = InstrumentId.from_str("A-1.ORBITEXCH")
    iid2 = InstrumentId.from_str("B-1.ORBITEXCH")
    c._market_to_instruments = {"m1": {"s1": [(iid1, "yes")]}, "m2": {"s2": [(iid2, "yes")]}}
    c._market_to_page_key = {"m1": "1_100", "m2": "1_100"}

    assert c._unregister_instrument_routing(iid1) == []          # 页仍被 m2 引用
    assert "m1" not in c._market_to_instruments and "m1" not in c._market_to_page_key

    assert c._unregister_instrument_routing(iid2) == ["1_100"]   # 最后一个 market → 孤儿页
    assert c._market_to_instruments == {} and c._market_to_page_key == {}


def test_unsubscribe_closes_orphaned_page():
    """退订使页失去全部 market 订阅 → stop handler + close_page + 摘表;再次退订 no-op。"""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nautilus_trader.model.identifiers import InstrumentId

    c = _client()
    iid = InstrumentId.from_str("A-1.ORBITEXCH")
    c._market_to_instruments = {"m1": {"s1": [(iid, "yes")]}}
    c._market_to_page_key = {"m1": "1_100"}
    handler = SimpleNamespace(stop=AsyncMock())
    c._comp_pages["1_100"] = object()
    c._comp_handlers["1_100"] = handler
    c._browser_manager = SimpleNamespace(close_page=AsyncMock())

    asyncio.run(c._unsubscribe_order_book_deltas(SimpleNamespace(instrument_id=iid)))

    handler.stop.assert_awaited_once()
    c._browser_manager.close_page.assert_awaited_once_with("comp-1_100")
    assert c._comp_pages == {} and c._comp_handlers == {}

    asyncio.run(c._unsubscribe_order_book_deltas(SimpleNamespace(instrument_id=iid)))  # 幂等
    assert c._browser_manager.close_page.await_count == 1


def test_oe_subscription_plan_from_instrument_pure():
    """#251:订阅状态机纯函数(对齐 SE)—— plan 提取:缺 market/selection → None;
    合成 no 腿优先 venue_selection_id;缺 sport/comp → page_key None。"""
    from types import SimpleNamespace

    from nautilus_trader.adapters.orbitexch.data import oe_subscription_plan_from_instrument

    assert oe_subscription_plan_from_instrument(None) is None
    assert oe_subscription_plan_from_instrument(
        SimpleNamespace(market_id=None, info={}, selection_id=None),
    ) is None

    plan = oe_subscription_plan_from_instrument(SimpleNamespace(
        market_id="1-123", info={"venue_selection_id": 42, "quote_claim": "NO"},
        selection_id=99, event_type_id=1, competition_id=100,
    ))
    assert plan == {"market_id": "1-123", "selection_id": "42", "claim": "no", "page_key": "1_100"}

    plan2 = oe_subscription_plan_from_instrument(SimpleNamespace(
        market_id="1-123", info={}, selection_id=7, event_type_id="", competition_id="",
    ))
    assert plan2["selection_id"] == "7" and plan2["claim"] == "yes" and plan2["page_key"] is None


def test_oe_update_and_remove_subscription_state_pure():
    """#251:写入幂等(同 iid 不重复)+ 移除返回孤儿 page_key,两表同步清空。"""
    from nautilus_trader.model.identifiers import InstrumentId

    from nautilus_trader.adapters.orbitexch.data import oe_remove_subscription_state
    from nautilus_trader.adapters.orbitexch.data import oe_update_subscription_state

    iid = InstrumentId.from_str("A-1.ORBITEXCH")
    routing: dict = {}
    pages: dict = {}
    plan = {"market_id": "m1", "selection_id": "s1", "claim": "yes", "page_key": "1_100"}
    oe_update_subscription_state(market_routing=routing, market_to_page_key=pages, instrument_id=iid, plan=plan)
    oe_update_subscription_state(market_routing=routing, market_to_page_key=pages, instrument_id=iid, plan=plan)
    assert routing == {"m1": {"s1": [(iid, "yes")]}} and pages == {"m1": "1_100"}

    assert oe_remove_subscription_state(
        market_routing=routing, market_to_page_key=pages, instrument_id=iid,
    ) == ["1_100"]
    assert routing == {} and pages == {}
