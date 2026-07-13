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
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness

from tests.arbitrage.adapters.sharpexch.test_provider import _event


def _client(*, liveness=None, browser_manager=None, browser_lock=None):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    return SharpExchExecutionClient(
        loop=asyncio.new_event_loop(),
        browser_manager=browser_manager,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=SharpExchExecClientConfig(username="u", password="p"),
        venue_liveness=liveness,
        browser_lock=browser_lock,
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


def _order(client, inst, *, qty=7.0, price=1.01):
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=client._clock,
    )
    return factory.limit(inst.id, OrderSide.BUY, inst.make_qty(qty), inst.make_price(price))


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


def test_submit_order_accepts_executor_success():
    client = _client()
    events = {}
    client._begin_session = lambda command: True
    client.generate_order_accepted = lambda *, strategy_id, instrument_id, client_order_id, venue_order_id, ts_event: events.update(
        venue_order_id=venue_order_id,
    )
    client.generate_order_rejected = lambda **kwargs: events.update(rejected=kwargs)

    async def place(order):
        return {"success": True, "venue_order_id": "SE-OFFER-1", "message": "ok"}

    client._place_via_executor = place
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id="I",
        client_order_id=SimpleNamespace(value="COID-1"),
    )

    _run(client._submit_order(SimpleNamespace(order=order)))

    assert str(events["venue_order_id"]) == "SE-OFFER-1"
    assert "rejected" not in events


def test_place_via_executor_translates_nt_order_and_passes_page():
    client = _client()
    inst = _instrument("away")
    client._cache.add_instrument(inst)
    order = _order(client, inst, qty=12.5, price=2.34)
    page = object()
    captured = {}

    class Executor:
        async def place_order(self, legacy_order, passed_page):
            captured.update(legacy_order=legacy_order, page=passed_page)
            return {"success": True, "venue_order_id": "SE-OFFER-1", "message": "ok"}

    client._executor = Executor()
    client._page = page

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


def test_submit_order_rejects_executor_failure_and_exception_ends_session():
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
    assert "exception before venue acknowledgement" in events["reason"]
    assert ended == [order.client_order_id]


def test_submit_order_cancel_only_discards():
    client = _client()
    placed = []
    client._begin_session = lambda command: False
    client._place_via_executor = lambda order: placed.append(order)

    _run(client._submit_order(SimpleNamespace(order=SimpleNamespace())))

    assert placed == []


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


def test_cancel_residual_one_reuses_normal_cancel_path():
    client = _client()
    captured = {}

    async def cancel_one(strategy_id, instrument_id, client_order_id, venue_order_id):
        captured.update(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
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


def test_reconcile_without_current_bets_marks_liveness_dead():
    liveness = VenueExecutionLiveness()
    client = _client(liveness=liveness)
    liveness.mark_order_alive("SHARPEXCH")
    liveness.mark_position_alive("SHARPEXCH")
    client._page = FakeSharpExchPage()
    client._reload_bets_wait_ns = 1

    assert _run(client.generate_order_status_reports(SimpleNamespace())) == []
    assert _run(client.generate_order_status_report(SimpleNamespace(venue_order_id=None, client_order_id=None))) is None
    assert _run(client.generate_position_status_reports(SimpleNamespace())) == []

    assert liveness.order_alive("SHARPEXCH") is False
    assert liveness.position_alive("SHARPEXCH") is False
