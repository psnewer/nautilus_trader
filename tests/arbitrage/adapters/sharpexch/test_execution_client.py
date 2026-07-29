"""SharpExchExecutionClient 离线边界测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.sharpexch.config import SharpExchExecClientConfig
from nautilus_trader.adapters.sharpexch.execution import SharpExchExecutionClient
from nautilus_trader.adapters.sharpexch.providers import SharpExchInstrumentProvider
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness

from tests.arbitrage.adapters.sharpexch.test_provider import _event


def _client(
    *,
    liveness=None,
    browser_manager=None,
    browser_lock=None,
    config=None,
    market_order_enabled=False,
):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    return SharpExchExecutionClient(
        loop=asyncio.new_event_loop(),
        browser_manager=browser_manager,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=config or SharpExchExecClientConfig(username="u", password="p"),
        venue_liveness=liveness,
        browser_lock=browser_lock,
        market_order_enabled=market_order_enabled,
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _instrument(role="home"):
    provider = SharpExchInstrumentProvider(SimpleNamespace())
    return next(inst for inst in provider._build_legs(_event()) if inst.info["selection_role"] == role)


def _order(client, inst, *, qty=7.0, price=1.01, side=OrderSide.BUY):
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=client._clock,
    )
    return factory.limit(inst.id, side, inst.make_qty(qty), inst.make_price(price))


class FakeSharpExchPage:
    def __init__(self):
        self.url = ""
        self.events = []
        self.default_timeout = None
        self.listeners = {}

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout
        self.events.append(("timeout", timeout))

    def on(self, event, callback):
        self.listeners[event] = callback
        self.events.append(("on", event))

    def remove_listener(self, event, callback):
        self.listeners.pop(event, None)
        self.events.append(("remove_listener", event))

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.events.append(("goto", url, wait_until, timeout))
        self.url = "https://portal.sharpxch.com/customer/"

    async def reload(self, *, wait_until=None, timeout=None):
        self.events.append(("reload", wait_until, timeout))


class FakeBrowserManager:
    def __init__(self):
        self.started = False
        self.page = FakeSharpExchPage()
        self.created = []

    async def start(self):
        self.started = True

    async def create_page(self, name):
        self.created.append(name)
        return self.page


def test_execution_client_connects_with_fake_page_and_registers_ws_before_navigation():
    browser = FakeBrowserManager()
    client = _client(browser_manager=browser)
    client._connect_ready_timeout_secs = 0.001
    captured = {}
    client.generate_account_state = lambda *, balances, margins, reported, ts_event, info=None: captured.update(
        balances=balances,
        reported=reported,
    )

    assert str(client.venue) == "SHARPEXCH"
    assert str(client.account_id) == "SHARPEXCH-001"

    _run(client._connect())

    assert browser.started is True
    assert browser.created == ["execution"]
    assert browser.page.default_timeout == client._config.page_timeout
    assert browser.page.events[0] == ("timeout", client._config.page_timeout)
    assert browser.page.events[1] == ("on", "websocket")
    assert browser.page.events[2] == ("on", "response")
    assert browser.page.events[3][0] == "goto"
    assert captured["reported"] is True
    assert captured["balances"][0].total.as_double() == pytest.approx(0.0)

    _run(client._disconnect())

    assert ("remove_listener", "websocket") in browser.page.events
    assert ("remove_listener", "response") in browser.page.events
    assert client._page is None
    assert client._ws_handler is None


def test_execution_client_connect_delegates_login_to_helper():
    class Lock:
        def __init__(self):
            self.events = []

        async def __aenter__(self):
            self.events.append("enter")

        async def __aexit__(self, exc_type, exc, tb):
            self.events.append("exit")

    lock = Lock()
    browser = FakeBrowserManager()
    client = _client(browser_manager=browser, browser_lock=lock)
    client._connect_ready_timeout_secs = 0.001

    async def login():
        lock.events.append("login")

    client._login = login
    client.generate_account_state = lambda **kwargs: None

    _run(client._connect())

    assert lock.events == ["login"]


def test_execution_client_connect_waits_for_balance_and_current_bets_signals():
    class Response:
        url = "https://portal.sharpxch.com/customer/api/profile"

        async def json(self):
            return {"balance": "42.50"}

    browser = FakeBrowserManager()
    client = _client(browser_manager=browser)
    captured = {}
    client.generate_account_state = lambda *, balances, margins, reported, ts_event, info=None: captured.update(
        balances=balances,
        reported=reported,
    )

    async def login():
        await client._on_response(Response())
        client._on_current_bets([])

    client._login = login

    _run(client._connect())

    assert captured["reported"] is True
    assert captured["balances"][0].total.as_double() == pytest.approx(42.50)
    assert client._balance_seen is True
    assert client._last_current_bets_ns > 0


def test_on_response_keeps_updating_profile_balance_after_startup():
    class Response:
        url = "https://portal.sharpxch.com/customer/api/profile"

        def __init__(self, balance):
            self._balance = balance

        async def json(self):
            return {"balance": str(self._balance)}

    client = _client()
    captured_balances = []

    def capture_account_state(*, balances, margins, reported, ts_event, info=None):
        captured_balances.append(balances[0].total.as_double())

    client.generate_account_state = capture_account_state

    _run(client._on_response(Response(42.5)))
    _run(client._on_response(Response(40.0)))

    assert captured_balances == pytest.approx([42.5, 40.0])


def test_on_response_ignores_duplicate_profile_balance():
    class Response:
        url = "https://portal.sharpxch.com/customer/api/profile"

        async def json(self):
            return {"balance": "42.50"}

    client = _client()
    calls = []
    client.generate_account_state = lambda **kwargs: calls.append(kwargs)

    _run(client._on_response(Response()))
    _run(client._on_response(Response()))

    assert len(calls) == 1


def test_on_general_frame_balance_is_ignored():
    """WS BALANCE 帧不再消费(实测返回 0.00 不可靠),余额改从 HTTP 响应拦截。"""
    client = _client()
    calls = []
    client.generate_account_state = lambda **kwargs: calls.append(kwargs)

    client._on_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})

    assert calls == []


def test_arbitrage_fx_command_does_not_change_se_usd_fx():
    client = _client()
    client._msgbus.publish(topic=TOPIC_ARBITRAGE_PARAMS, msg=SetArbitrageParamsCommand(fx=1.3))
    assert client._current_fx() == pytest.approx(1.0)


def test_on_general_frame_unknown_ignored():
    client = _client()
    calls = []
    client.generate_account_state = lambda **kwargs: calls.append(kwargs)

    client._on_general_frame({"SOMETHING": 1})

    assert calls == []


def test_current_bets_snapshot_marks_liveness_and_keeps_usd_amounts():
    liveness = VenueExecutionLiveness()
    client = _client(liveness=liveness)
    client._fx = 1.25

    client._on_general_frame(
        {
            "CURRENT_BETS": [
                {
                    "offerId": "SE-1",
                    "marketId": "M1",
                    "selectionId": "111",
                    "side": "BACK",
                    "sizePlaced": "10.00",
                    "sizeMatched": "0.00",
                    "sizeRemaining": "10.00",
                    "averagePrice": "0",
                    "price": "2.0",
                },
            ],
        },
    )

    assert client._current_bets["SE-1"]["sizePlaced"] == pytest.approx(10.0)
    assert client._current_bets_frames_seen == 1
    assert client._last_current_bets_ns > 0
    assert liveness.order_alive("SHARPEXCH") is True
    assert liveness.position_alive("SHARPEXCH") is True


def test_current_bets_matched_fires_generate_order_filled():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=7.0, price=1.01)
    client._cache.add_order(order)
    voi = VenueOrderId("222016509")
    client._cache.add_venue_order_id(order.client_order_id, voi)
    captured = []
    client.generate_order_filled = lambda **kwargs: captured.append(kwargs)

    client._on_current_bets(
        [
            {
                "offerId": "222016509",
                "marketId": inst.market_id,
                "selectionId": str(inst.selection_id),
                "side": "BACK",
                "sizePlaced": "7.00",
                "sizeMatched": "7.00",
                "sizeRemaining": "0.00",
                "averagePrice": "2.3",
                "price": "1.01",
            },
        ],
    )

    assert len(captured) == 1
    fill = captured[0]
    assert fill["venue_order_id"] == voi
    assert fill["last_qty"].as_double() == pytest.approx(7.0)
    assert fill["last_px"].as_double() == pytest.approx(2.3)
    assert fill["liquidity_side"] == LiquiditySide.MAKER


def test_submit_order_success_registers_pending_accept_not_immediate_ack():
    """ack 不再由 place 回执触发(见 `_on_current_bets`);回执成功只登记待确认表。"""
    client = _client()
    events = {}
    client._begin_session = lambda command: True
    client.generate_order_accepted = lambda **kwargs: events.update(accepted=kwargs)
    client.generate_order_rejected = lambda **kwargs: events.update(rejected=kwargs)

    async def place(order):
        return {"success": True, "venue_order_id": "SE-OFFER-1", "message": "ok"}

    client._place_via_executor = place
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id="I",
        client_order_id=ClientOrderId("COID-1"),
    )

    _run(client._submit_order(SimpleNamespace(order=order)))

    assert "accepted" not in events
    assert "rejected" not in events
    assert client._pending_accept["SE-OFFER-1"] == ClientOrderId("COID-1")
    assert "rejected" not in events


def test_place_via_executor_translates_nt_order_and_passes_page():
    client = _client()
    inst = _instrument("away")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.5, price=2.34)
    page = object()
    captured = {}
    calls = []

    class Executor:
        async def place_order(self, legacy_order, passed_page):
            calls.append("request")
            captured.update(legacy_order=legacy_order, page=passed_page)
            return {"success": True, "venue_order_id": "SE-OFFER-1", "message": "ok"}

    client._executor = Executor()
    client._page = page
    client.generate_order_submitted = lambda **kwargs: calls.append("submitted")

    result = _run(client._place_via_executor(order))

    assert result["success"] is True
    legacy = captured["legacy_order"]
    assert legacy.venue == "sharpexch"
    assert legacy.market_id == inst.market_id
    assert legacy.selection_id == str(inst.selection_id)
    assert legacy.side == "BACK"
    assert legacy.size == pytest.approx(12.5)
    assert legacy.price == pytest.approx(2.34)
    assert captured["page"] is page
    assert calls == ["submitted", "request"]


def test_place_via_executor_market_lay_uses_worst_book_price():
    from nautilus_trader.adapters.sharpexch.data import se_runner_to_book_deltas
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType

    client = _client(market_order_enabled=True)
    inst = _instrument("away")
    client._cache.add_instrument(inst)
    book = OrderBook(inst.id, BookType.L2_MBP)
    book.apply_deltas(
        se_runner_to_book_deltas(
            inst.id,
            {
                "back": [{"price": 2.1, "size": 10}],
                "lay": [{"price": 2.0, "size": 10}, {"price": 4.0, "size": 10}],
            },
            1,
        ),
    )
    client._cache.add_order_book(book)
    order = _order(client, inst, qty=12.0, price=2.0, side=OrderSide.SELL)
    captured = {}

    class Executor:
        async def place_order(self, legacy_order, passed_page):
            captured["order"] = legacy_order
            return {"success": True, "venue_order_id": "SE-OFFER-1", "message": "ok"}

    client._executor = Executor()
    client._page = object()
    client.generate_order_submitted = lambda **kwargs: None

    _run(client._place_via_executor(order))

    assert captured["order"].price == pytest.approx(4.0)


def test_place_via_executor_timeout_releases_page_lock():
    client = _client()
    client._order_io_timeout_secs = 0.001
    inst = _instrument("away")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.5, price=2.34)

    class Executor:
        async def place_order(self, legacy_order, passed_page):
            await asyncio.Event().wait()

    client._executor = Executor()
    client._page = object()
    client.generate_order_submitted = lambda **kwargs: None

    with pytest.raises(asyncio.TimeoutError):
        _run(client._place_via_executor(order))
    assert not client._page_lock.locked()


def test_submit_order_rejects_explicit_failure_but_keeps_transport_exception_inflight():
    client = _client()
    events = {}
    ended = []
    client._begin_session = lambda command: True
    client.generate_order_rejected = lambda *, strategy_id, instrument_id, client_order_id, reason, ts_event: events.update(
        reason=reason,
    )
    client._end_session = ended.append

    async def fail(order):
        return {"success": False, "message": "venue rejected"}

    client._place_via_executor = fail
    order = SimpleNamespace(strategy_id="S", instrument_id="I", client_order_id=ClientOrderId("COID-1"))
    _run(client._submit_order(SimpleNamespace(order=order)))
    assert events["reason"] == "venue rejected"
    assert ended == []

    async def boom(order):
        raise TimeoutError("page crashed")

    client._place_via_executor = boom
    _run(client._submit_order(SimpleNamespace(order=order)))
    assert events["reason"] == "venue rejected"
    assert ended == []


def test_submit_order_transport_result_keeps_inflight_session():
    client = _client()
    rejected = []
    client._begin_session = lambda command: True
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    async def unknown(order):
        return {
            "success": False,
            "message": "connection reset",
            "transport_unknown": True,
        }

    client._place_via_executor = unknown
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id="I",
        client_order_id=ClientOrderId("COID-1"),
    )
    _run(client._submit_order(SimpleNamespace(order=order)))

    assert rejected == []


def test_submit_order_cancel_only_discards():
    """#261:cancel-only 判定挪到**同步** `submit_order`(session 必须同步建立)。"""
    from unittest.mock import patch
    from nautilus_trader.live.execution_client import LiveExecutionClient

    client = _client()
    placed = []
    dispatched = []
    client._begin_session = lambda command: False
    client._place_via_executor = lambda order: placed.append(order)

    with patch.object(LiveExecutionClient, "submit_order",
                      lambda self, cmd: dispatched.append(cmd)):
        client.submit_order(SimpleNamespace(order=SimpleNamespace()))

    assert dispatched == []                          # 未下发给 NT
    assert placed == []


