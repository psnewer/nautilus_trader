"""ArbPolymarketExecutionClient —— 离线可测部分(纯映射 + MRO)。

完整集成(真 ClobClient/ws_auth/Data API、_submit_order/_run_health_check 接线)经 /live-test 验。
"""

import inspect
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import msgspec
import pytest
from py_clob_client_v2 import ClobClient
from py_clob_client_v2 import PostOrdersArgs as TopLevelPostOrdersArgs
from py_clob_client_v2.clob_types import OrderPayload
from py_clob_client_v2.clob_types import PostOrdersV2Args

from nautilus_trader.adapters.polymarket.common.enums import PolymarketEventType
from nautilus_trader.adapters.polymarket.common.enums import PolymarketLiquiditySide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketTradeStatus
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.execution import polymarket_signed_order_id
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.http import transport as pm_transport
from nautilus_trader.adapters.polymarket.schemas.order import PolymarketMakerOrder
from nautilus_trader.adapters.polymarket.schemas.trade import PolymarketTradeReport
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import pm_raw_position_to_settlement
from nautilus_trader.adapters.polymarket.contract import TxResult
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from nautilus_trader.adapters.polymarket.settlement import SettlementResult
from nautilus_trader.adapters.polymarket.settlement import SettlementPosition
from tests.arbitrage.risk._factories import pm_instrument


@dataclass
class _PMPos:
    condition_id: str
    size: float
    neg_risk: bool = False
    redeemable: bool = False


class _RetryManager:
    result = True
    message = ""

    async def run(self, _name, _keys, runner, fn, *args):
        return await runner(fn, *args)


class _RetryPool:
    async def acquire(self):
        return _RetryManager()

    async def release(self, _retry_manager):
        return None


class _Clock:
    def timestamp_ns(self):
        return 123


class _Log:
    def info(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


class _TrackingLog(_Log):
    def __init__(self):
        self.debugs = []
        self.warnings = []

    def debug(self, *args, **_kwargs):
        self.debugs.append(args)

    def warning(self, *args, **_kwargs):
        self.warnings.append(args)


def _cancel_test_client(response):
    client = SimpleNamespace()
    client._retry_manager_pool = _RetryPool()
    client._clock = _Clock()
    client._log = _Log()
    client._http_client = SimpleNamespace(cancel_order=lambda payload: response)
    client._maintain_active_market = lambda instrument_id: _noop_async()
    client._begin_cancel_session = lambda order: True

    captured = {}
    client.generate_order_canceled = lambda **kwargs: captured.update(canceled=kwargs)
    client.generate_order_cancel_rejected = lambda **kwargs: captured.update(rejected=kwargs)

    venue_order_id = VenueOrderId("0x" + "a" * 64)
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=venue_order_id,
        is_closed=False,
    )
    client._cache = SimpleNamespace(order=lambda _coid: order)
    client._generate_cancel_event = PolymarketExecutionClient._generate_cancel_event.__get__(client)
    client._generate_cancel_success_event = (
        PolymarketExecutionClient._generate_cancel_success_event.__get__(client)
    )
    client._log_cancel_request_accepted = (
        PolymarketExecutionClient._log_cancel_request_accepted.__get__(client)
    )
    client._cancel_terminal_already_emitted = (
        PolymarketExecutionClient._cancel_terminal_already_emitted.__get__(client)
    )
    client._cancel_order = PolymarketExecutionClient._cancel_order.__get__(client)
    command = SimpleNamespace(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
    )
    return client, command, captured, venue_order_id


async def _noop_async():
    return None


async def _pm_positions_with_avg_price(**_kwargs):
    return [
        {
            "conditionId": "0xcond",
            "asset": "123",
            "size": "5",
            "avgPrice": "0.47",
        },
    ]


async def _pm_positions_without_valid_avg_price(**_kwargs):
    return [
        {
            "conditionId": "0xcond",
            "asset": "123",
            "size": "5",
            "avgPrice": "not-a-price",
        },
    ]


def _run(coro):
    return asyncio.run(coro)


def test_mro_mixin_before_upstream():
    # mixin 必须在上游前,才能覆盖 _send_order_event / _submit_order
    mro = ArbPolymarketExecutionClient.__mro__
    assert mro.index(ArbExecutionSessionMixin) < mro.index(PolymarketExecutionClient)


