"""SharpExchDataClient 离线边界测试。"""

import asyncio
from types import SimpleNamespace

from nautilus_trader.adapters.sharpexch.config import SharpExchDataClientConfig
from nautilus_trader.adapters.sharpexch.data import SharpExchDataClient
from nautilus_trader.adapters.sharpexch.data import se_update_market_routing
from nautilus_trader.adapters.sharpexch.data import se_update_subscription_state
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchMarketEvent
from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchRunner
from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.data import CustomData
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.market_order_book import OrderBookFrameDeltas
from nautilus_trader.model.market_order_book import OrderBookFrameProcessed
from nautilus_trader.model.market_order_book import market_order_book_data_type
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from tests.arbitrage.adapters.sharpexch.test_provider import _event


def _client(*, update_interval=60, browser_manager=None):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    return SharpExchDataClient(
        loop=asyncio.new_event_loop(),
        browser_manager=browser_manager,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=SharpExchDataClientConfig(
            username="u",
            password="p",
            update_instruments_interval_mins=update_interval,
        ),
    )


def _instrument(role="home"):
    provider = SharpExchInstrumentProvider(SimpleNamespace())
    return next(inst for inst in provider._build_legs(_event()) if inst.info["selection_role"] == role)


def test_data_client_constructs_offline():
    client = _client()
    assert str(client.venue) == "SHARPEXCH"
    assert client._market_to_instruments == {}
    assert client._comp_pages == {}


def test_market_subscription_records_source_market_game_id():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)

    async def no_book(_command):
        return None

    client._subscribe_order_book_deltas = no_book
    command = SimpleNamespace(
        data_type=market_order_book_data_type(Venue("SHARPEXCH"), inst.info["binary_market_id"]),
        params={
            "source_market_id": inst.market_id,
            "instrument_ids": (inst.id,),
            "game_id": 77,
        },
    )
    client._loop.run_until_complete(client._subscribe(command))

    assert client._market_to_game_id[inst.market_id] == 77


def test_connect_starts_browser_loads_provider_and_sends_instruments():
    class Browser:
        def __init__(self):
            self.started = 0

        async def start(self):
            self.started += 1

    class Provider:
        def __init__(self):
            self.loaded = 0
            self.instrument = _instrument("home")

        async def load_all_async(self):
            self.loaded += 1

        def get_all(self):
            return {self.instrument.id: self.instrument}

    browser = Browser()
    provider = Provider()
    client = _client(update_interval=None, browser_manager=browser)
    client._instrument_provider = provider
    captured = []
    client._handle_data = captured.append

    client._loop.run_until_complete(client._connect())

    assert browser.started == 1
    assert provider.loaded == 1
    assert captured == [provider.instrument]
    assert client._disconnecting is False


def test_connect_initial_load_failure_still_starts_periodic_retry():
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

    browser = Browser()
    provider = Provider()
    client = _client(update_interval=1, browser_manager=browser)
    client._instrument_provider = provider
    tasks = []

    def fake_create_task(coro):
        coro.close()
        tasks.append(coro)
        return SimpleNamespace(cancel=lambda: None)

    client.create_task = fake_create_task
    client._loop.run_until_complete(client._connect())

    assert browser.started == 1
    assert provider.loaded == 1
    assert len(tasks) == 1
    assert client._disconnecting is False


def test_disconnect_cancels_update_task_and_stops_handlers():
    class Task:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Handler:
        def __init__(self):
            self.stopped = 0

        async def stop(self):
            self.stopped += 1

    client = _client()
    task = Task()
    handler = Handler()
    client._update_instruments_task = task
    client._comp_handlers = {"2_12597512": handler}
    client._comp_pages = {"2_12597512": object()}

    client._loop.run_until_complete(client._disconnect())

    assert client._disconnecting is True
    assert task.cancelled is True
    assert handler.stopped == 1
    assert client._update_instruments_task is None
    assert client._comp_handlers == {}
    assert client._comp_pages == {}