def test_submit_order_builds_session_before_dispatch():
    """#261 承重前提:session 同步建立后才交 NT create_task,顺序不可颠倒。"""
    from unittest.mock import patch
    from nautilus_trader.live.execution_client import LiveExecutionClient

    client = _client()
    order_seq = []
    client._begin_session = lambda command: order_seq.append("session") or True

    with patch.object(LiveExecutionClient, "submit_order",
                      lambda self, cmd: order_seq.append("dispatch")):
        client.submit_order(SimpleNamespace(order=SimpleNamespace()))

    assert order_seq == ["session", "dispatch"]


def test_cancel_order_uses_instrument_market_id_and_accepts_success():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.0, price=1.01)
    client._cache.add_order(order)
    voi = VenueOrderId("SE-OFFER-1")
    client._cache.add_venue_order_id(order.client_order_id, voi)
    captured = {}

    class Executor:
        async def cancel_order(self, market_id, venue_order_id, page, *, bet=None):
            captured.update(market_id=market_id, venue_order_id=venue_order_id, bet=bet)
            return {"success": True, "message": "ok"}

    client._executor = Executor()
    client.generate_order_canceled = lambda *args, **kwargs: captured.update(canceled=True)

    _run(
        client._cancel_order(
            SimpleNamespace(
                strategy_id="S",
                instrument_id=inst.id,
                client_order_id=order.client_order_id,
                venue_order_id=voi,
            ),
        ),
    )

    assert captured["market_id"] == inst.market_id
    assert captured["venue_order_id"] == "SE-OFFER-1"
    assert captured["bet"] == {}
    assert "canceled" not in captured

    client._on_current_bets([])
    assert captured["canceled"] is True