def test_raw_position_to_settlement_maps_fields():
    """#110:原始 /positions dict(Data API 键:conditionId/size/negativeRisk/redeemable)→ SettlementPosition。"""
    item = {"conditionId": "0xcond", "size": 80.0, "negativeRisk": True, "redeemable": True}
    assert pm_raw_position_to_settlement(item) == SettlementPosition(
        condition_id="0xcond", size=80.0, neg_risk=True, redeemable=True,
    )


def test_raw_position_to_settlement_defaults():
    s = pm_raw_position_to_settlement({"conditionId": "0xc", "size": 10.0})
    assert s.neg_risk is False and s.redeemable is False


def test_polymarket_execution_uses_py_clob_client_v2_surface():
    """PM live 已要求 CLOB v2;锁 execution/factory 不回退旧 py_clob_client。"""
    assert inspect.signature(get_polymarket_http_client).return_annotation is ClobClient
    assert "py_clob_client_v2 import ClobClient" in inspect.getsource(
        inspect.getmodule(get_polymarket_http_client),
    )

    assert str(inspect.signature(ClobClient.post_order)) == (
        "(self, order, order_type: py_clob_client_v2.clob_types.OrderType = 'GTC', "
        "post_only: bool = False, defer_exec: bool = False)"
    )
    assert str(inspect.signature(PostOrdersV2Args)) == (
        "(order: Any, orderType: py_clob_client_v2.clob_types.OrderType = 'GTC', "
        "deferExec: bool = False) -> None"
    )
    assert str(TopLevelPostOrdersArgs).startswith("typing.Union[")
    assert str(inspect.signature(OrderPayload)) == "(orderID: str) -> None"

    source = inspect.getsource(PolymarketExecutionClient._post_signed_order)
    assert "self._http_client.post_order" in source

    module_source = inspect.getsource(inspect.getmodule(PolymarketExecutionClient))
    assert "PostOrdersV2Args as PostOrdersArgs" in module_source

    batch_source = inspect.getsource(PolymarketExecutionClient._sign_orders_for_batch)
    assert "PostOrdersArgs" in batch_source

    reports_source = inspect.getsource(PolymarketExecutionClient.generate_order_status_reports)
    assert "self._http_client.get_open_orders" in reports_source
    assert "self._http_client.get_orders" not in reports_source

    cancel_source = inspect.getsource(PolymarketExecutionClient._cancel_order)
    assert "self._http_client.cancel_order" in cancel_source
    assert "OrderPayload(orderID=venue_order_id.value)" in cancel_source


def test_polymarket_signed_order_id_is_deterministic_clob_hash():
    signed_order = SimpleNamespace(
        salt="1",
        maker="0x0000000000000000000000000000000000000001",
        signer="0x0000000000000000000000000000000000000002",
        tokenId="3",
        makerAmount="4000000",
        takerAmount="5000000",
        side=0,
        signatureType=2,
        timestamp="1710000000000",
        metadata="0x" + "00" * 32,
        builder="0x" + "00" * 32,
        expiration="0",
        signature="0x",
    )

    assert polymarket_signed_order_id(signed_order, chain_id=137, neg_risk=False) == VenueOrderId(
        "0x926967de7a3565093df01b8db43a0890bf5f3f7d6d9863c0f04f9e1cd60c1f6f",
    )


def test_arb_ambiguous_submit_failure_keeps_session_untouched():
    client = SimpleNamespace(
        _cache=SimpleNamespace(
            venue_order_id=lambda _coid: VenueOrderId("0x" + "a" * 64),
        ),
        _log=_TrackingLog(),
    )
    order = SimpleNamespace(client_order_id=ClientOrderId("O-INFLIGHT"))

    ArbPolymarketExecutionClient._handle_ambiguous_submit_failure(
        client,
        order,
        "post response lost",
    )

    assert any("retaining SUBMITTED order" in args[0] for args in client._log.warnings)


