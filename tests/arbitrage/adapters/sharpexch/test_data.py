"""SharpExch data 侧纯映射测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.sharpexch.data import se_competition_page_ref_from_instrument
from nautilus_trader.adapters.sharpexch.data import se_competition_page_url
from nautilus_trader.adapters.sharpexch.data import se_ensure_competition_page
from nautilus_trader.adapters.sharpexch.data import se_handle_price_frame
from nautilus_trader.adapters.sharpexch.data import se_market_price_message_to_book_deltas
from nautilus_trader.adapters.sharpexch.data import se_open_or_reload_competition_page
from nautilus_trader.adapters.sharpexch.data import se_price_message_to_book_deltas
from nautilus_trader.adapters.sharpexch.data import se_publish_routed_book_deltas
from nautilus_trader.adapters.sharpexch.data import se_reopen_missing_page
from nautilus_trader.adapters.sharpexch.data import se_remove_market_routing
from nautilus_trader.adapters.sharpexch.data import se_remove_subscription_state
from nautilus_trader.adapters.sharpexch.data import se_reload_competition_on_disconnect
from nautilus_trader.adapters.sharpexch.data import se_routing_entry_from_instrument
from nautilus_trader.adapters.sharpexch.data import se_runner_to_book_deltas
from nautilus_trader.adapters.sharpexch.data import se_should_reload_on_disconnect
from nautilus_trader.adapters.sharpexch.data import se_should_reopen_missing_page
from nautilus_trader.adapters.sharpexch.data import se_subscription_plan_from_instrument
from nautilus_trader.adapters.sharpexch.data import se_update_market_routing
from nautilus_trader.adapters.sharpexch.data import se_update_subscription_state
from nautilus_trader.adapters.sharpexch.data import se_websocket_summary
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue


def _iid():
    return InstrumentId(Symbol("1-259502313-111-None"), Venue("SHARPEXCH"))


def _iid_away():
    return InstrumentId(Symbol("1-259502313-222-None"), Venue("SHARPEXCH"))


class _FakePage:
    def __init__(self, calls, fail_goto=False):
        self.calls = calls
        self.fail_goto = fail_goto

    async def bring_to_front(self):
        self.calls.append("bring_to_front")

    async def goto(self, url, wait_until, timeout):
        self.calls.append(("goto", url, wait_until, timeout))
        if self.fail_goto:
            raise RuntimeError("goto failed")

    async def reload(self, wait_until, timeout):
        self.calls.append(("reload", wait_until, timeout))


class _FakeBrowserManager:
    def __init__(self, page):
        self.page = page
        self.created = []
        self.closed = []

    async def create_page(self, name):
        self.created.append(name)
        return self.page

    async def close_page(self, name):
        self.closed.append(name)


class _FakeHandler:
    def __init__(self, page, logger=None, **kwargs):
        self.page = page
        self.logger = logger
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.price_callbacks = []
        self.disconnect_callbacks = []

    def on_price_update(self, callback):
        self.price_callbacks.append(callback)

    def on_disconnect(self, callback):
        self.disconnect_callbacks.append(callback)

    async def start(self):
        self.started = True
        self.page.calls.append("handler_start")

    async def stop(self):
        self.stopped = True
        self.page.calls.append("handler_stop")

    def get_active_websockets(self):
        return [{"url": "u1", "type": "prices"}]


def test_routing_entry_from_instrument_coerces_ids_to_strings():
    inst = SimpleNamespace(market_id="1.259502313", selection_id=111)
    assert se_routing_entry_from_instrument(inst) == ("1.259502313", "111")


def test_routing_entry_from_instrument_requires_market_and_selection():
    assert se_routing_entry_from_instrument(SimpleNamespace(market_id="", selection_id=111)) is None
    assert se_routing_entry_from_instrument(SimpleNamespace(market_id="1.259502313", selection_id=None)) is None


def test_update_and_remove_market_routing():
    routing = {}
    inst = SimpleNamespace(market_id="1.259502313", selection_id=111)

    assert se_update_market_routing(routing, _iid(), inst) is True
    assert routing == {"1.259502313": {"111": [(_iid(), "yes")]}}

    se_remove_market_routing(routing, _iid())
    assert routing == {"1.259502313": {}}


def test_update_market_routing_returns_false_for_missing_fields():
    routing = {}
    inst = SimpleNamespace(market_id="", selection_id=111)
    assert se_update_market_routing(routing, _iid(), inst) is False
    assert routing == {}


def test_subscription_plan_from_instrument_combines_routing_and_page_ref():
    inst = SimpleNamespace(
        market_id="1.259502313",
        selection_id=111,
        event_type_id=2,
        competition_id=12597512,
    )

    assert se_subscription_plan_from_instrument(inst) == {
        "market_id": "1.259502313",
        "selection_id": "111",
        "page_key": "2_12597512",
        "sport_id": "2",
        "competition_id": "12597512",
    }


def test_subscription_plan_from_instrument_requires_routing_and_page_ref():
    assert se_subscription_plan_from_instrument(
        SimpleNamespace(market_id="", selection_id=111, event_type_id=2, competition_id=12597512),
    ) is None
    assert se_subscription_plan_from_instrument(
        SimpleNamespace(market_id="1.259502313", selection_id=111, event_type_id="", competition_id=12597512),
    ) is None


def test_update_subscription_state_writes_all_data_client_indexes():
    market_routing = {}
    market_to_page_key = {}
    comp_page_refs = {}
    inst = SimpleNamespace(
        market_id="1.259502313",
        selection_id=111,
        event_type_id=2,
        competition_id=12597512,
    )

    plan = se_update_subscription_state(
        market_routing=market_routing,
        market_to_page_key=market_to_page_key,
        comp_page_refs=comp_page_refs,
        instrument_id=_iid(),
        inst=inst,
    )

    assert plan == {
        "market_id": "1.259502313",
        "selection_id": "111",
        "page_key": "2_12597512",
        "sport_id": "2",
        "competition_id": "12597512",
    }
    assert market_routing == {"1.259502313": {"111": [(_iid(), "yes")]}}
    assert market_to_page_key == {"1.259502313": "2_12597512"}
    assert comp_page_refs == {"2_12597512": ("2", "12597512")}


def test_update_subscription_state_returns_none_without_side_effects_for_bad_instrument():
    market_routing = {}
    market_to_page_key = {}
    comp_page_refs = {}

    plan = se_update_subscription_state(
        market_routing=market_routing,
        market_to_page_key=market_to_page_key,
        comp_page_refs=comp_page_refs,
        instrument_id=_iid(),
        inst=SimpleNamespace(market_id="", selection_id=111, event_type_id=2, competition_id=12597512),
    )

    assert plan is None
    assert market_routing == {}
    assert market_to_page_key == {}
    assert comp_page_refs == {}


def test_remove_subscription_state_keeps_market_and_page_when_other_selection_remains():
    market_routing = {"1.259502313": {"111": [(_iid(), "yes")], "222": [(_iid_away(), "yes")]}}
    market_to_page_key = {"1.259502313": "2_12597512"}
    comp_page_refs = {"2_12597512": ("2", "12597512")}

    se_remove_subscription_state(
        market_routing=market_routing,
        market_to_page_key=market_to_page_key,
        comp_page_refs=comp_page_refs,
        instrument_id=_iid(),
    )

    assert market_routing == {"1.259502313": {"222": [(_iid_away(), "yes")]}}
    assert market_to_page_key == {"1.259502313": "2_12597512"}
    assert comp_page_refs == {"2_12597512": ("2", "12597512")}


def test_remove_subscription_state_prunes_empty_market_and_unreferenced_page():
    market_routing = {"1.259502313": {"111": [(_iid(), "yes")]}}
    market_to_page_key = {"1.259502313": "2_12597512"}
    comp_page_refs = {"2_12597512": ("2", "12597512")}

    se_remove_subscription_state(
        market_routing=market_routing,
        market_to_page_key=market_to_page_key,
        comp_page_refs=comp_page_refs,
        instrument_id=_iid(),
    )

    assert market_routing == {}
    assert market_to_page_key == {}
    assert comp_page_refs == {}


def test_remove_subscription_state_keeps_page_ref_when_other_market_uses_same_page():
    market_routing = {
        "1.259502313": {"111": [(_iid(), "yes")]},
        "1.259502314": {"222": [(_iid_away(), "yes")]},
    }
    market_to_page_key = {
        "1.259502313": "2_12597512",
        "1.259502314": "2_12597512",
    }
    comp_page_refs = {"2_12597512": ("2", "12597512")}

    se_remove_subscription_state(
        market_routing=market_routing,
        market_to_page_key=market_to_page_key,
        comp_page_refs=comp_page_refs,
        instrument_id=_iid(),
    )

    assert market_routing == {"1.259502314": {"222": [(_iid_away(), "yes")]}}
    assert market_to_page_key == {"1.259502314": "2_12597512"}
    assert comp_page_refs == {"2_12597512": ("2", "12597512")}


def test_competition_page_ref_from_instrument():
    inst = SimpleNamespace(event_type_id=2, competition_id=12597512)
    assert se_competition_page_ref_from_instrument(inst) == ("2_12597512", "2", "12597512")


def test_competition_page_ref_requires_sport_and_competition():
    assert se_competition_page_ref_from_instrument(SimpleNamespace(event_type_id="", competition_id=1)) is None
    assert se_competition_page_ref_from_instrument(SimpleNamespace(event_type_id=2, competition_id=None)) is None


def test_competition_page_url_strips_trailing_slash():
    assert (
        se_competition_page_url("https://portal.sharpxch.com/", "2", "12597512")
        == "https://portal.sharpxch.com/customer/sport/2/competition/12597512"
    )


def test_should_reopen_missing_page_allows_still_subscribed_missing_page():
    assert se_should_reopen_missing_page(
        page_key="2_12597512",
        market_to_page_key={"1.259502313": "2_12597512"},
        comp_pages={},
    ) is True


def test_should_reopen_missing_page_guards_shutdown_opened_and_unsubscribed():
    common = {
        "page_key": "2_12597512",
        "market_to_page_key": {"1.259502313": "2_12597512"},
    }
    assert se_should_reopen_missing_page(
        **common,
        comp_pages={},
        disconnecting=True,
    ) is False
    assert se_should_reopen_missing_page(
        **common,
        comp_pages={"2_12597512": object()},
    ) is False
    assert se_should_reopen_missing_page(
        page_key="2_12597512",
        market_to_page_key={"1.111": "2_999"},
        comp_pages={},
    ) is False


def test_reopen_missing_page_calls_open_page_when_still_subscribed():
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened", "page_key": kwargs["page_key"]}

    result = asyncio.run(
        se_reopen_missing_page(
            page_key="2_12597512",
            market_to_page_key={"1.259502313": "2_12597512"},
            comp_page_refs={"2_12597512": ("2", "12597512")},
            comp_pages={},
            open_page=open_page,
        ),
    )

    assert result == {"action": "opened", "page_key": "2_12597512"}
    assert calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]


def test_reopen_missing_page_returns_none_when_gate_rejects_or_ref_missing():
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened"}

    assert asyncio.run(
        se_reopen_missing_page(
            page_key="2_12597512",
            market_to_page_key={"1.259502313": "2_12597512"},
            comp_page_refs={"2_12597512": ("2", "12597512")},
            comp_pages={},
            open_page=open_page,
            disconnecting=True,
        ),
    ) is None
    assert asyncio.run(
        se_reopen_missing_page(
            page_key="2_12597512",
            market_to_page_key={"1.259502313": "2_12597512"},
            comp_page_refs={},
            comp_pages={},
            open_page=open_page,
        ),
    ) is None
    assert calls == []


def test_reopen_missing_page_propagates_open_failure():
    async def open_page(**kwargs):
        raise RuntimeError(f"reopen failed: {kwargs['page_key']}")

    with pytest.raises(RuntimeError, match="reopen failed: 2_12597512"):
        asyncio.run(
            se_reopen_missing_page(
                page_key="2_12597512",
                market_to_page_key={"1.259502313": "2_12597512"},
                comp_page_refs={"2_12597512": ("2", "12597512")},
                comp_pages={},
                open_page=open_page,
            ),
        )


def test_ensure_competition_page_returns_already_open_when_page_exists():
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened"}

    result = asyncio.run(
        se_ensure_competition_page(
            plan={"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"},
            comp_pages={"2_12597512": object()},
            open_page=open_page,
        ),
    )

    assert result == {"action": "already_open", "page_key": "2_12597512"}
    assert calls == []


def test_ensure_competition_page_calls_open_page_when_missing():
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened", "page_key": kwargs["page_key"]}

    result = asyncio.run(
        se_ensure_competition_page(
            plan={"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"},
            comp_pages={},
            open_page=open_page,
        ),
    )

    assert result == {"action": "opened", "page_key": "2_12597512"}
    assert calls == [{"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"}]


def test_ensure_competition_page_returns_none_for_bad_plan():
    calls = []

    async def open_page(**kwargs):
        calls.append(kwargs)
        return {"action": "opened"}

    assert asyncio.run(se_ensure_competition_page(plan=None, comp_pages={}, open_page=open_page)) is None
    assert asyncio.run(
        se_ensure_competition_page(
            plan={"page_key": "2_12597512", "sport_id": "", "competition_id": "12597512"},
            comp_pages={},
            open_page=open_page,
        ),
    ) is None
    assert calls == []


def test_ensure_competition_page_propagates_open_failure():
    async def open_page(**kwargs):
        raise RuntimeError(f"open failed: {kwargs['page_key']}")

    with pytest.raises(RuntimeError, match="open failed: 2_12597512"):
        asyncio.run(
            se_ensure_competition_page(
                plan={"page_key": "2_12597512", "sport_id": "2", "competition_id": "12597512"},
                comp_pages={},
                open_page=open_page,
            ),
        )


def test_websocket_summary_counts_types():
    handler = SimpleNamespace(
        get_active_websockets=lambda: [
            {"url": "u1", "type": "prices"},
            {"url": "u2", "type": "orders"},
            {"url": "u3", "type": "prices"},
            {"url": "u4"},
        ],
        get_frame_counts=lambda: {"prices": 3, "orders": 1},
    )
    assert (
        se_websocket_summary(handler)
        == "ws_count=4, ws_types={'prices': 2, 'orders': 1, 'unknown': 1}, frame_counts={'prices': 3, 'orders': 1}"
    )


def test_websocket_summary_handles_empty():
    handler = SimpleNamespace(get_active_websockets=lambda: [])
    assert se_websocket_summary(handler) == "ws_count=0, ws_types={}, frame_counts={}"


def test_open_competition_page_registers_handler_before_goto():
    calls = []
    page = _FakePage(calls)
    browser_manager = _FakeBrowserManager(page)
    comp_pages = {}
    comp_handlers = {}
    captured_prices = []
    captured_disconnects = []

    result = asyncio.run(
        se_open_or_reload_competition_page(
            page_key="2_12597512",
            sport_id="2",
            competition_id="12597512",
            base_url="https://portal.sharpxch.com/",
            browser_manager=browser_manager,
            comp_pages=comp_pages,
            comp_handlers=comp_handlers,
            price_callback=captured_prices.append,
            disconnect_callback=lambda page_key, reason: captured_disconnects.append((page_key, reason)),
            page_timeout=120000,
            handler_factory=_FakeHandler,
        ),
    )

    assert result == {
        "action": "opened",
        "url": "https://portal.sharpxch.com/customer/sport/2/competition/12597512",
        "summary": "ws_count=1, ws_types={'prices': 1}, frame_counts={}",
    }
    assert calls == [
        "handler_start",
        "bring_to_front",
        ("goto", "https://portal.sharpxch.com/customer/sport/2/competition/12597512", "domcontentloaded", 120000),
    ]
    assert browser_manager.created == ["comp-2_12597512"]
    assert comp_pages == {"2_12597512": page}
    handler = comp_handlers["2_12597512"]
    assert handler.price_callbacks == [captured_prices.append]
    assert handler.kwargs == {
        "clock": None,
        "liveness_timeout_secs": None,
        "liveness_name": None,
        "liveness_ws_type": None,
    }
    handler.disconnect_callbacks[0]("close:prices")
    assert captured_disconnects == [("2_12597512", "close:prices")]


def test_open_competition_page_passes_liveness_options_to_handler():
    calls = []
    page = _FakePage(calls)
    clock = object()
    comp_handlers = {}

    asyncio.run(
        se_open_or_reload_competition_page(
            page_key="2_12597512",
            sport_id="2",
            competition_id="12597512",
            base_url="https://portal.sharpxch.com/",
            browser_manager=_FakeBrowserManager(page),
            comp_pages={},
            comp_handlers=comp_handlers,
            price_callback=lambda message: None,
            clock=clock,
            liveness_timeout_secs=300.0,
            liveness_name="se_comp_ws_liveness:2_12597512",
            liveness_ws_type="prices",
            handler_factory=_FakeHandler,
        ),
    )

    assert comp_handlers["2_12597512"].kwargs == {
        "clock": clock,
        "liveness_timeout_secs": 300.0,
        "liveness_name": "se_comp_ws_liveness:2_12597512",
        "liveness_ws_type": "prices",
    }


def test_reload_competition_page_reuses_existing_page():
    calls = []
    page = _FakePage(calls)
    handler = _FakeHandler(page)
    result = asyncio.run(
        se_open_or_reload_competition_page(
            page_key="2_12597512",
            sport_id="2",
            competition_id="12597512",
            base_url="https://portal.sharpxch.com",
            browser_manager=_FakeBrowserManager(page),
            comp_pages={"2_12597512": page},
            comp_handlers={"2_12597512": handler},
            price_callback=lambda message: None,
            page_timeout=120000,
            handler_factory=_FakeHandler,
        ),
    )

    assert result["action"] == "reloaded"
    assert result["summary"] == "ws_count=1, ws_types={'prices': 1}, frame_counts={}"
    assert calls == ["bring_to_front", ("reload", "domcontentloaded", 120000)]


def test_open_competition_page_cleans_up_on_goto_failure():
    calls = []
    page = _FakePage(calls, fail_goto=True)
    browser_manager = _FakeBrowserManager(page)
    comp_pages = {}
    comp_handlers = {}

    with pytest.raises(RuntimeError, match="goto failed"):
        asyncio.run(
            se_open_or_reload_competition_page(
                page_key="2_12597512",
                sport_id="2",
                competition_id="12597512",
                base_url="https://portal.sharpxch.com",
                browser_manager=browser_manager,
                comp_pages=comp_pages,
                comp_handlers=comp_handlers,
                price_callback=lambda message: None,
                handler_factory=_FakeHandler,
            ),
        )

    assert calls == [
        "handler_start",
        "bring_to_front",
        ("goto", "https://portal.sharpxch.com/customer/sport/2/competition/12597512", "domcontentloaded", 120000),
        "handler_stop",
    ]
    assert browser_manager.closed == ["comp-2_12597512"]
    assert comp_pages == {}
    assert comp_handlers == {}


def test_should_reload_on_disconnect_allows_prices_close_and_updates_cooldown():
    last_reload = {}
    out = se_should_reload_on_disconnect(
        page_key="2_12597512",
        reason="close:prices",
        comp_pages={"2_12597512": object()},
        comp_reloading=set(),
        comp_last_reload_ns=last_reload,
        now_ns=1000,
        cooldown_ns=100,
    )
    assert out is True
    assert last_reload == {"2_12597512": 1000}


def test_should_reload_on_disconnect_allows_liveness_timeout():
    assert se_should_reload_on_disconnect(
        page_key="2_12597512",
        reason="liveness_timeout",
        comp_pages={"2_12597512": object()},
        comp_reloading=set(),
        comp_last_reload_ns={},
        now_ns=1000,
        cooldown_ns=100,
    ) is True


def test_should_reload_on_disconnect_ignores_non_price_reason():
    last_reload = {}
    assert se_should_reload_on_disconnect(
        page_key="2_12597512",
        reason="close:orders",
        comp_pages={"2_12597512": object()},
        comp_reloading=set(),
        comp_last_reload_ns=last_reload,
        now_ns=1000,
        cooldown_ns=100,
    ) is False
    assert last_reload == {}


def test_should_reload_on_disconnect_guards_state():
    common = {
        "page_key": "2_12597512",
        "reason": "close:prices",
        "now_ns": 1000,
        "cooldown_ns": 100,
    }
    assert se_should_reload_on_disconnect(
        **common,
        comp_pages={"2_12597512": object()},
        comp_reloading=set(),
        comp_last_reload_ns={},
        disconnecting=True,
    ) is False
    assert se_should_reload_on_disconnect(
        **common,
        comp_pages={"2_12597512": object()},
        comp_reloading={"2_12597512"},
        comp_last_reload_ns={},
    ) is False
    assert se_should_reload_on_disconnect(
        **common,
        comp_pages={},
        comp_reloading=set(),
        comp_last_reload_ns={},
    ) is False


def test_should_reload_on_disconnect_respects_cooldown():
    last_reload = {"2_12597512": 950}
    assert se_should_reload_on_disconnect(
        page_key="2_12597512",
        reason="close:prices",
        comp_pages={"2_12597512": object()},
        comp_reloading=set(),
        comp_last_reload_ns=last_reload,
        now_ns=1000,
        cooldown_ns=100,
    ) is False
    assert last_reload == {"2_12597512": 950}


def test_reload_competition_on_disconnect_reloads_existing_page():
    calls = []
    page = _FakePage(calls)
    comp_reloading = set()
    last_reload = {}

    result = asyncio.run(
        se_reload_competition_on_disconnect(
            page_key="2_12597512",
            reason="close:prices",
            comp_page_refs={"2_12597512": ("2", "12597512")},
            base_url="https://portal.sharpxch.com",
            browser_manager=_FakeBrowserManager(page),
            comp_pages={"2_12597512": page},
            comp_handlers={"2_12597512": _FakeHandler(page)},
            comp_reloading=comp_reloading,
            comp_last_reload_ns=last_reload,
            now_ns=1000,
            cooldown_ns=100,
            price_callback=lambda message: None,
            handler_factory=_FakeHandler,
        ),
    )

    assert result["action"] == "reloaded"
    assert calls == ["bring_to_front", ("reload", "domcontentloaded", 120000)]
    assert comp_reloading == set()
    assert last_reload == {"2_12597512": 1000}


def test_reload_competition_on_disconnect_skips_when_gate_rejects():
    calls = []
    page = _FakePage(calls)
    comp_reloading = set()
    last_reload = {}

    result = asyncio.run(
        se_reload_competition_on_disconnect(
            page_key="2_12597512",
            reason="close:orders",
            comp_page_refs={"2_12597512": ("2", "12597512")},
            base_url="https://portal.sharpxch.com",
            browser_manager=_FakeBrowserManager(page),
            comp_pages={"2_12597512": page},
            comp_handlers={"2_12597512": _FakeHandler(page)},
            comp_reloading=comp_reloading,
            comp_last_reload_ns=last_reload,
            now_ns=1000,
            cooldown_ns=100,
            price_callback=lambda message: None,
            handler_factory=_FakeHandler,
        ),
    )

    assert result is None
    assert calls == []
    assert comp_reloading == set()
    assert last_reload == {}


def test_reload_competition_on_disconnect_clears_reloading_on_failure():
    calls = []
    page = _FakePage(calls)
    comp_reloading = set()
    last_reload = {}

    async def fail_reload(*, wait_until, timeout):
        calls.append(("reload", wait_until, timeout))
        raise RuntimeError("reload failed")

    page.reload = fail_reload

    with pytest.raises(RuntimeError, match="reload failed"):
        asyncio.run(
            se_reload_competition_on_disconnect(
                page_key="2_12597512",
                reason="liveness_timeout",
                comp_page_refs={"2_12597512": ("2", "12597512")},
                base_url="https://portal.sharpxch.com",
                browser_manager=_FakeBrowserManager(page),
                comp_pages={"2_12597512": page},
                comp_handlers={"2_12597512": _FakeHandler(page)},
                comp_reloading=comp_reloading,
                comp_last_reload_ns=last_reload,
                now_ns=1000,
                cooldown_ns=100,
                price_callback=lambda message: None,
                handler_factory=_FakeHandler,
            ),
        )

    assert calls == ["bring_to_front", ("reload", "domcontentloaded", 120000)]
    assert comp_reloading == set()
    assert last_reload == {"2_12597512": 1000}


def test_reload_competition_on_disconnect_requires_page_ref():
    calls = []
    page = _FakePage(calls)
    last_reload = {}
    result = asyncio.run(
        se_reload_competition_on_disconnect(
            page_key="2_12597512",
            reason="close:prices",
            comp_page_refs={},
            base_url="https://portal.sharpxch.com",
            browser_manager=_FakeBrowserManager(page),
            comp_pages={"2_12597512": page},
            comp_handlers={"2_12597512": _FakeHandler(page)},
            comp_reloading=set(),
            comp_last_reload_ns=last_reload,
            now_ns=1000,
            cooldown_ns=100,
            price_callback=lambda message: None,
            handler_factory=_FakeHandler,
        ),
    )

    assert result is None
    assert calls == []
    assert last_reload == {}


def test_price_message_to_book_deltas_routes_subscribed_runners():
    message = {
        "id": "1.259502313",
        "rc": [
            {
                "id": 111,
                "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}],
                "bdatl": [{"index": 0, "odds": 2.1, "amount": 5.0}],
            },
            {"id": 222, "bdatb": [{"index": 0, "odds": 1.9, "amount": 7.0}], "bdatl": []},
        ],
    }
    routing = {"111": [(_iid(), "yes")], "222": [(_iid_away(), "yes")]}

    out = se_price_message_to_book_deltas(message, routing, ts_init_ns=1000)

    assert len(out) == 2
    assert all(isinstance(item, OrderBookDeltas) for item in out)
    assert out[0].instrument_id == _iid()
    assert out[1].instrument_id == _iid_away()
    assert len(list(out[0].deltas)) == 3
    assert len(list(out[1].deltas)) == 2


def test_market_price_message_to_book_deltas_routes_by_market_id():
    message = {
        "id": "1.259502313",
        "marketDefinition": {"inPlay": True},
        "rc": [
            {
                "id": 111,
                "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}],
                "bdatl": [{"index": 0, "odds": 2.1, "amount": 5.0}],
            },
            {"id": 222, "bdatb": [{"index": 0, "odds": 1.9, "amount": 7.0}], "bdatl": []},
            {"id": 333, "bdatb": [{"index": 0, "odds": 1.8, "amount": 3.0}], "bdatl": []},
        ],
    }
    market_routing = {"1.259502313": {"111": [(_iid(), "yes")], "222": [(_iid_away(), "yes")]}}

    out = se_market_price_message_to_book_deltas(message, market_routing, ts_init_ns=1000)

    assert out["market_id"] == "1.259502313"
    assert out["in_play"] is True
    assert out["runners"] == 3
    assert out["subscribed_selections"] == 2
    assert [item.instrument_id for item in out["deltas"]] == [_iid(), _iid_away()]


def test_market_price_message_to_book_deltas_returns_none_for_unrouted_or_bad_message():
    message = {
        "id": "1.259502313",
        "rc": [{"id": 111, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}], "bdatl": []}],
    }

    assert se_market_price_message_to_book_deltas(message, {}, ts_init_ns=1000) is None
    assert se_market_price_message_to_book_deltas({}, {"1.259502313": {"111": [(_iid(), "yes")]}}, ts_init_ns=1000) is None


def test_market_price_message_to_book_deltas_keeps_frame_metadata_when_no_deltas():
    message = {
        "id": "1.259502313",
        "marketDefinition": {"inPlay": False},
        "rc": [{"id": 111, "bdatb": [], "bdatl": []}],
    }

    out = se_market_price_message_to_book_deltas(message, {"1.259502313": {"111": [(_iid(), "yes")]}}, ts_init_ns=1000)

    assert out["market_id"] == "1.259502313"
    assert out["in_play"] is False
    assert out["runners"] == 1
    assert out["subscribed_selections"] == 1
    assert out["deltas"] == []


def test_publish_routed_book_deltas_publishes_and_writes_in_play():
    message = {
        "id": "1.259502313",
        "marketDefinition": {"inPlay": True},
        "rc": [
            {"id": 111, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}], "bdatl": []},
            {"id": 222, "bdatb": [{"index": 0, "odds": 1.9, "amount": 7.0}], "bdatl": []},
        ],
    }
    routed = se_market_price_message_to_book_deltas(
        message,
        {"1.259502313": {"111": [(_iid(), "yes")], "222": [(_iid_away(), "yes")]}},
        ts_init_ns=1000,
    )
    published = []
    in_play_writes = []

    count = se_publish_routed_book_deltas(
        routed,
        published.append,
        write_in_play=lambda instrument_id, in_play: in_play_writes.append((instrument_id, in_play)),
    )

    assert count == 2
    assert [item.instrument_id for item in published] == [_iid(), _iid_away()]
    assert in_play_writes == [(_iid(), True), (_iid_away(), True)]


def test_publish_routed_book_deltas_handles_empty_payloads():
    published = []
    in_play_writes = []
    assert se_publish_routed_book_deltas(None, published.append) == 0
    assert se_publish_routed_book_deltas({"deltas": [], "in_play": True}, published.append) == 0
    assert se_publish_routed_book_deltas(
        {"deltas": [], "in_play": True},
        published.append,
        write_in_play=lambda instrument_id, in_play: in_play_writes.append((instrument_id, in_play)),
    ) == 0
    assert published == []
    assert in_play_writes == []


def test_handle_price_frame_routes_publishes_and_returns_summary():
    message = {
        "id": "1.259502313",
        "marketDefinition": {"inPlay": True},
        "rc": [
            {"id": 111, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}], "bdatl": []},
            {"id": 222, "bdatb": [{"index": 0, "odds": 1.9, "amount": 7.0}], "bdatl": []},
        ],
    }
    published = []
    in_play_writes = []

    out = se_handle_price_frame(
        message,
        {"1.259502313": {"111": [(_iid(), "yes")], "222": [(_iid_away(), "yes")]}},
        1000,
        published.append,
        write_in_play=lambda instrument_id, in_play: in_play_writes.append((instrument_id, in_play)),
    )

    assert out["market_id"] == "1.259502313"
    assert out["published_count"] == 2
    assert out["subscribed_selections"] == 2
    assert [item.instrument_id for item in published] == [_iid(), _iid_away()]
    assert in_play_writes == [(_iid(), True), (_iid_away(), True)]


def test_handle_price_frame_returns_none_for_unrouted_frame():
    published = []
    out = se_handle_price_frame(
        {"id": "1.259502313", "rc": [{"id": 111, "bdatb": [{"index": 0, "odds": 2.0, "amount": 10.0}], "bdatl": []}]},
        {},
        1000,
        published.append,
    )
    assert out is None
    assert published == []


def test_handle_price_frame_returns_summary_without_publish_for_empty_book():
    published = []
    out = se_handle_price_frame(
        {"id": "1.259502313", "marketDefinition": {"inPlay": False}, "rc": [{"id": 111, "bdatb": [], "bdatl": []}]},
        {"1.259502313": {"111": [(_iid(), "yes")]}},
        1000,
        published.append,
    )
    assert out["market_id"] == "1.259502313"
    assert out["published_count"] == 0
    assert out["deltas"] == []
    assert published == []


def test_price_message_to_book_deltas_skips_unsubscribed_and_empty_runners():
    message = {
        "id": "1.259502313",
        "rc": [
            {"id": 111, "bdatb": {}, "bdatl": {}},
            {"id": 222, "bdatb": [{"index": 0, "odds": 1.9, "amount": 7.0}], "bdatl": []},
            {"id": 333, "bdatb": [{"index": 0, "odds": 1.8, "amount": 7.0}], "bdatl": []},
        ],
    }
    out = se_price_message_to_book_deltas(message, {"222": [(_iid_away(), "yes")]}, ts_init_ns=1000)
    assert len(out) == 1
    assert out[0].instrument_id == _iid_away()


def test_price_message_to_book_deltas_returns_empty_for_bad_message():
    assert se_price_message_to_book_deltas({}, {"111": [(_iid(), "yes")]}, ts_init_ns=1000) == []


def test_runner_to_book_deltas_clears_then_adds_both_sides():
    runner = {
        "selection_id": "111",
        "back": [{"price": 2.0, "size": 10}, {"price": 1.99, "size": 7}],
        "lay": [{"price": 2.1, "size": 5}, {"price": 2.08, "size": 3}],
    }
    out = se_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000)
    assert isinstance(out, OrderBookDeltas)
    deltas = list(out.deltas)
    assert len(deltas) == 3
    assert deltas[0].action == BookAction.CLEAR
    # back → SELL (卖方出价 = asks),decimal odds 取最高赔率。
    assert deltas[1].order.side == OrderSide.SELL
    assert float(deltas[1].order.price) == pytest.approx(2.0)
    # lay → BUY (买方出价 = bids),decimal odds 取最低赔率。
    assert deltas[2].order.side == OrderSide.BUY
    assert float(deltas[2].order.price) == pytest.approx(2.08)


def test_runner_to_book_deltas_makes_nt_best_prices_match_back_and_lay_top():
    runner = {
        "back": [{"price": 1.85, "size": 10}, {"price": 1.82, "size": 20}],
        "lay": [{"price": 1.90, "size": 30}, {"price": 1.88, "size": 40}],
    }
    deltas = se_runner_to_book_deltas(_iid(), runner, ts_init_ns=1000)
    book = OrderBook(_iid(), BookType.L2_MBP)
    book.apply_deltas(deltas)

    assert float(book.best_ask_price()) == pytest.approx(1.85)
    assert float(book.best_bid_price()) == pytest.approx(1.88)


def test_runner_to_book_deltas_returns_none_when_empty():
    assert se_runner_to_book_deltas(_iid(), {"back": [], "lay": []}, ts_init_ns=1) is None
    assert se_runner_to_book_deltas(_iid(), {}, ts_init_ns=1) is None


def test_runner_to_book_deltas_skips_zero_or_invalid_sizes():
    runner = {
        "back": [{"price": 2.0, "size": 0}, {"price": 0, "size": 10}],
        "lay": [{"price": 2.1, "size": -1}],
    }
    assert se_runner_to_book_deltas(_iid(), runner, ts_init_ns=1) is None