def test_cancel_order_transport_failure_keeps_pending_cancel():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.0, price=1.01)
    client._cache.add_order(order)
    voi = VenueOrderId("SE-OFFER-1")
    client._cache.add_venue_order_id(order.client_order_id, voi)
    rejected = []

    class Executor:
        async def cancel_order(self, market_id, venue_order_id, page, *, bet=None):
            return {
                "success": False,
                "message": "connection reset",
                "transport_unknown": True,
            }

    client._executor = Executor()
    client._begin_cancel_session = lambda nt_order: True
    client.generate_order_cancel_rejected = lambda *args, **kwargs: rejected.append((args, kwargs))

    _run(client._cancel_order(SimpleNamespace(
        strategy_id=order.strategy_id,
        instrument_id=inst.id,
        client_order_id=order.client_order_id,
        venue_order_id=voi,
    )))

    assert rejected == []


def test_cancel_residual_one_reuses_normal_cancel_path():
    client = _client()
    captured = {}

    async def cancel_one(strategy_id, instrument_id, client_order_id, venue_order_id, *, session_started=False):
        captured.update(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            session_started=session_started,
        )

    client._cancel_one = cancel_one
    residual = SimpleNamespace(
        strategy_id=StrategyId("S-000"),
        instrument_id="I",
        client_order_id=ClientOrderId("COID-1"),
        venue_order_id=VenueOrderId("SE-OFFER-1"),
    )

    _run(client._cancel_residual_one(residual))

    assert captured == {
        "strategy_id": residual.strategy_id,
        "instrument_id": "I",
        "client_order_id": residual.client_order_id,
        "venue_order_id": residual.venue_order_id,
        "session_started": True,
    }