def test_polymarket_empty_submit_response_is_ambiguous_not_rejected():
    captured = []
    client = SimpleNamespace(
        _retry_manager_pool=_RetryPool(),
        _http_client=SimpleNamespace(post_order=lambda *_args: None),
        _handle_ambiguous_submit_failure=lambda order, reason: captured.append(
            (order.client_order_id, reason),
        ),
        generate_order_rejected=lambda **_kwargs: pytest.fail("empty response is not a definite rejection"),
    )
    client._post_signed_order = PolymarketExecutionClient._post_signed_order.__get__(client)
    order = SimpleNamespace(
        client_order_id=ClientOrderId("O-INFLIGHT"),
        time_in_force="GTC",
    )

    _run(client._post_signed_order(order, SimpleNamespace(), order_type_override="GTC"))

    assert captured == [(ClientOrderId("O-INFLIGHT"), "")]


def test_arb_inflight_query_updates_order_before_marking_alive():
    calls = []

    class Liveness:
        def mark_order_dead(self, venue):
            calls.append(("dead", venue))

        def mark_order_alive(self, venue):
            calls.append(("alive", venue))

    report = SimpleNamespace()
    client = SimpleNamespace(
        _venue_liveness=Liveness(),
        _clock=_Clock(),
        _log=_TrackingLog(),
        _send_order_status_report=lambda value: calls.append(("update", value)),
    )

    async def generate(_command, *, retry=True):
        calls.append(("query", retry))
        return report

    client.generate_order_status_report = generate
    command = SimpleNamespace(
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-INFLIGHT"),
        venue_order_id=VenueOrderId("0x" + "a" * 64),
    )

    _run(ArbPolymarketExecutionClient._query_order(client, command))

    assert calls == [
        ("dead", POLYMARKET),
        ("query", False),
        ("update", report),
        ("alive", POLYMARKET),
    ]


def test_arb_inflight_query_failure_stays_dead_without_session_call():
    calls = []

    class Liveness:
        def mark_order_dead(self, venue):
            calls.append(("dead", venue))

        def mark_order_alive(self, venue):
            pytest.fail(f"must remain dead: {venue}")

    client = SimpleNamespace(
        _venue_liveness=Liveness(),
        _clock=_Clock(),
        _log=_TrackingLog(),
    )

    async def generate(_command, *, retry=True):
        calls.append(("query", retry))
        return None

    client.generate_order_status_report = generate
    command = SimpleNamespace(
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-INFLIGHT"),
        venue_order_id=VenueOrderId("0x" + "a" * 64),
    )

    _run(ArbPolymarketExecutionClient._query_order(client, command))

    assert calls == [("dead", POLYMARKET), ("query", False)]


def test_polymarket_single_report_without_retry_calls_http_once():
    calls = []

    class NoRetryPool:
        async def acquire(self):
            pytest.fail("one-shot in-flight query must not acquire RetryManager")

    client = SimpleNamespace(
        _maintain_active_market=lambda _instrument_id: _noop_async(),
        _cache=SimpleNamespace(venue_order_id=lambda _coid: VenueOrderId("0x" + "a" * 64)),
        _clock=_Clock(),
        _log=_Log(),
        _retry_manager_pool=NoRetryPool(),
        _http_client=SimpleNamespace(
            get_order=lambda **_kwargs: calls.append("get_order"),
        ),
    )
    client.generate_order_status_report = PolymarketExecutionClient.generate_order_status_report.__get__(client)
    command = SimpleNamespace(
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-INFLIGHT"),
        venue_order_id=None,
    )

    report = _run(client.generate_order_status_report(command, retry=False))

    assert report is None
    assert calls == ["get_order"]