def test_update_instruments_continues_after_provider_error(monkeypatch):
    client = _client()
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
    client._instrument_provider = Provider()
    client._send_all_instruments_to_data_engine = lambda: calls.__setitem__("send", calls["send"] + 1)

    client._loop.run_until_complete(client._update_instruments(1))

    assert calls["load"] == 2
    assert calls["send"] == 1


def test_subscribe_registers_state_and_opens_page_via_injected_method():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    open_calls = []

    async def open_page(**kwargs):
        open_calls.append(kwargs)
        return {"action": "opened"}

    client._open_or_reload_competition_page = open_page

    asyncio.run(client._subscribe_order_book_deltas(SimpleNamespace(instrument_id=inst.id)))

    assert client._market_to_instruments[inst.market_id][str(inst.selection_id)] == [(inst.id, "yes")]
    assert client._market_to_page_key[inst.market_id] == "2_12597512"
    assert client._comp_page_refs == {"2_12597512": ("2", "12597512")}
    assert open_calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]


def test_synthetic_no_routing_uses_real_venue_selection_id():
    """合成 no 的负 selection 只用于缓存身份，WS 路由必须使用真实 selection。"""
    instrument_id = _instrument("home").id
    inst = SimpleNamespace(
        market_id="1.259502399",
        selection_id=-112,
        info={"venue_selection_id": 111, "quote_claim": "no"},
    )
    routing = {}

    assert se_update_market_routing(routing, instrument_id, inst) is True
    assert routing == {"1.259502399": {"111": [(instrument_id, "no")]}}


def test_concurrent_subscribe_same_competition_dedups_page():
    client = _client()
    home = _instrument("home")
    away = _instrument("away")
    client._cache.add_instrument(home)
    client._cache.add_instrument(away)
    open_calls = []

    async def open_page(**kwargs):
        await asyncio.sleep(0)
        open_calls.append(kwargs)
        client._comp_pages[kwargs["page_key"]] = object()
        return {"action": "opened"}

    async def subscribe_both():
        await asyncio.gather(
            client._subscribe_order_book_deltas(SimpleNamespace(instrument_id=home.id)),
            client._subscribe_order_book_deltas(SimpleNamespace(instrument_id=away.id)),
        )

    client._open_or_reload_competition_page = open_page

    asyncio.run(subscribe_both())

    assert client._market_to_instruments[home.market_id][str(home.selection_id)] == [(home.id, "yes")]
    assert client._market_to_instruments[away.market_id][str(away.selection_id)] == [(away.id, "yes")]
    assert open_calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]
    assert set(client._comp_pages) == {"2_12597512"}


def test_subscribe_open_failure_schedules_delayed_reopen():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    scheduled = []

    async def open_page(**kwargs):
        raise RuntimeError("open failed")

    async def delayed_reopen(page_key, *, delay_secs):
        return (page_key, delay_secs)

    client._open_or_reload_competition_page = open_page
    client._delayed_reopen = delayed_reopen
    client._schedule_task = scheduled.append

    asyncio.run(client._subscribe_order_book_deltas(SimpleNamespace(instrument_id=inst.id)))

    assert len(scheduled) == 1
    scheduled[0].close()
    assert client._market_to_page_key == {inst.market_id: "2_12597512"}


def test_unsubscribe_removes_subscription_state():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._market_to_instruments = {inst.market_id: {str(inst.selection_id): [(inst.id, "yes")]}}
    client._market_to_page_key = {inst.market_id: "2_12597512"}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}

    asyncio.run(client._unsubscribe_order_book_deltas(SimpleNamespace(instrument_id=inst.id)))

    assert client._market_to_instruments == {}
    assert client._market_to_page_key == {}
    assert client._comp_page_refs == {}


def test_delayed_reopen_opens_when_still_subscribed_and_missing():
    client = _client()
    inst = _instrument("home")
    client._market_to_page_key = {inst.market_id: "2_12597512"}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened"}

    client._open_or_reload_competition_page = open_page

    client._loop.run_until_complete(client._delayed_reopen("2_12597512", delay_secs=0))

    assert calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]