def test_modify_order_is_rejected():
    client = _client()
    rejected = {}
    client.generate_order_modify_rejected = lambda *, strategy_id, instrument_id, client_order_id, venue_order_id, reason, ts_event: rejected.update(
        reason=reason,
    )

    _run(
        client._modify_order(
            SimpleNamespace(
                strategy_id="S",
                instrument_id="I",
                client_order_id=ClientOrderId("COID-1"),
                venue_order_id=None,
            ),
        ),
    )

    assert "does not support" in rejected["reason"]


def test_order_status_reports_from_current_bets_snapshot():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=7.0, price=1.01)
    client._cache.add_order(order)
    voi = VenueOrderId("SE-OFFER-1")
    client._cache.add_venue_order_id(order.client_order_id, voi)
    client._current_bets = {
        "SE-OFFER-1": {
            "offerId": "SE-OFFER-1",
            "marketId": inst.market_id,
            "selectionId": str(inst.selection_id),
            "side": "BACK",
            "sizePlaced": "7.00",
            "sizeMatched": "3.00",
            "sizeRemaining": "4.00",
            "averagePrice": "2.3",
            "price": "1.01",
        },
    }
    client._last_current_bets_ns = client._clock.timestamp_ns()
    client._mark_exec_frame()

    reports = _run(client.generate_order_status_reports(SimpleNamespace()))

    assert len(reports) == 1
    report = reports[0]
    assert report.venue_order_id == voi
    assert report.client_order_id == order.client_order_id
    assert report.order_status == OrderStatus.PARTIALLY_FILLED
    assert report.filled_qty.as_double() == pytest.approx(3.0)
    assert float(report.avg_px) == pytest.approx(2.3)