def test_polymarket_cancel_order_success_waits_for_ws_cancellation_event():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [venue_order_id],
        "not_canceled": {},
    })

    _run(client._cancel_order(command))

    assert "canceled" not in captured
    assert "rejected" not in captured

    client._generate_cancel_success_event(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=expected_venue_order_id,
        ts_event=123,
    )

    assert captured["canceled"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["canceled"]["venue_order_id"] == expected_venue_order_id
    assert "rejected" not in captured


def test_polymarket_cancel_success_skips_duplicate_canceled_order():
    venue_order_id = VenueOrderId("0x" + "a" * 64)
    client = SimpleNamespace()
    client._clock = _Clock()
    client._log = _Log()
    client._cache = SimpleNamespace(
        order=lambda _coid: SimpleNamespace(status=OrderStatus.CANCELED),
    )
    client._cancel_terminal_client_ids = OrderedDict()
    captured = {}
    client.generate_order_canceled = lambda **kwargs: captured.update(canceled=kwargs)
    client._generate_cancel_success_event = (
        PolymarketExecutionClient._generate_cancel_success_event.__get__(client)
    )
    client._cancel_terminal_already_emitted = (
        PolymarketExecutionClient._cancel_terminal_already_emitted.__get__(client)
    )

    client._generate_cancel_success_event(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=venue_order_id,
        ts_event=123,
    )

    assert captured == {}


def test_polymarket_cancel_success_skips_duplicate_before_cache_updates():
    venue_order_id = VenueOrderId("0x" + "a" * 64)
    client = SimpleNamespace()
    client._clock = _Clock()
    client._log = _Log()
    client._cache = SimpleNamespace(
        order=lambda _coid: SimpleNamespace(status=OrderStatus.ACCEPTED),
    )
    client._cancel_terminal_client_ids = OrderedDict()
    captured = []
    client.generate_order_canceled = lambda **kwargs: captured.append(kwargs)
    client._generate_cancel_success_event = (
        PolymarketExecutionClient._generate_cancel_success_event.__get__(client)
    )
    client._cancel_terminal_already_emitted = (
        PolymarketExecutionClient._cancel_terminal_already_emitted.__get__(client)
    )

    kwargs = dict(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=venue_order_id,
        ts_event=123,
    )
    client._generate_cancel_success_event(**kwargs)
    client._generate_cancel_success_event(**kwargs)

    assert len(captured) == 1


def test_polymarket_cancel_order_reject_generates_cancel_rejected_event():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [],
        "not_canceled": {venue_order_id: "already open on another market"},
    })

    _run(client._cancel_order(command))

    assert captured["rejected"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["rejected"]["venue_order_id"] == expected_venue_order_id
    assert captured["rejected"]["reason"] == "already open on another market"
    assert "canceled" not in captured


def test_polymarket_position_report_maps_avg_price_from_data_api():
    """PM /positions 的 avgPrice 必须进入 NT PositionStatusReport.avg_px_open。"""
    instrument_id = InstrumentId.from_str("0xcond-123.POLYMARKET")
    client = SimpleNamespace()
    client.account_id = AccountId("PM-001")
    client._clock = _Clock()
    client._log = _Log()
    client._fetch_user_positions = _pm_positions_with_avg_price
    client._fetch_positions_from_data_api = (
        PolymarketExecutionClient._fetch_positions_from_data_api.__get__(client)
    )
    client.generate_position_status_reports = (
        PolymarketExecutionClient.generate_position_status_reports.__get__(client)
    )
    client._log_report_receipt = lambda *_args, **_kwargs: None

    reports = _run(client.generate_position_status_reports(
        SimpleNamespace(instrument_id=instrument_id, log_receipt_level=None),
    ))

    assert len(reports) == 1
    assert reports[0].instrument_id == instrument_id
    assert reports[0].quantity.as_double() == 5.0
    assert reports[0].avg_px_open == Decimal("0.47")


def test_polymarket_position_report_keeps_quantity_when_avg_price_unknown():
    instrument_id = InstrumentId.from_str("0xcond-123.POLYMARKET")
    client = SimpleNamespace()
    client.account_id = AccountId("PM-001")
    client._clock = _Clock()
    client._log = _Log()
    client._fetch_user_positions = _pm_positions_without_valid_avg_price
    client._fetch_positions_from_data_api = (
        PolymarketExecutionClient._fetch_positions_from_data_api.__get__(client)
    )
    client.generate_position_status_reports = (
        PolymarketExecutionClient.generate_position_status_reports.__get__(client)
    )
    client._log_report_receipt = lambda *_args, **_kwargs: None

    reports = _run(client.generate_position_status_reports(
        SimpleNamespace(instrument_id=instrument_id, log_receipt_level=None),
    ))

    assert len(reports) == 1
    assert reports[0].quantity.as_double() == 5.0
    assert reports[0].avg_px_open is None


def test_polymarket_fill_history_unknown_instrument_is_debug_noise():
    """PM 历史成交可包含当前未加载 market;跳过即可,不应在 live 中刷 WARN。"""
    log = _TrackingLog()
    client = SimpleNamespace(
        _decoder_trade_report=msgspec.json.Decoder(PolymarketTradeReport),
        _wallet_address="0xmaker",
        _api_key="api-key",
        _cache=SimpleNamespace(instrument=lambda _instrument_id: None),
        _log=log,
    )
    reports = []
    trade_payload = {
        "id": "trade-abc",
        "taker_order_id": "0xtaker",
        "market": "0xmarket",
        "asset_id": "123",
        "side": "BUY",
        "size": "5",
        "fee_rate_bps": "0",
        "price": "0.55",
        "status": "CONFIRMED",
        "match_time": "1710000000",
        "last_update": "1710000001",
        "outcome": "Yes",
        "bucket_index": 0,
        "owner": "api-key",
        "maker_address": "0xmaker",
        "transaction_hash": "0xdeadbeef",
        "maker_orders": [],
        "trader_side": "TAKER",
    }

    PolymarketExecutionClient._parse_trades_response_object(
        client,
        command=SimpleNamespace(instrument_id=None, venue_order_id=None),
        json_obj=trade_payload,
        parsed_fill_keys=set(),
        reports=reports,
    )

    assert reports == []
    assert log.warnings == []
    assert len(log.debugs) == 1


class _PMFillTracker:
    def __init__(self):
        self.recorded = []

    def snap_fill_qty(self, _venue_order_id, fill_qty):
        return fill_qty

    def record_fill(self, *, venue_order_id, qty, px, ts):
        self.recorded.append((venue_order_id, qty, px, ts))


class _PMTradeMsg:
    id = "trade-1"
    market = "0xcond"
    match_time = "1710000000"

    def __init__(self, status):
        self.status = status

    def venue_order_id(self, _order_id):
        return VenueOrderId("PM-OID-1")

    def get_asset_id(self, _order_id):
        return "tok1"

    def last_qty(self, _order_id):
        return Decimal("5")

    def last_px(self, _order_id):
        return Decimal("0.42")

    def liquidity_side(self):
        return LiquiditySide.TAKER

    def to_dict(self):
        return {"status": self.status.value}


def test_polymarket_realtime_fill_waits_for_confirmed_status():
    """PM 实时成交只在 CONFIRMED 终态发 NT fill;MATCHED 只记录状态,不释放完全成交 session。"""
    cache = TestComponentStubs.cache()
    inst = pm_instrument("ATP", "home", token="tok1")
    cache.add_instrument(inst)
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    )
    order = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(5), inst.make_price(0.42))
    cache.add_order(order)
    cache.add_venue_order_id(order.client_order_id, VenueOrderId("PM-OID-1"))

    client = SimpleNamespace(
        account_id=AccountId("POLYMARKET-001"),
        _api_key="api-key",
        _cache=cache,
        _clock=_Clock(),
        _fill_tracker=_PMFillTracker(),
        _finalized_trades=OrderedDict(),
        _log=_Log(),
        _processed_fills=OrderedDict(),
        _processed_trades=OrderedDict(),
        _wallet_address="0xwallet",
        PROCESSED_TRADES_LIMIT=100,
    )
    captured = []
    client.generate_order_filled = lambda **kwargs: captured.append(kwargs)
    client._truncate_ordered_dict = PolymarketExecutionClient._truncate_ordered_dict.__get__(client)
    client._record_processed_fill = PolymarketExecutionClient._record_processed_fill.__get__(client)
    client._record_processed_trade = PolymarketExecutionClient._record_processed_trade.__get__(client)
    client._handle_user_trade_in_ws_trade_msg = (
        PolymarketExecutionClient._handle_user_trade_in_ws_trade_msg.__get__(client)
    )

    client._handle_user_trade_in_ws_trade_msg(
        _PMTradeMsg(PolymarketTradeStatus.MATCHED),
        trade_id="trade-1",
        wait_for_ack=False,
        order_id="PM-OID-1",
    )
    assert captured == []

    client._handle_user_trade_in_ws_trade_msg(
        _PMTradeMsg(PolymarketTradeStatus.CONFIRMED),
        trade_id="trade-1",
        wait_for_ack=False,
        order_id="PM-OID-1",
    )

    assert len(captured) == 1
    assert captured[0]["last_qty"].as_double() == pytest.approx(5.0)
    assert captured[0]["last_px"].as_double() == pytest.approx(0.42)