def test_delayed_reopen_noops_after_unsubscribe():
    client = _client()
    calls = []
    client._open_or_reload_competition_page = lambda **kwargs: calls.append(kwargs)

    client._loop.run_until_complete(client._delayed_reopen("2_12597512", delay_secs=0))

    assert calls == []


def test_delayed_reopen_failure_schedules_next_retry():
    client = _client()
    inst = _instrument("home")
    client._market_to_page_key = {inst.market_id: "2_12597512"}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}
    scheduled = []

    async def open_page(**kwargs):
        raise RuntimeError("reopen failed")

    client._open_or_reload_competition_page = open_page
    client._schedule_task = scheduled.append

    client._loop.run_until_complete(client._delayed_reopen("2_12597512", delay_secs=0))

    assert len(scheduled) == 1
    scheduled[0].close()


def test_on_price_frame_publishes_deltas():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._market_to_instruments = {inst.market_id: {str(inst.selection_id): [(inst.id, "yes")]}}
    captured = []
    client._handle_data = captured.append

    client._on_price_frame(
        {
            "id": inst.market_id,
            "marketDefinition": {"inPlay": True},
            "rc": [
                {
                    "id": inst.selection_id,
                    "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}],
                    "bdatl": [{"index": 0, "odds": 2.1, "amount": 5.0}],
                },
            ],
        },
    )

    assert len(captured) == 1
    assert isinstance(captured[0], OrderBookDeltas)
    assert "in_play" not in inst.info      # #250:info["in_play"] 写回已废除
    assert client._price_frames_seen == 1
    assert client._price_deltas_published == 1


def test_on_price_frame_updates_phase_without_instrument_info_mutation():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._market_to_instruments = {inst.market_id: {str(inst.selection_id): [(inst.id, "yes")]}}
    client._market_to_game_id[inst.market_id] = 77
    client._handle_data = lambda _data: None

    client._on_price_frame({
        "id": inst.market_id,
        "marketDefinition": {"inPlay": True},
        "rc": [{"id": inst.selection_id, "bdatb": [], "bdatl": []}],
    })

    assert client._phase_store.get(77).phase == PHASE_IN_PLAY
    assert "in_play" not in inst.info


def test_on_price_frame_missing_inplay_does_not_create_phase():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._market_to_instruments = {inst.market_id: {str(inst.selection_id): [(inst.id, "yes")]}}
    client._market_to_game_id[inst.market_id] = 77
    client._handle_data = lambda _data: None

    client._on_price_frame({"id": inst.market_id, "marketDefinition": {}, "rc": [{"id": inst.selection_id}]})

    assert client._phase_store.get(77) is None


def test_on_price_frame_publishes_one_market_batch_for_all_runners():
    client = _client()
    home = _instrument("home")
    away = _instrument("away")
    for instrument in (home, away):
        client._cache.add_instrument(instrument)
    client._market_to_instruments = {
        home.market_id: {
            str(home.selection_id): [(home.id, "yes")],
            str(away.selection_id): [(away.id, "yes")],
        },
    }
    data_type = market_order_book_data_type(Venue("SHARPEXCH"), home.market_id)
    client._add_subscription(data_type)
    captured = []
    client._handle_data = captured.append

    client._on_price_frame({
        "id": home.market_id,
        "rc": [
            {"id": home.selection_id, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10}], "bdatl": []},
            {"id": away.selection_id, "bdatb": [{"index": 0, "odds": 3.0, "amount": 10}], "bdatl": []},
        ],
        "marketDefinition": {},
    })

    assert len(captured) == 1
    assert isinstance(captured[0], CustomData)
    assert isinstance(captured[0].data, OrderBookFrameDeltas)
    assert captured[0].data.markets[0].instrument_ids == (home.id, away.id)