def test_single_order_status_report_filters_by_venue_order_id():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._current_bets = {
        "A": {
            "offerId": "A",
            "marketId": inst.market_id,
            "selectionId": str(inst.selection_id),
            "side": "BACK",
            "sizePlaced": "7.00",
            "sizeMatched": "0.00",
            "sizeRemaining": "7.00",
            "averagePrice": "0",
            "price": "1.50",
        },
        "B": {
            "offerId": "B",
            "marketId": inst.market_id,
            "selectionId": str(inst.selection_id),
            "side": "BACK",
            "sizePlaced": "5.00",
            "sizeMatched": "5.00",
            "sizeRemaining": "0.00",
            "averagePrice": "1.8",
            "price": "1.80",
        },
    }
    client._last_current_bets_ns = client._clock.timestamp_ns()
    client._mark_exec_frame()

    report = _run(
        client.generate_order_status_report(
            SimpleNamespace(venue_order_id=VenueOrderId("B"), client_order_id=None),
        ),
    )

    assert report.venue_order_id == VenueOrderId("B")
    assert report.order_status == OrderStatus.FILLED


def test_position_status_reports_aggregate_current_bets():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    client._current_bets = {
        "A": {
            "offerId": "A",
            "marketId": inst.market_id,
            "selectionId": str(inst.selection_id),
            "side": "BACK",
            "sizeMatched": "10.00",
            "averagePrice": "2.0",
            "sizeRemaining": "0.00",
        },
        "B": {
            "offerId": "B",
            "marketId": inst.market_id,
            "selectionId": str(inst.selection_id),
            "side": "BACK",
            "sizeMatched": "5.00",
            "averagePrice": "2.2",
            "sizeRemaining": "0.00",
        },
    }
    client._last_current_bets_ns = client._clock.timestamp_ns()
    client._mark_exec_frame()

    reports = _run(client.generate_position_status_reports(SimpleNamespace()))

    assert len(reports) == 1
    report = reports[0]
    assert report.instrument_id == inst.id
    assert report.position_side == PositionSide.LONG
    assert report.quantity.as_double() == pytest.approx(15.0)
    assert float(report.avg_px_open) == pytest.approx((10 * 2.0 + 5 * 2.2) / 15)