def test_polymarket_realtime_maker_fill_uses_maker_order_fields():
    """PM maker 成交用 maker_orders 中属于本账户的 order_id/matched_amount/price。"""
    cache = TestComponentStubs.cache()
    inst = pm_instrument("ATP", "home", token="tok1")
    cache.add_instrument(inst)
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    )
    order = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(3.25), inst.make_price(0.37))
    cache.add_order(order)
    cache.add_venue_order_id(order.client_order_id, VenueOrderId("PM-MAKER-OID"))

    client = SimpleNamespace(
        account_id=AccountId("POLYMARKET-001"),
        _api_key="api-key",
        _cache=cache,
        _clock=_Clock(),
        _fill_tracker=_PMFillTracker(),
        _finalized_trades=OrderedDict(),
        _log=_Log(),
        _processed_fills=OrderedDict(),
        _processed_trades=OrderedDict(),
        _wallet_address="0xwallet",
        PROCESSED_TRADES_LIMIT=100,
    )
    captured = []
    client.generate_order_filled = lambda **kwargs: captured.append(kwargs)
    client._truncate_ordered_dict = PolymarketExecutionClient._truncate_ordered_dict.__get__(client)
    client._record_processed_fill = PolymarketExecutionClient._record_processed_fill.__get__(client)
    client._record_processed_trade = PolymarketExecutionClient._record_processed_trade.__get__(client)
    client._handle_user_trade_in_ws_trade_msg = (
        PolymarketExecutionClient._handle_user_trade_in_ws_trade_msg.__get__(client)
    )

    maker_order = PolymarketMakerOrder(
        asset_id="tok1",
        fee_rate_bps="",
        maker_address="0xwallet",
        matched_amount="3.25",
        order_id="PM-MAKER-OID",
        outcome="Alexander Zverev",
        owner="api-key",
        price="0.37",
    )
    msg = PolymarketUserTrade(
        asset_id="other-token",
        bucket_index=0,
        fee_rate_bps="0",
        id="trade-maker-1",
        last_update="1710000000",
        maker_address="0xwallet",
        maker_orders=[maker_order],
        market="0xcond",
        match_time="1710000000",
        outcome="Jannik Sinner",
        owner="other-owner",
        price="0.63",
        side=PolymarketOrderSide.SELL,
        size="3.25",
        status=PolymarketTradeStatus.CONFIRMED,
        taker_order_id="PM-TAKER-OID",
        timestamp="1710000000000",
        trade_owner="other-owner",
        trader_side=PolymarketLiquiditySide.MAKER,
        type=PolymarketEventType.TRADE,
    )

    assert msg.get_filled_user_order_ids("0xwallet", "api-key") == ["PM-MAKER-OID"]

    client._handle_user_trade_in_ws_trade_msg(
        msg,
        trade_id="trade-maker-1",
        wait_for_ack=False,
        order_id="PM-MAKER-OID",
    )

    assert len(captured) == 1
    assert str(captured[0]["venue_order_id"]) == "PM-MAKER-OID"
    assert captured[0]["last_qty"].as_double() == pytest.approx(3.25)
    assert captured[0]["last_px"].as_double() == pytest.approx(0.37)
    assert captured[0]["liquidity_side"] == LiquiditySide.MAKER