def test_three_way_price_frame_is_atomic_but_split_into_binary_markets():
    client = _client()
    event = SharpExchMarketEvent(
        sport="Soccer",
        competition="EPL",
        home_team="Arsenal",
        away_team="Chelsea",
        sport_id="1",
        competition_id="10932509",
        market_id="1.259502399",
        start_ts=1782768600000 * 1_000_000,
        runners=(
            SharpExchRunner(selection_id="111", runner_name="Arsenal", role="home"),
            SharpExchRunner(selection_id="333", runner_name="The Draw", role="draw"),
            SharpExchRunner(selection_id="222", runner_name="Chelsea", role="away"),
        ),
    )
    legs = list(SharpExchInstrumentProvider(SimpleNamespace())._build_legs(event))
    for instrument in legs:
        client._cache.add_instrument(instrument)
        assert se_update_market_routing(client._market_to_instruments, instrument.id, instrument)
        client._add_subscription(
            market_order_book_data_type(Venue("SHARPEXCH"), instrument.info["binary_market_id"]),
        )
    captured = []
    client._handle_data = captured.append

    def frame(selection_ids):
        return {
            "id": event.market_id,
            "rc": [
                {"id": selection_id, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10}], "bdatl": []}
                for selection_id in selection_ids
            ],
            "marketDefinition": {},
        }

    client._on_price_frame(frame(("111", "333", "222")))

    assert len(captured) == 1
    source_frame = captured[0].data
    assert isinstance(source_frame, OrderBookFrameDeltas)
    assert len(source_frame.markets) == 3
    assert all(len(market.instrument_ids) == 2 for market in source_frame.markets)

    client._on_price_frame(frame(("111",)))
    assert len(captured) == 1
    client._handle_market_frame_processed(
        OrderBookFrameProcessed(
            source_frame.venue,
            source_frame.source_market_id,
            source_frame.frame_id,
            True,
        ),
    )
    assert len(captured) == 2
    assert [market.market_id for market in captured[1].data.markets] == ["1.259502399:111"]


def test_on_price_frame_drops_unrouted_market():
    client = _client()
    captured = []
    client._handle_data = captured.append

    client._on_price_frame({"id": "unrouted", "rc": []})

    assert captured == []
    assert client._price_frames_seen == 0
    assert client._price_deltas_published == 0


def test_comp_disconnect_schedules_reload_for_prices_close():
    client = _client()
    calls = []
    client._comp_pages = {"2_12597512": object()}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "reloaded"}

    client._open_or_reload_competition_page = open_page

    client._on_comp_disconnect("2_12597512", "close:prices")
    client._loop.run_until_complete(asyncio.sleep(0))

    assert calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]
    assert client._comp_reloading == set()


def test_comp_disconnect_clears_binary_market_before_reload():
    """SE prices 断线先以同一 market frame 清空两腿，再 reload。"""
    client = _client()
    yes = _instrument("home")
    no = _instrument("away")
    for instrument in (yes, no):
        client._cache.add_instrument(instrument)
        plan = se_update_subscription_state(
            market_routing=client._market_to_instruments,
            market_to_page_key=client._market_to_page_key,
            comp_page_refs=client._comp_page_refs,
            instrument_id=instrument.id,
            inst=instrument,
        )
        assert plan is not None
    market_id = str(yes.info["binary_market_id"])
    client._add_subscription(market_order_book_data_type(client.venue, market_id))
    client._comp_pages["2_12597512"] = object()
    events = []
    client._handle_data = lambda data: events.append(("clear", data))

    async def open_page(**kwargs):
        events.append(("reload", kwargs))

    client._open_or_reload_competition_page = open_page
    client._on_comp_disconnect("2_12597512", "close:prices")

    assert [event[0] for event in events] == ["clear"]
    frame = events[0][1].data
    assert isinstance(frame, OrderBookFrameDeltas)
    assert frame.source_market_id == str(yes.market_id)
    assert len(frame.markets) == 1
    assert set(frame.markets[0].instrument_ids) == {yes.id, no.id}
    assert all(
        len(deltas.deltas) == 1
        and deltas.deltas[0].action == BookAction.CLEAR
        for deltas in frame.markets[0].deltas
    )

    client._loop.run_until_complete(asyncio.sleep(0))
    assert [event[0] for event in events] == ["clear", "reload"]
    assert client._comp_reloading == set()