def test_reconcile_reload_waits_for_current_bets_snapshot():
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)

    class ReloadPage:
        async def reload(self, *, wait_until=None, timeout=None):
            client._on_current_bets(
                [
                    {
                        "offerId": "SE-OFFER-1",
                        "marketId": inst.market_id,
                        "selectionId": str(inst.selection_id),
                        "side": "BACK",
                        "sizePlaced": "7.00",
                        "sizeMatched": "0.00",
                        "sizeRemaining": "7.00",
                        "averagePrice": "0",
                        "price": "1.50",
                    },
                ],
            )

    client._page = ReloadPage()

    reports = _run(client.generate_order_status_reports(SimpleNamespace()))

    assert len(reports) == 1
    assert reports[0].venue_order_id == VenueOrderId("SE-OFFER-1")
    assert reports[0].order_status == OrderStatus.ACCEPTED


def test_pending_accept_acks_on_first_current_bets_sighting_unmatched():
    """ack 不看是否成交:offerId 首次出现即 ack,即便 sizeMatched=0(纯挂单)。"""
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.0, price=1.01)
    client._cache.add_order(order)
    client._pending_accept["SE-OFFER-1"] = order.client_order_id
    accepted = []
    client.generate_order_accepted = lambda **kwargs: accepted.append(kwargs)

    client._on_current_bets([{
        "offerId": "SE-OFFER-1",
        "marketId": inst.market_id,
        "selectionId": str(inst.selection_id),
        "side": "BACK",
        "sizeMatched": "0.00",
        "averagePrice": "0",
        "sizeRemaining": "12.00",
    }])

    assert len(accepted) == 1
    assert accepted[0]["client_order_id"] == order.client_order_id
    assert accepted[0]["venue_order_id"] == VenueOrderId("SE-OFFER-1")
    assert "SE-OFFER-1" not in client._pending_accept  # 弹出,防重复 ack


def test_pending_accept_acks_during_reload_quiet_frame():
    """ack 与 fill 派生的静默帧规则无关(#255 只管 fill),reload 首帧也照常 ack。"""
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.0, price=1.01)
    client._cache.add_order(order)
    client._pending_accept["SE-OFFER-1"] = order.client_order_id
    client._reload_frame_pending = True
    accepted = []
    client.generate_order_accepted = lambda **kwargs: accepted.append(kwargs)

    client._on_current_bets([{
        "offerId": "SE-OFFER-1",
        "marketId": inst.market_id,
        "selectionId": str(inst.selection_id),
        "side": "BACK",
        "sizeMatched": "0.00",
        "sizeRemaining": "12.00",
    }])

    assert len(accepted) == 1
    assert "SE-OFFER-1" not in client._pending_accept