def test_arb_generate_position_reports_marks_alive_and_dispatches_settlement(monkeypatch):
    """#110:连续 position 对账进入 PM override 后,一次拉喂 report + settlement。"""
    settlement_calls = []
    balance_calls = []

    async def fake_super(self, command):
        self._last_raw_positions = [
            {"conditionId": "cond1", "size": "10", "negativeRisk": True, "redeemable": False},
        ]
        return ["report"]

    class _Settlement:
        async def run(self, positions):
            settlement_calls.append(positions)

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._settlement = _Settlement()
        client._settlement_inflight = False
        client._loop = asyncio.get_running_loop()

        async def refresh_balance():
            balance_calls.append("balance_refresh")

        client._update_account_state = refresh_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())
        assert reports == ["report"]
        assert client._venue_liveness.position_alive(POLYMARKET)
        assert client._settlement_inflight is True

        await asyncio.sleep(0)
        assert client._settlement_inflight is False

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())

    assert balance_calls == ["balance_refresh"]
    assert settlement_calls == [[SettlementPosition("cond1", 10.0, neg_risk=True, redeemable=False)]]


def test_arb_generate_position_reports_balance_refresh_failure_does_not_fail_reconcile(monkeypatch):
    async def fake_super(self, command):
        self._last_raw_positions = []
        return ["report"]

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._settlement = None
        client._settlement_inflight = False
        client._loop = asyncio.get_running_loop()

        async def fail_balance():
            raise RuntimeError("balance unavailable")

        client._update_account_state = fail_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert reports == ["report"]
        assert client._venue_liveness.position_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())