def test_comp_duplicate_disconnect_does_not_clear_twice():
    """reload task 尚未运行时的重复 close 也只能清空一次。"""
    client = _client()
    instrument = _instrument("home")
    client._cache.add_instrument(instrument)
    plan = se_update_subscription_state(
        market_routing=client._market_to_instruments,
        market_to_page_key=client._market_to_page_key,
        comp_page_refs=client._comp_page_refs,
        instrument_id=instrument.id,
        inst=instrument,
    )
    assert plan is not None
    market_id = str(instrument.info["binary_market_id"])
    client._add_subscription(market_order_book_data_type(client.venue, market_id))
    client._comp_pages["2_12597512"] = object()
    emitted = []
    scheduled = []
    client._handle_data = emitted.append
    client._schedule_task = scheduled.append

    client._on_comp_disconnect("2_12597512", "close:prices")
    client._on_comp_disconnect("2_12597512", "liveness_timeout")

    assert len(emitted) == 1
    assert len(scheduled) == 1
    for task in scheduled:
        task.close()


def test_comp_disconnect_ignores_non_price_reason():
    client = _client()
    calls = []
    client._comp_pages = {"2_12597512": object()}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}
    client._open_or_reload_competition_page = lambda **kwargs: calls.append(kwargs)

    client._on_comp_disconnect("2_12597512", "close:orders")
    client._loop.run_until_complete(asyncio.sleep(0))

    assert calls == []
    assert client._comp_last_reload_ns == {}


def test_reload_comp_on_disconnect_logs_and_clears_reloading_on_failure():
    client = _client()
    client._comp_pages = {"2_12597512": object()}
    client._comp_page_refs = {"2_12597512": ("2", "12597512")}

    async def open_page(**kwargs):
        raise RuntimeError("reload failed")

    client._open_or_reload_competition_page = open_page

    client._loop.run_until_complete(client._reload_comp_on_disconnect("2_12597512"))

    assert client._comp_reloading == set()


# ── #251:退订归零关 competition 页(对齐 OE)──────────────────────────────
def test_unsubscribe_closes_orphaned_page():
    """退订使页失去全部 market 引用 → stop handler + close_page + 摘表;同页仍有 market 时不关。"""
    from unittest.mock import AsyncMock

    from nautilus_trader.model.identifiers import InstrumentId

    client = _client()
    iid1 = InstrumentId.from_str("A-1.SHARPEXCH")
    iid2 = InstrumentId.from_str("B-1.SHARPEXCH")
    client._market_to_instruments = {"m1": {"s1": [(iid1, "yes")]}, "m2": {"s2": [(iid2, "yes")]}}
    client._market_to_page_key = {"m1": "2_200", "m2": "2_200"}
    client._comp_page_refs = {"2_200": ("2", "200")}
    handler = SimpleNamespace(stop=AsyncMock())
    client._comp_pages["2_200"] = object()
    client._comp_handlers["2_200"] = handler
    client._browser_manager = SimpleNamespace(close_page=AsyncMock())

    client._loop.run_until_complete(
        client._unsubscribe_order_book_deltas(SimpleNamespace(instrument_id=iid1)),
    )
    assert client._comp_pages != {}                  # 页仍被 m2 引用,不关
    handler.stop.assert_not_awaited()

    client._loop.run_until_complete(
        client._unsubscribe_order_book_deltas(SimpleNamespace(instrument_id=iid2)),
    )
    handler.stop.assert_awaited_once()
    client._browser_manager.close_page.assert_awaited_once_with("comp-2_200")
    assert client._comp_pages == {} and client._comp_handlers == {}
    assert client._comp_page_refs == {}