def test_pending_accept_same_frame_fill_resolves_via_newly_acked():
    """同一帧内 offerId 既是首次 ack 又已开始成交:fill 派生要靠 `newly_acked` 兜底
    解析 client_order_id(cache 索引还没跟上刚 enqueue 的 accepted 事件)。"""
    client = _client()
    inst = _instrument("home")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.0, price=1.01)
    client._cache.add_order(order)
    client._pending_accept["SE-OFFER-1"] = order.client_order_id
    accepted = []
    filled = []
    client.generate_order_accepted = lambda **kwargs: accepted.append(kwargs)
    client.generate_order_filled = lambda **kwargs: filled.append(kwargs)

    client._on_current_bets([{
        "offerId": "SE-OFFER-1",
        "marketId": inst.market_id,
        "selectionId": str(inst.selection_id),
        "side": "BACK",
        "sizeMatched": "12.00",
        "averagePrice": "1.01",
        "sizeRemaining": "0.00",
    }])

    assert len(accepted) == 1
    assert len(filled) == 1
    assert filled[0]["client_order_id"] == order.client_order_id
    assert filled[0]["last_qty"].as_double() == pytest.approx(12.0)


def test_query_order_forces_reload_without_pushing_reports():
    """#256:卡在飞仍是 dead → 强制 reload(同 #255 判定逻辑)→ alive,但不再对账
    (不构造/推送任何报告——状态同步交给 WS 监听在后续自然帧里做)。"""
    client = _client()
    calls = []
    client._venue_liveness = SimpleNamespace(
        mark_order_dead=lambda venue: calls.append(("dead", venue)),
        mark_position_dead=lambda venue: calls.append(("position_dead", venue)),
        mark_order_alive=lambda venue: calls.append(("alive", venue)),
        mark_position_alive=lambda venue: calls.append(("position_alive", venue)),
    )

    async def fresh(*, force=False):
        calls.append(("fresh", force))
        return True

    client._ensure_exec_snapshot_fresh = fresh

    _run(client._query_order(SimpleNamespace(client_order_id=ClientOrderId("O-1"))))

    assert calls == [
        ("dead", "SHARPEXCH"),
        ("position_dead", "SHARPEXCH"),
        ("fresh", True),
        ("alive", "SHARPEXCH"),
        ("position_alive", "SHARPEXCH"),
    ]
    assert not hasattr(client, "_push_reports_from_snapshot")


def test_query_order_reload_failure_keeps_order_liveness_dead():
    client = _client()
    calls = []
    client._venue_liveness = SimpleNamespace(
        mark_order_dead=lambda venue: calls.append(("dead", venue)),
        mark_position_dead=lambda venue: calls.append(("position_dead", venue)),
        mark_order_alive=lambda venue: calls.append(("alive", venue)),
    )

    async def stale(*, force=False):
        calls.append(("fresh", force))
        return False

    client._ensure_exec_snapshot_fresh = stale

    _run(client._query_order(SimpleNamespace(client_order_id=ClientOrderId("O-1"))))

    assert calls == [
        ("dead", "SHARPEXCH"),
        ("position_dead", "SHARPEXCH"),
        ("fresh", True),
    ]


def test_current_bets_normal_frame_skips_report_push():
    """#255:常规帧只走事件路径,不推 order/position 报告(同帧双路 = 双计根因)。"""
    client = _client()
    calls = []
    client._emit_cancel_events_from_current_bets = lambda: calls.append("updates")
    client._build_order_report = lambda bet: calls.append("build_report") or "R-A"
    client._build_position_status_reports_from_current_bets = (
        lambda: calls.append("build_positions") or ["P-A"]
    )
    client._venue_liveness = SimpleNamespace(
        mark_order_alive=lambda venue: calls.append("order_alive"),
        mark_position_alive=lambda venue: calls.append("position_alive"),
    )

    client._on_current_bets([{"offerId": "A"}])

    assert calls == ["updates", "order_alive", "position_alive"]