def test_run_settlement_does_not_auto_sync_collateral_balance_after_successful_tx():
    class _Settlement:
        async def run(self, _positions):
            return SettlementResult(merges=[TxResult(success=True, tx_hash="0xm")])

    calls = []

    def update_balance_allowance(params):
        calls.append(params)
        return {"ok": True}

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._settlement = _Settlement()
        client._settlement_inflight = True
        client._http_client = SimpleNamespace(update_balance_allowance=update_balance_allowance)
        client._config = SimpleNamespace(signature_type=2)

        await client._run_settlement([{"conditionId": "cond1", "size": "10"}])

        assert client._settlement_inflight is False
        assert calls == []

    _run(scenario())


def test_run_settlement_does_not_sync_collateral_balance_without_successful_tx():
    class _Settlement:
        async def run(self, _positions):
            return SettlementResult(merges=[TxResult(success=False, message="reverted")])

    calls = []

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._settlement = _Settlement()
        client._settlement_inflight = True
        client._http_client = SimpleNamespace(update_balance_allowance=lambda params: calls.append(params))
        client._config = SimpleNamespace(signature_type=2)

        await client._run_settlement([{"conditionId": "cond1", "size": "10"}])

        assert client._settlement_inflight is False
        assert calls == []

    _run(scenario())


def test_arb_generate_position_reports_failure_marks_dead(monkeypatch):
    async def fake_super(self, command):
        raise RuntimeError("positions unavailable")

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()

        client._venue_liveness.mark_position_alive(POLYMARKET)
        # #122:失败 mark_dead + 返空(不 raise,对齐 OE;避免 startup reconciliation 卡死)
        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert reports == []
        assert not client._venue_liveness.position_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())


class _FailingRetryManager:
    def __init__(self, fail_name: str):
        self.fail_name = fail_name
        self.result = True
        self.message = None
        self.run_calls = []

    async def run(self, name, details, func, *args, **kwargs):
        self.run_calls.append(name)
        if name == self.fail_name:
            self.result = False
            self.message = "transport unavailable"
            return None
        self.result = True
        return []


class _FailingRetryPool:
    def __init__(self, fail_name: str):
        self.manager = _FailingRetryManager(fail_name)

    async def acquire(self):
        return self.manager

    async def release(self, _manager):
        return None


def test_arb_generate_order_reports_retry_failure_marks_dead(monkeypatch):
    """PM RetryManager 返回 None 时,不能把空 reports 当作 order liveness alive。"""

    async def fake_super(self, command):
        retry_manager = await self._retry_manager_pool.acquire()
        try:
            await retry_manager.run("generate_order_status_reports", [], None)
        finally:
            await self._retry_manager_pool.release(retry_manager)
        return []

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._retry_manager_pool = _FailingRetryPool("generate_order_status_reports")
        client._venue_liveness.mark_order_alive(POLYMARKET)

        # #122:retry failure → mark_dead + 返空(不 raise,对齐 OE)
        reports = await client.generate_order_status_reports(SimpleNamespace())

        assert reports == []
        assert not client._venue_liveness.order_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_reports", fake_super)

    _run(scenario())