def test_current_bets_reload_frame_is_quiet(monkeypatch):
    """#255:reload 后首帧静默 —— 不自派生 fill、不推报告、不标 alive(归触发方)。"""
    from nautilus_trader.adapters.sharpexch import execution as se_exec

    client = _client()
    calls = []
    monkeypatch.setattr(
        se_exec, "current_bets_to_fills", lambda bets: calls.append("fills") or [],
    )
    client._build_order_report = lambda bet: calls.append("build_report") or "R-A"
    client._send_order_status_report = lambda report: calls.append("send_report")
    client._build_position_status_reports_from_current_bets = (
        lambda: calls.append("build_positions") or []
    )
    client._venue_liveness = SimpleNamespace(
        mark_order_alive=lambda venue: calls.append("order_alive"),
        mark_position_alive=lambda venue: calls.append("position_alive"),
    )
    client._reload_frame_pending = True

    client._on_current_bets([{"offerId": "A"}])

    assert calls == []                              # 静默:无 fill、无报告、无 alive
    assert client._reload_frame_pending is False
    assert client._last_current_bets_ns > 0         # 快照完成时间仍推进(reload 等待依赖它)

    client._on_current_bets([{"offerId": "A"}])
    assert "fills" in calls                         # 下一常规帧恢复事件路径


def test_reload_exec_page_marks_next_frame_quiet(monkeypatch):
    """#255:任何 reload 入口(含拉取路径 stale-WS)都经 _reload_exec_page 置静默标记。"""
    from nautilus_trader.adapters.sharpexch import execution as se_exec

    client = _client()
    fills_calls = []
    monkeypatch.setattr(
        se_exec, "current_bets_to_fills", lambda bets: fills_calls.append("fills") or [],
    )

    class ReloadPage:
        async def reload(self, *, wait_until=None, timeout=None):
            assert client._reload_frame_pending is True  # 标记先于重推
            client._on_current_bets([{"offerId": "A"}])

    client._page = ReloadPage()

    assert _run(client._reload_exec_page()) is True
    assert fills_calls == []                        # reload 帧未自派生 fill
    assert client._reload_frame_pending is False


def test_cancel_io_timeout_releases_page_lock_and_keeps_pending():
    client = _client()
    client._order_io_timeout_secs = 0.001
    rejected = []

    class Executor:
        async def cancel_order(self, market_id, venue_order_id, page, *, bet=None):
            await asyncio.Event().wait()

    client._executor = Executor()
    client._page = object()
    client.generate_order_cancel_rejected = lambda *args, **kwargs: rejected.append(args)

    _run(client._cancel_order(SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1-1-1-None.SHARPEXCH"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=VenueOrderId("111"),
    )))

    assert rejected == []
    assert not client._page_lock.locked()


def test_reload_current_bets_wait_budget_uses_page_timeout():
    client = _client(
        config=SharpExchExecClientConfig(username="u", password="p", page_timeout=4321),
    )
    assert client._reload_bets_wait_ns == 4_321_000_000


def test_reconcile_without_current_bets_marks_liveness_dead():
    liveness = VenueExecutionLiveness()
    client = _client(liveness=liveness)
    liveness.mark_order_alive("SHARPEXCH")
    liveness.mark_position_alive("SHARPEXCH")
    client._page = FakeSharpExchPage()
    client._reload_bets_wait_ns = 1

    # #259:与 OE 对称——快照不可信 = 查询失败 → 抛,不返空。
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(client.generate_order_status_reports(SimpleNamespace()))
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(client.generate_order_status_report(SimpleNamespace(venue_order_id=None, client_order_id=None)))
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(client.generate_position_status_reports(SimpleNamespace()))

    assert liveness.order_alive("SHARPEXCH") is False
    assert liveness.position_alive("SHARPEXCH") is False