def test_arb_generate_single_order_report_retry_failure_marks_dead(monkeypatch):
    """single order report 查询失败也必须使 order liveness dead。"""

    async def fake_super(self, command, *, retry=True):
        retry_manager = await self._retry_manager_pool.acquire()
        try:
            await retry_manager.run("generate_order_status_report", [], None)
        finally:
            await self._retry_manager_pool.release(retry_manager)
        return None

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._retry_manager_pool = _FailingRetryPool("generate_order_status_report")
        client._venue_liveness.mark_order_alive(POLYMARKET)

        # #122:single report retry failure → mark_dead + 返 None(不 raise)
        report = await client.generate_order_status_report(SimpleNamespace())

        assert report is None
        assert not client._venue_liveness.order_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_report", fake_super)

    _run(scenario())


def test_arb_generate_order_reports_fill_retry_failure_marks_dead(monkeypatch):
    """bulk order reports 内部 fill report 查询失败也必须使 order liveness dead。"""

    async def fake_super(self, command):
        retry_manager = await self._retry_manager_pool.acquire()
        try:
            await retry_manager.run("generate_order_status_reports", [], None)
        finally:
            await self._retry_manager_pool.release(retry_manager)
        retry_manager = await self._retry_manager_pool.acquire()
        try:
            await retry_manager.run("generate_fill_reports", [], None)
        finally:
            await self._retry_manager_pool.release(retry_manager)
        return []

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._retry_manager_pool = _FailingRetryPool("generate_fill_reports")
        client._venue_liveness.mark_order_alive(POLYMARKET)

        # #122:retry failure → mark_dead + 返空(不 raise,对齐 OE)
        reports = await client.generate_order_status_reports(SimpleNamespace())

        assert reports == []
        assert not client._venue_liveness.order_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_reports", fake_super)

    _run(scenario())


def test_polymarket_factory_configures_v2_http_proxy(monkeypatch):
    """PM CLOB REST 必须吃项目 proxy_url,避免 WS/REST 走不同出口。"""
    get_polymarket_http_client.cache_clear()

    created = []

    class _OldClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old_client = _OldClient()
    monkeypatch.setattr(pm_transport.clob_http_helpers, "_http_client", old_client)
    monkeypatch.setattr(pm_transport, "_configured_proxy_url", None)

    class _HttpxClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def close(self):
            pass

    class _ClobClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)
    monkeypatch.setattr("nautilus_trader.adapters.polymarket.factories.ClobClient", _ClobClient)

    get_polymarket_http_client(
        api_key="K",
        api_secret="S",
        passphrase="P",
        private_key="0x" + "1" * 64,
        funder="0x" + "2" * 40,
        proxy_url="http://127.0.0.1:7890",
    )

    assert created == [{"http2": True, "proxy": "http://127.0.0.1:7890", "trust_env": False}]
    assert old_client.closed


def test_polymarket_data_api_http_client_uses_proxy(monkeypatch):
    """PM Data API(`/positions`) 必须吃同一个 proxy_url,避免周期 position 对账直连。"""
    source = inspect.getsource(PolymarketExecutionClient.__init__)
    assert "HttpClient(timeout_secs=15, proxy_url=config.proxy_url)" in source


def test_polymarket_geoblock_preflight_rejects_blocked_route(monkeypatch):
    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": True, "country": "AU", "region": "NSW"}

    class _HttpxClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            assert url == pm_transport.GEOBLOCK_URL
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    try:
        pm_transport.check_polymarket_geoblock("http://127.0.0.1:7890")
    except RuntimeError as e:
        assert "geoblocked" in str(e)
        assert "country=AU" in str(e)
    else:
        raise AssertionError("blocked geoblock response should fail preflight")


def test_polymarket_geoblock_preflight_allows_frontend_only_restricted_country(monkeypatch):
    """官方文档列 JP 为 frontend-only restricted;API preflight 不应一刀切拦。"""
    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": True, "country": "JP", "region": "27"}

    class _HttpxClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    assert pm_transport.check_polymarket_geoblock(None)["country"] == "JP"


def test_polymarket_geoblock_preflight_uses_configured_proxy(monkeypatch):
    created = []

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": False, "country": "IE", "region": ""}

    class _HttpxClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)

    assert pm_transport.check_polymarket_geoblock("http://127.0.0.1:7890")["blocked"] is False
    assert created == [{"proxy": "http://127.0.0.1:7890", "trust_env": False, "timeout": 10.0}]
