"""ArbPolymarketExecutionClient —— 离线可测部分(纯映射 + MRO)。

完整集成(真 ClobClient/ws_auth/Data API、_submit_order/_run_health_check 接线)经 /live-test 验。
"""

import asyncio
import inspect
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import msgspec
import pytest
from py_clob_client_v2 import ClobClient
from py_clob_client_v2 import PostOrdersArgs as TopLevelPostOrdersArgs
from py_clob_client_v2.clob_types import OrderPayload
from py_clob_client_v2.clob_types import OrderType as PolyOrderType
from py_clob_client_v2.clob_types import PostOrdersV2Args
from py_clob_client_v2.exceptions import PolyApiException
from py_clob_client_v2.order_utils.model.order_data_v2 import SignedOrderV2
from py_clob_client_v2.order_utils.model.side import Side as PolySide
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient
from nautilus_trader.adapters.polymarket.arb_execution import _realized_by_instrument
from nautilus_trader.adapters.polymarket.arb_execution import pm_raw_position_to_settlement
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET
from nautilus_trader.adapters.polymarket.common.enums import PolymarketEventType
from nautilus_trader.adapters.polymarket.common.enums import PolymarketLiquiditySide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketTradeStatus
from nautilus_trader.adapters.polymarket.contract import TxResult
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.execution import polymarket_signed_order_id
from nautilus_trader.adapters.polymarket.factories import get_polymarket_http_client
from nautilus_trader.adapters.polymarket.http import transport as pm_transport
from nautilus_trader.adapters.polymarket.schemas.order import PolymarketMakerOrder
from nautilus_trader.adapters.polymarket.schemas.trade import PolymarketTradeReport
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade
from nautilus_trader.adapters.polymarket.settlement import SettlementPosition
from nautilus_trader.adapters.polymarket.settlement import SettlementResult
from nautilus_trader.adapters.polymarket.websocket.types import USER_WS_MESSAGE
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import tags_from_meta
from src.arbitrage.common.realized_pnl import RealizedPnlLedger
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.execution.session import ArbExecutionSessionMixin
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


class _FailedRetryPool:
    def __init__(self, exc):
        self.manager = SimpleNamespace(
            result=False,
            message=str(exc),
            last_exception=exc,
        )

        async def run(*_args, **_kwargs):
            return None

        self.manager.run = run

    async def acquire(self):
        return self.manager

    async def release(self, _retry_manager):
        return None


class _Clock:
    def timestamp_ns(self):
        return 123


def _stable_reconciliation_state(client, state=None):
    state = state or {"version": 0}

    class _Snapshot:
        def __init__(self, kind):
            self.kind = kind
            self.value = state["version"]

        def is_current_for_instruments(self, _client, _instrument_ids):
            # #318:per-instrument 判定;此 fake 用 version 模拟 stale,忽略具体 instrument。
            return self.value == state["version"]

    client._capture_reconciliation_state_snapshot = lambda *, kind: _Snapshot(kind)
    return state


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
    client._ack_cancel_session = lambda coid, voi: captured.update(cancel_ack=(coid, voi))

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
    client._ack_normal_cancel_response = (
        PolymarketExecutionClient._ack_normal_cancel_response.__get__(client)
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


def test_realized_by_instrument_aggregates_rows_within_one_snapshot():
    rows = [
        {"conditionId": "0xc", "asset": "1", "realizedPnl": "1.25"},
        {"conditionId": "0xc", "asset": "1", "realizedPnl": "-0.25"},
        {"conditionId": "0xc", "asset": "2", "realizedPnl": "3"},
    ]

    assert _realized_by_instrument(rows) == {
        "0xc-1.POLYMARKET": 1.0,
        "0xc-2.POLYMARKET": 3.0,
    }


def test_position_reconcile_deduplicates_realized_snapshot_overlap():
    """同一 asset 同时出现在 current/closed 时是累计快照重叠,不得当两笔流水相加。"""
    async def scenario():
        client = SimpleNamespace(_realized_pnl_ledger=object())

        async def closed_positions():
            return [
                {"conditionId": "0xc", "asset": "1", "realizedPnl": "0.75"},
                {"conditionId": "0xc", "asset": "2", "realizedPnl": "-0.75"},
            ]

        client._fetch_closed_positions = closed_positions
        load = ArbPolymarketExecutionClient._load_realized_pnl_snapshot.__get__(client)

        snapshot = await load([
            {"conditionId": "0xc", "asset": "1", "realizedPnl": "0.75"},
        ])

        assert snapshot == {
            "0xc-1.POLYMARKET": 0.75,
            "0xc-2.POLYMARKET": -0.75,
        }

    _run(scenario())


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


def test_arb_pm_accepted_reserve_is_noop():
    """#254:PM 关闭 accepted 预扣 —— 覆盖为 no-op,不读账户不写 AccountState。"""
    client = SimpleNamespace()  # 无任何依赖:实现若读 cache/account 会 AttributeError

    result = ArbPolymarketExecutionClient._reserve_available_balance_for_accepted_order(
        client,
        event=SimpleNamespace(),
        sess={},
    )

    assert result is None


@pytest.mark.parametrize(
    ("payload", "venue_order_id"),
    [
        (
            {
                "event_type": "order",
                "id": "PM-OID-UNKNOWN-ORDER",
                "status": "UNKNOWN_ORDER_STATUS",
            },
            "PM-OID-UNKNOWN-ORDER",
        ),
        (
            {
                "event_type": "trade",
                "taker_order_id": "PM-OID-UNKNOWN-TRADE",
                "maker_orders": [],
                "status": "CANCELED_order couldn't be fully filled",
            },
            "PM-OID-UNKNOWN-TRADE",
        ),
    ],
)
def test_polymarket_unknown_user_ws_status_generates_order_rejected(
    payload,
    venue_order_id,
):
    coid = ClientOrderId("O-UNKNOWN-STATUS")
    order = SimpleNamespace(
        strategy_id=StrategyId("S-1"),
        instrument_id=InstrumentId.from_str("0xcond-token.POLYMARKET"),
        client_order_id=coid,
    )
    rejected = []
    client = SimpleNamespace(
        _cache=SimpleNamespace(
            client_order_id=lambda value: coid if value == VenueOrderId(venue_order_id) else None,
            order=lambda value: order if value == coid else None,
        ),
        _clock=_Clock(),
        _log=_TrackingLog(),
        generate_order_rejected=lambda **kwargs: rejected.append(kwargs),
    )

    handled = PolymarketExecutionClient._reject_unknown_user_ws_status(
        client,
        msgspec.json.encode(payload),
    )

    assert handled is True
    assert len(rejected) == 1
    assert rejected[0]["client_order_id"] == coid
    assert "Unrecognized Polymarket" in rejected[0]["reason"]


def test_polymarket_known_user_ws_status_is_not_reclassified():
    client = SimpleNamespace()
    raw = msgspec.json.encode({
        "event_type": "trade",
        "taker_order_id": "PM-OID-KNOWN",
        "maker_orders": [],
        "status": "CONFIRMED",
    })

    assert PolymarketExecutionClient._reject_unknown_user_ws_status(client, raw) is False


def test_polymarket_ws_decoder_routes_validation_error_to_unknown_status_handler():
    handled = []
    client = SimpleNamespace(
        _config=SimpleNamespace(log_raw_ws_messages=False),
        _decoder_user_msg=msgspec.json.Decoder(USER_WS_MESSAGE),
        _reject_unknown_user_ws_status=lambda raw: handled.append(raw) or True,
        _log=_TrackingLog(),
    )
    raw = msgspec.json.encode({
        "event_type": "trade",
        "asset_id": "token-1",
        "bucket_index": 0,
        "fee_rate_bps": "0",
        "id": "trade-fok-killed",
        "last_update": "1",
        "maker_address": "0xmaker",
        "maker_orders": [],
        "market": "0xcondition",
        "match_time": "1",
        "outcome": "Yes",
        "owner": "owner",
        "price": "0.35",
        "side": "SELL",
        "size": "5.28",
        "status": "CANCELED_order couldn't be fully filled. FOK orders are fully filled or killed.",
        "taker_order_id": "PM-OID-FOK",
        "timestamp": "1",
        "trade_owner": "owner",
        "trader_side": "TAKER",
        "type": "TRADE",
    })

    PolymarketExecutionClient._handle_ws_message(client, raw)

    assert handled == [raw]


def test_pm_order_without_market_metadata_delegates_to_upstream_limit(monkeypatch):
    calls = []

    async def upstream(_self, command, instrument):
        calls.append((command, instrument))

    monkeypatch.setattr(PolymarketExecutionClient, "_submit_limit_order", upstream)
    client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
    command = SimpleNamespace(order=SimpleNamespace(tags=[]))
    instrument = SimpleNamespace()

    _run(ArbPolymarketExecutionClient._submit_limit_order(client, command, instrument))

    assert calls == [(command, instrument)]


@pytest.mark.parametrize(
    ("side", "expected_amount", "expected_price", "expected_base_quantity"),
    [
        (OrderSide.BUY, 4.0, 0, 9.5),
        (OrderSide.SELL, 10.0, 0.01, None),
    ],
)
def test_pm_market_metadata_uses_official_market_order_at_submit_boundary(
    side,
    expected_amount,
    expected_price,
    expected_base_quantity,
):
    captured = {}
    signed_order = SignedOrderV2(
        salt="1",
        maker="0x" + "1" * 40,
        signer="0x" + "2" * 40,
        tokenId="123",
        makerAmount="4000000",
        takerAmount="9500000",
        side=PolySide.BUY,
        signatureType=SignatureTypeV2.EOA,
        timestamp="1",
        metadata="0x" + "0" * 64,
        builder="0x" + "0" * 64,
        signature="0x" + "3" * 130,
    )

    def create_market_order(args, *, options):
        captured["args"] = args
        captured["options"] = options
        return signed_order

    async def post_signed_order(
        order,
        signed,
        *,
        order_type_override,
        base_quantity=None,
    ):
        captured["posted"] = (order, signed, order_type_override, base_quantity)

    order = SimpleNamespace(
        strategy_id=StrategyId("S-1"),
        instrument_id=InstrumentId.from_str("0xcond-123.POLYMARKET"),
        client_order_id=ClientOrderId("O-MARKET"),
        side=side,
        quantity=10.0,
        price=0.4,
        tags=tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-market",
                pair_id="pair-market",
                leg_key="pm:yes:0",
                expected_legs=("pm:yes:0",),
                market=True,
            ),
        ),
    )
    client = SimpleNamespace(
        _http_client=SimpleNamespace(create_market_order=create_market_order),
        _clock=_Clock(),
        _get_neg_risk_for_instrument=lambda _instrument: True,
        _register_signed_order_id=lambda *args, **kwargs: captured.setdefault(
            "registered",
            (args, kwargs),
        ),
        generate_order_submitted=lambda **kwargs: captured.setdefault("submitted", kwargs),
        _post_signed_order=post_signed_order,
    )
    instrument = SimpleNamespace(size_precision=6)

    _run(
        ArbPolymarketExecutionClient._submit_limit_order(
            client,
            SimpleNamespace(order=order),
            instrument,
        ),
    )

    assert captured["args"].token_id == "123"
    assert captured["args"].amount == pytest.approx(expected_amount)
    assert captured["args"].side == ("BUY" if side == OrderSide.BUY else "SELL")
    assert captured["args"].price == pytest.approx(expected_price)
    assert captured["args"].order_type == PolyOrderType.FOK
    assert captured["options"].neg_risk is True
    _, posted_signed, posted_type, base_quantity = captured["posted"]
    assert posted_signed is signed_order
    assert posted_type == PolyOrderType.FOK
    if expected_base_quantity is None:
        assert base_quantity is None
    else:
        assert float(base_quantity) == pytest.approx(expected_base_quantity)


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
        is_post_only=False,
    )

    _run(client._post_signed_order(order, SimpleNamespace(), order_type_override="GTC"))

    assert captured == [(ClientOrderId("O-INFLIGHT"), "")]


def test_polymarket_post_only_is_forwarded_to_clob_submit():
    posted = []
    client = SimpleNamespace(
        _retry_manager_pool=_RetryPool(),
        _http_client=SimpleNamespace(
            post_order=lambda signed, order_type, post_only: posted.append(
                (signed, order_type, post_only),
            ) or {"success": False, "errorMsg": "test"},
        ),
        _clock=_Clock(),
        generate_order_rejected=lambda **_kwargs: None,
    )
    client._post_signed_order = PolymarketExecutionClient._post_signed_order.__get__(client)
    signed_order = SimpleNamespace()
    order = SimpleNamespace(
        strategy_id=StrategyId("S-1"),
        instrument_id=InstrumentId.from_str("0xcond-token.POLYMARKET"),
        client_order_id=ClientOrderId("O-POST-ONLY"),
        time_in_force="GTC",
        is_post_only=True,
    )

    _run(client._post_signed_order(order, signed_order, order_type_override="GTC"))

    assert posted == [(signed_order, "GTC", True)]


def test_polymarket_http_submit_rejection_is_not_ambiguous():
    response = SimpleNamespace(
        status_code=400,
        json=lambda: {"error": "not enough balance"},
    )
    exc = PolyApiException(resp=response)
    rejected = []
    client = SimpleNamespace(
        _retry_manager_pool=_FailedRetryPool(exc),
        _http_client=SimpleNamespace(post_order=lambda *_args: None),
        _clock=_Clock(),
        _handle_ambiguous_submit_failure=lambda *_args: pytest.fail(
            "HTTP 400 is a definite rejection",
        ),
        generate_order_rejected=lambda **kwargs: rejected.append(kwargs),
    )
    client._post_signed_order = PolymarketExecutionClient._post_signed_order.__get__(client)
    order = SimpleNamespace(
        strategy_id=StrategyId("S-1"),
        instrument_id=InstrumentId.from_str("0xcond-token.POLYMARKET"),
        client_order_id=ClientOrderId("O-REJECTED"),
        time_in_force="GTC",
        is_post_only=False,
    )

    _run(client._post_signed_order(order, SimpleNamespace(), order_type_override="GTC"))

    assert len(rejected) == 1
    assert rejected[0]["client_order_id"] == order.client_order_id
    assert "status_code=400" in rejected[0]["reason"]


def test_polymarket_transport_submit_failure_remains_ambiguous():
    exc = PolyApiException(error_msg="request timed out")
    ambiguous = []
    client = SimpleNamespace(
        _retry_manager_pool=_FailedRetryPool(exc),
        _http_client=SimpleNamespace(post_order=lambda *_args: None),
        _handle_ambiguous_submit_failure=lambda order, reason: ambiguous.append(
            (order.client_order_id, reason),
        ),
        generate_order_rejected=lambda **_kwargs: pytest.fail(
            "transport failure has unknown venue result",
        ),
    )
    client._post_signed_order = PolymarketExecutionClient._post_signed_order.__get__(client)
    order = SimpleNamespace(
        client_order_id=ClientOrderId("O-INFLIGHT"),
        time_in_force="GTC",
        is_post_only=False,
    )

    _run(client._post_signed_order(order, SimpleNamespace(), order_type_override="GTC"))

    assert ambiguous == [(order.client_order_id, str(exc))]


def test_arb_inflight_query_updates_order_without_changing_liveness():
    calls = []

    report = SimpleNamespace()
    client = SimpleNamespace(
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
        ("query", False),
        ("update", report),
    ]


def test_arb_inflight_query_failure_does_not_change_liveness_or_session():
    calls = []

    client = SimpleNamespace(
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

    assert calls == [("query", False)]


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


def test_polymarket_cancel_order_success_generates_canceled_event_and_ends_session():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [venue_order_id],
        "not_canceled": {},
    })

    _run(client._cancel_order(command))

    assert captured["canceled"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["canceled"]["venue_order_id"] == expected_venue_order_id
    assert "rejected" not in captured
    assert captured["cancel_ack"] == (ClientOrderId("O-1"), expected_venue_order_id)

    # 迟到的 USER WS CANCELLATION 复用同一 helper,不得重复发送撤单终态。
    client._generate_cancel_success_event(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1.POLYMARKET"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=expected_venue_order_id,
        ts_event=123,
    )

    assert "rejected" not in captured


def test_polymarket_deferred_cancel_success_generates_canceled_event_and_ends_session():
    venue_order_id = "0x" + "a" * 64
    client, command, captured, expected_venue_order_id = _cancel_test_client({
        "canceled": [venue_order_id],
        "not_canceled": {},
    })
    client._execute_deferred_cancel = (
        PolymarketExecutionClient._execute_deferred_cancel.__get__(client)
    )
    order = client._cache.order(command.client_order_id)

    _run(client._execute_deferred_cancel(order, expected_venue_order_id))

    assert captured["canceled"]["client_order_id"] == ClientOrderId("O-1")
    assert captured["canceled"]["venue_order_id"] == expected_venue_order_id
    assert captured["cancel_ack"] == (ClientOrderId("O-1"), expected_venue_order_id)
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


def _tracking_cancel_client(begin_returns):
    """`_cancel_test_client` 变体:记录 venue 撤单是否真被调用,并可控 `_begin_cancel_session` 返回值。"""
    venue_order_id_str = "0x" + "a" * 64
    client, command, _captured, _voi = _cancel_test_client({
        "canceled": [venue_order_id_str],
        "not_canceled": {},
    })
    calls = []

    def _cancel_order_http(payload):
        calls.append(payload)
        return {"canceled": [venue_order_id_str], "not_canceled": {}}

    client._http_client = SimpleNamespace(cancel_order=_cancel_order_http)
    client._begin_cancel_session = lambda _order: begin_returns
    return client, command, calls


def test_polymarket_residual_cancel_reaches_venue_despite_active_session():
    # 残单撤单:cancel session 已由 base `_cancel_residual_orders` 同步预开 → 里层 begin 会返回 False。
    # `session_started=True` 必须让 `_cancel_order` 跳过 begin 守卫、仍把撤单发到 venue。
    # 回归 PM 撤残单自撞 bug:此前无旁路 → begin 返 False → 在守卫处 return,venue 撤单永不发出。
    client, command, calls = _tracking_cancel_client(begin_returns=False)

    _run(client._cancel_order(command, session_started=True))

    assert len(calls) == 1  # venue 撤单确实发出


def test_polymarket_explicit_cancel_bails_when_session_already_active():
    # 显式 CancelOrder(session_started 默认 False):begin 返回 False = 该单已有撤单在飞,
    # 守卫应短路、不重复发 venue 撤单(保留去重语义,不被上面的旁路破坏)。
    client, command, calls = _tracking_cancel_client(begin_returns=False)

    _run(client._cancel_order(command))

    assert calls == []


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
    assert captured["cancel_ack"] == (ClientOrderId("O-1"), expected_venue_order_id)
    assert "canceled" not in captured


def test_polymarket_cancel_order_unknown_result_ends_session():
    # 用户改:除「200 且本单在 canceled 列表」外一律结束 session;unknown/无回执也 ack 结束
    # session(没有本次撤单对应的 WS 终态可等)。
    client, command, captured, expected_venue_order_id = _cancel_test_client(None)

    _run(client._cancel_order(command))

    assert captured["cancel_ack"] == (ClientOrderId("O-1"), expected_venue_order_id)
    assert "rejected" not in captured
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

    def check_dust_residual(self, _venue_order_id):
        return None  # 这两个用例不测 dust 收口(#280);无残量


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
    client._should_book_early_fill = (
        PolymarketExecutionClient._should_book_early_fill.__get__(client)
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


def test_polymarket_realtime_fill_books_early_when_opted_in():
    """opt-in(`_should_book_early_fill` → True)时 MATCHED 即记账,不等 CONFIRMED;
    随后 CONFIRMED 由 per-fill 去重挡掉,不重复记。"""
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
    client._should_book_early_fill = lambda _vid: True
    client._truncate_ordered_dict = PolymarketExecutionClient._truncate_ordered_dict.__get__(client)
    client._record_processed_fill = PolymarketExecutionClient._record_processed_fill.__get__(client)
    client._record_processed_trade = PolymarketExecutionClient._record_processed_trade.__get__(client)
    client._handle_user_trade_in_ws_trade_msg = (
        PolymarketExecutionClient._handle_user_trade_in_ws_trade_msg.__get__(client)
    )

    # MATCHED already books the fill (order still open).
    client._handle_user_trade_in_ws_trade_msg(
        _PMTradeMsg(PolymarketTradeStatus.MATCHED),
        trade_id="trade-1",
        wait_for_ack=False,
        order_id="PM-OID-1",
    )
    assert len(captured) == 1
    assert captured[0]["last_qty"].as_double() == pytest.approx(5.0)

    # CONFIRMED for the same trade is deduped — no second fill.
    client._handle_user_trade_in_ws_trade_msg(
        _PMTradeMsg(PolymarketTradeStatus.CONFIRMED),
        trade_id="trade-1",
        wait_for_ack=False,
        order_id="PM-OID-1",
    )
    assert len(captured) == 1


def test_arb_should_book_early_fill_only_when_enable_timeout_false():
    """套利子类只对 `enable_timeout=false` 的主单提前(MATCHED)记账;true / 无 arb tag / 未知单不提前。"""
    cache = TestComponentStubs.cache()
    inst = pm_instrument("ATP", "home", token="tok1")
    cache.add_instrument(inst)
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    )

    def _tagged_order(enable_timeout: bool, vid: str):
        meta = OpportunityMeta(
            opportunity_id="OPP",
            pair_id="P",
            leg_key="polymarket:yes:0",
            expected_legs=("polymarket:yes:0",),
            enable_timeout=enable_timeout,
        )
        order = factory.limit(
            inst.id, OrderSide.BUY, inst.make_qty(5), inst.make_price(0.42),
            tags=tags_from_meta(meta),
        )
        cache.add_order(order)
        cache.add_venue_order_id(order.client_order_id, VenueOrderId(vid))

    _tagged_order(enable_timeout=False, vid="PM-OID-F")
    _tagged_order(enable_timeout=True, vid="PM-OID-T")
    plain = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(5), inst.make_price(0.42))
    cache.add_order(plain)
    cache.add_venue_order_id(plain.client_order_id, VenueOrderId("PM-OID-P"))

    client = SimpleNamespace(_cache=cache)
    hook = ArbPolymarketExecutionClient._should_book_early_fill.__get__(client)

    assert hook(VenueOrderId("PM-OID-F")) is True
    assert hook(VenueOrderId("PM-OID-T")) is False
    assert hook(VenueOrderId("PM-OID-P")) is False
    assert hook(VenueOrderId("PM-OID-UNKNOWN")) is False


def test_polymarket_realtime_fill_dust_residual_closes_via_cancel():
    """#280:fill 后累计只差 dust 尾量 → fill-handler 源头本地 cancel 收口,不补 synthetic fill。

    真实 fill 照发(实际成交量),额外经 `_generate_cancel_success_event` 撤掉未成交尾量收口;
    绝不发第二笔(synthetic)fill —— 那会动仓(reduce-SELL 卖穿成 SHORT / BUY 造 phantom LONG)。
    """
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

    class _DustTracker(_PMFillTracker):
        def check_dust_residual(self, _venue_order_id):
            return 0.002  # 非 None = 检测到 dust 尾量

    client = SimpleNamespace(
        account_id=AccountId("POLYMARKET-001"),
        _api_key="api-key",
        _cache=cache,
        _clock=_Clock(),
        _fill_tracker=_DustTracker(),
        _finalized_trades=OrderedDict(),
        _log=_Log(),
        _processed_fills=OrderedDict(),
        _processed_trades=OrderedDict(),
        _wallet_address="0xwallet",
        PROCESSED_TRADES_LIMIT=100,
    )
    filled = []
    cancels = []
    client.generate_order_filled = lambda **kwargs: filled.append(kwargs)
    client._generate_cancel_success_event = lambda **kwargs: cancels.append(kwargs)
    client._truncate_ordered_dict = PolymarketExecutionClient._truncate_ordered_dict.__get__(client)
    client._record_processed_fill = PolymarketExecutionClient._record_processed_fill.__get__(client)
    client._record_processed_trade = PolymarketExecutionClient._record_processed_trade.__get__(client)
    client._handle_user_trade_in_ws_trade_msg = (
        PolymarketExecutionClient._handle_user_trade_in_ws_trade_msg.__get__(client)
    )
    client._should_book_early_fill = (
        PolymarketExecutionClient._should_book_early_fill.__get__(client)
    )

    client._handle_user_trade_in_ws_trade_msg(
        _PMTradeMsg(PolymarketTradeStatus.CONFIRMED),
        trade_id="trade-dust",
        wait_for_ack=False,
        order_id="PM-OID-1",
    )

    assert len(filled) == 1                       # 真实 fill 照发(唯一一笔,非 synthetic)
    assert len(cancels) == 1                      # dust 尾量经本地 cancel 收口
    assert cancels[0]["client_order_id"] == order.client_order_id


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
    client._should_book_early_fill = (
        PolymarketExecutionClient._should_book_early_fill.__get__(client)
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


def test_polymarket_external_taker_fill_bootstraps_order_before_fill():
    """手动 taker 成交没有 PLACEMENT；先建 EXTERNAL order，真实 fill 才能推进 NT Position。"""
    cache = TestComponentStubs.cache()
    inst = pm_instrument("ATP", "home", token="tok1")
    cache.add_instrument(inst)
    client = SimpleNamespace(
        account_id=AccountId("POLYMARKET-001"),
        _api_key="api-key",
        _cache=cache,
        _clock=_Clock(),
        _finalized_trades=OrderedDict(),
        _log=_Log(),
        _processed_fills=OrderedDict(),
        _processed_trades=OrderedDict(),
        _wallet_address="0xwallet",
        PROCESSED_TRADES_LIMIT=100,
    )
    sent = []
    client._send_order_status_report = lambda report: sent.append(("order", report))
    client._send_fill_report = lambda report: sent.append(("fill", report))
    client._should_book_early_fill = lambda _venue_order_id: False
    client._truncate_ordered_dict = PolymarketExecutionClient._truncate_ordered_dict.__get__(client)
    client._record_processed_fill = PolymarketExecutionClient._record_processed_fill.__get__(client)
    client._record_processed_trade = PolymarketExecutionClient._record_processed_trade.__get__(client)
    client._handle_user_trade_in_ws_trade_msg = (
        PolymarketExecutionClient._handle_user_trade_in_ws_trade_msg.__get__(client)
    )
    msg = PolymarketUserTrade(
        asset_id="tok1",
        bucket_index=0,
        fee_rate_bps="0",
        id="manual-trade-1",
        last_update="1710000000",
        maker_address="0xwallet",
        maker_orders=[],
        market="0xcond",
        match_time="1710000000",
        outcome="Home",
        owner="api-key",
        price="0.26",
        side=PolymarketOrderSide.SELL,
        size="30",
        status=PolymarketTradeStatus.CONFIRMED,
        taker_order_id="manual-order-1",
        timestamp="1710000000000",
        trade_owner="api-key",
        trader_side=PolymarketLiquiditySide.TAKER,
        type=PolymarketEventType.TRADE,
    )

    client._handle_user_trade_in_ws_trade_msg(
        msg,
        trade_id=TradeId("manual-trade-1"),
        wait_for_ack=False,
        order_id="manual-order-1",
    )

    assert [kind for kind, _ in sent] == ["order", "fill"]
    order_report = sent[0][1]
    fill_report = sent[1][1]
    assert order_report.client_order_id is None
    assert order_report.order_status == OrderStatus.ACCEPTED
    assert order_report.order_side == OrderSide.SELL
    assert order_report.quantity == fill_report.last_qty
    assert order_report.filled_qty.as_double() == 0.0
    assert order_report.price == fill_report.last_px


def test_arb_generate_position_reports_settles_without_writing_liveness(monkeypatch):
    """适配器只生成仓位批次，liveness 由调用它的 reconciliation 管理。"""
    position_calls = []
    settlement_calls = []
    balance_calls = []

    async def fake_super(self, command):
        position_calls.append(command)
        self._last_raw_positions = [
            {"conditionId": "cond1", "size": "10", "negativeRisk": True, "redeemable": False},
        ]
        return [SimpleNamespace(name="report")]

    class _Settlement:
        async def run(self, positions):
            settlement_calls.append(positions)
            return SettlementResult()

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._settlement = _Settlement()
        client._settlement_inflight = False
        client._loop = asyncio.get_running_loop()
        state = _stable_reconciliation_state(client)

        async def refresh_balance():
            balance_calls.append("balance_refresh")

        client._update_account_state = refresh_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())
        assert [report.name for report in reports] == ["report"]
        assert not client._venue_liveness.position_alive(POLYMARKET)
        assert client._settlement_inflight is False
        assert reports.snapshot.is_current_for_instruments(client, [])
        state["version"] += 1
        assert not reports.snapshot.is_current_for_instruments(client, [])

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())

    assert balance_calls == ["balance_refresh"]
    assert len(position_calls) == 1
    assert settlement_calls == [[SettlementPosition("cond1", 10.0, neg_risk=True, redeemable=False)]]


@pytest.mark.parametrize("merge_success", [True, False])
def test_settlement_attempt_refetches_positions_before_returning_reports(
    monkeypatch,
    merge_success,
):
    """merge 一旦尝试，无论结果如何都重拉 positions，不能把旧 LONG 交给 NT。"""
    calls = []
    liveness = VenueExecutionLiveness()
    responses = [
        (
            [{"conditionId": "cond1", "asset": "yes", "size": "10"}],
            [SimpleNamespace(name="pre-report")],
        ),
        (
            [],
            [SimpleNamespace(name="post-report")],
        ),
    ]

    async def fake_super(self, command):
        calls.append("positions")
        raw, reports = responses.pop(0)
        self._last_raw_positions = raw
        return reports

    class _Settlement:
        async def run(self, positions):
            calls.append("merge")
            return SettlementResult(
                merges=[
                    TxResult(
                        success=merge_success,
                        tx_hash="0xmerge" if merge_success else "",
                        message="" if merge_success else "reverted",
                    ),
                ],
            )

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = liveness
        client._settlement = _Settlement()
        client._settlement_inflight = False
        client._realized_pnl_ledger = None
        _stable_reconciliation_state(client)

        async def refresh_balance():
            calls.append("balance")
            return None

        async def load_realized(raw):
            calls.append("closed")
            assert raw == []
            return None

        client._update_account_state = refresh_balance
        client._load_realized_pnl_snapshot = load_realized

        reports = await client.generate_position_status_reports(SimpleNamespace())
        assert [report.name for report in reports] == ["post-report"]
        assert not client._venue_liveness.position_alive(POLYMARKET)
        assert responses == []
        assert calls == ["positions", "merge", "positions", "closed", "balance"]

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)
    _run(scenario())


def test_arb_generate_position_reports_balance_refresh_failure_does_not_fail_reconcile(monkeypatch):
    async def fake_super(self, command):
        self._last_raw_positions = []
        return [SimpleNamespace(name="report")]

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._settlement = None
        client._settlement_inflight = False
        client._loop = asyncio.get_running_loop()
        client._realized_pnl_ledger = None
        _stable_reconciliation_state(client)

        async def fail_balance():
            raise RuntimeError("balance unavailable")

        client._update_account_state = fail_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert [report.name for report in reports] == ["report"]
        assert not client._venue_liveness.position_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())


def test_position_reconcile_returns_stale_guard_when_state_changes_during_fetch(monkeypatch):
    state = {"version": 0}
    settlement_calls = []

    async def fake_super(self, command):
        self._last_raw_positions = [{"conditionId": "0xc", "asset": "1", "size": "10"}]
        state["version"] += 1
        return [SimpleNamespace(name="stale-report")]

    class _Settlement:
        async def run(self, positions):
            settlement_calls.append(positions)
            return SettlementResult()

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_position_alive(POLYMARKET)
        client._settlement = _Settlement()
        client._settlement_inflight = False
        client._realized_pnl_ledger = None
        _stable_reconciliation_state(client, state)

        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert client._venue_liveness.position_alive(POLYMARKET)
        assert not reports.snapshot.is_current_for_instruments(client, [])
        assert settlement_calls

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)
    _run(scenario())


def test_position_reconcile_defers_realized_when_state_changes_during_closed_fetch(
    monkeypatch,
):
    state = {"version": 0}
    instrument_id = "0xc-1.POLYMARKET"

    async def fake_super(self, command):
        self._last_raw_positions = [
            {"conditionId": "0xc", "asset": "1", "size": "0", "realizedPnl": "9.0"},
        ]
        return [SimpleNamespace(name="stale-report")]

    async def scenario():
        ledger = RealizedPnlLedger()
        ledger.replace_instrument_snapshot(
            "POLYMARKET-001",
            external_realized={instrument_id: 4.0},
            native_realized={},
        )
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_position_alive(POLYMARKET)
        client._settlement = None
        client._settlement_inflight = False
        client._realized_pnl_ledger = ledger
        _stable_reconciliation_state(client, state)

        async def fetch_closed_positions():
            ledger.replace_instrument_snapshot(
                "POLYMARKET-001",
                external_realized={instrument_id: 5.0},
                native_realized={},
            )
            return []

        async def refresh_balance():
            return None

        client._fetch_closed_positions = fetch_closed_positions
        client._refresh_account_state_after_position_reconcile = refresh_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert client._venue_liveness.position_alive(POLYMARKET)
        # #318:realized_revision 已删 —— 快照不再侦测 fetch 期间的 ledger bump(其职责改由 position_digest
        # 覆盖 cache realized_pnl);realized payload 照常加载,offset 由 engine 侧选择性 commit。
        assert reports.payload == {instrument_id: 9.0}
        assert ledger.instrument_adjustment(
            instrument_id,
            "POLYMARKET-001",
        ) == pytest.approx(5.0)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)
    _run(scenario())


def test_position_reconcile_returns_stale_guard_when_state_changes_during_balance_refresh(
    monkeypatch,
):
    state = {"version": 0}

    async def fake_super(self, command):
        self._last_raw_positions = []
        return [SimpleNamespace(name="stale-report")]

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_position_alive(POLYMARKET)
        client._settlement = None
        client._settlement_inflight = False
        client._realized_pnl_ledger = None
        _stable_reconciliation_state(client, state)

        async def refresh_balance():
            state["version"] += 1

        client._refresh_account_state_after_position_reconcile = refresh_balance

        reports = await client.generate_position_status_reports(SimpleNamespace())

        assert client._venue_liveness.position_alive(POLYMARKET)
        assert not reports.snapshot.is_current_for_instruments(client, [])

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)
    _run(scenario())


def test_arb_generate_fill_reports_returns_empty_without_trades_api(monkeypatch):
    """#279:arb PM reconcile 不拉 trades API —— `generate_fill_reports` 恒返回 `[]`,
    且**不调用上游**(上游拉 trades:启动可超时抛异常连坐 mass-status,连续会把持仓拖进
    fill 挂历史母单的脆弱路径)。position 对账走纯 NET 快照,不需要真 fill;live 成交走 WS。"""

    async def boom_super(self, command):
        raise AssertionError("upstream generate_fill_reports must not be called under arb reconcile")

    monkeypatch.setattr(PolymarketExecutionClient, "generate_fill_reports", boom_super)

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        result = await client.generate_fill_reports(SimpleNamespace())
        assert result == []

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


def test_position_reconcile_sets_external_minus_native_realized_baseline():
    instrument_id = "0xc-1.POLYMARKET"

    async def scenario():
        ledger = RealizedPnlLedger()
        client = SimpleNamespace(
            _realized_pnl_ledger=ledger,
            account_id=AccountId("POLYMARKET-001"),
            _cache=SimpleNamespace(
                positions=lambda **kwargs: [
                    SimpleNamespace(realized_pnl=SimpleNamespace(as_double=lambda: 0.5)),
                ],
            ),
        )

        async def closed_positions():
            return [{"conditionId": "0xc", "asset": "1", "realizedPnl": "2.0"}]

        client._fetch_closed_positions = closed_positions
        load = ArbPolymarketExecutionClient._load_realized_pnl_snapshot.__get__(
            client,
        )
        external = await load([
            {"conditionId": "0xc", "asset": "1", "realizedPnl": "1.0"},
        ])
        commit = ArbPolymarketExecutionClient._commit_realized_pnl_snapshot.__get__(client)
        commit(external)

        assert ledger.instrument_adjustment(
            instrument_id,
            client.account_id,
        ) == pytest.approx(0.5)

    _run(scenario())


def test_arb_generate_position_reports_failure_does_not_write_liveness(monkeypatch):
    async def fake_super(self, command):
        raise RuntimeError("positions unavailable")

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        _stable_reconciliation_state(client)

        client._venue_liveness.mark_position_alive(POLYMARKET)
        # #259(修订 #122):失败 **重新抛出**。NT 判"venue 查询失败"只认异常
        # (`live/execution_engine.py:876` → `failed_venues`);返 [] 会被读成"查询成功、无持仓",
        # 使 `_did_position_status_query_fail` 跳过保护失效,连续对账合成成交抹掉真实持仓账面。
        with pytest.raises(RuntimeError, match="positions unavailable"):
            await client.generate_position_status_reports(SimpleNamespace())

        assert client._venue_liveness.position_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_position_status_reports", fake_super)

    _run(scenario())


class _FailedReportRetryManager:
    result = False
    message = "transport unavailable"
    last_exception = RuntimeError(message)

    async def run(self, *_args, **_kwargs):
        return None


class _FailedReportRetryPool:
    def __init__(self):
        self.manager = _FailedReportRetryManager()
        self.released = False

    async def acquire(self):
        return self.manager

    async def release(self, manager):
        assert manager is self.manager
        self.released = True


@pytest.mark.parametrize(
    ("method", "command"),
    [
        (
            PolymarketExecutionClient.generate_order_status_reports,
            SimpleNamespace(instrument_id=None),
        ),
        (
            PolymarketExecutionClient.generate_order_status_report,
            SimpleNamespace(
                instrument_id=InstrumentId.from_str("1.POLYMARKET"),
                client_order_id=ClientOrderId("O-1"),
                venue_order_id=VenueOrderId("V-1"),
            ),
        ),
        (
            PolymarketExecutionClient.generate_fill_reports,
            SimpleNamespace(instrument_id=None, start=None, end=None),
        ),
    ],
)
def test_polymarket_report_methods_restore_retry_manager_failure(method, command):
    """三个 report 入口都恢复原异常，并在异常路径归还 manager。"""

    async def scenario():
        pool = _FailedReportRetryPool()
        client = SimpleNamespace(
            _log=_Log(),
            _retry_manager_pool=pool,
            _http_client=SimpleNamespace(
                get_open_orders=lambda **_kwargs: [],
                get_order=lambda **_kwargs: None,
                get_trades=lambda **_kwargs: [],
            ),
            _maintain_active_market=lambda _instrument_id: _noop_async(),
        )

        with pytest.raises(RuntimeError, match="transport unavailable"):
            await method(client, command)

        assert pool.released

    _run(scenario())


def test_arb_generate_order_reports_failure_does_not_write_liveness(monkeypatch):
    async def fake_super(self, command):
        raise RuntimeError("transport unavailable")

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_order_alive(POLYMARKET)
        _stable_reconciliation_state(client)

        with pytest.raises(RuntimeError, match="transport unavailable"):
            await client.generate_order_status_reports(SimpleNamespace())

        assert client._venue_liveness.order_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_reports", fake_super)

    _run(scenario())


def test_arb_generate_single_order_report_failure_does_not_write_liveness(monkeypatch):
    async def fake_super(self, command, *, retry=True):
        raise RuntimeError("transport unavailable")

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_order_alive(POLYMARKET)
        _stable_reconciliation_state(client)

        with pytest.raises(RuntimeError, match="transport unavailable"):
            await client.generate_order_status_report(SimpleNamespace())

        assert client._venue_liveness.order_alive(POLYMARKET)

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_report", fake_super)

    _run(scenario())


def test_arb_generate_single_order_report_exempt_from_staleness_guard(monkeypatch):
    """#319:inflight-check 单数路径**不附 snapshot** → 落 `_reconciliation_report_is_current`

    的 `snapshot is None → return True` 恒放行,避免「订单存活」的解析报告被判 stale 丢掉致误 fail。
    """

    async def fake_super(self, command, *, retry=True):
        return SimpleNamespace(name="inflight-report")

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        # 即便 reconciliation state 就绪,单数路径也不该捕获/附摘要。
        _stable_reconciliation_state(client)

        report = await client.generate_order_status_report(SimpleNamespace())

        assert report.name == "inflight-report"
        assert not hasattr(report, "_arb_reconciliation_snapshot")

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_report", fake_super)
    _run(scenario())


def test_arb_generate_order_reports_returns_stale_guard_when_local_state_changes(monkeypatch):
    state = {"version": 0}

    async def fake_super(self, command):
        state["version"] += 1
        return [SimpleNamespace(name="stale-report")]

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        client._venue_liveness.mark_order_alive(POLYMARKET)
        _stable_reconciliation_state(client, state)

        reports = await client.generate_order_status_reports(SimpleNamespace())

        assert client._venue_liveness.order_alive(POLYMARKET)
        assert not reports.snapshot.is_current_for_instruments(client, [])

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_reports", fake_super)
    _run(scenario())


def test_arb_generate_order_reports_remembers_engine_boundary_state(monkeypatch):
    async def fake_super(self, command):
        return [SimpleNamespace(name="report")]

    async def scenario():
        client = ArbPolymarketExecutionClient.__new__(ArbPolymarketExecutionClient)
        client._venue_liveness = VenueExecutionLiveness()
        state = _stable_reconciliation_state(client)

        reports = await client.generate_order_status_reports(SimpleNamespace())
        assert [report.name for report in reports] == ["report"]
        assert reports.snapshot.is_current_for_instruments(client, [])

        state["version"] += 1
        assert not reports.snapshot.is_current_for_instruments(client, [])

    monkeypatch.setattr(PolymarketExecutionClient, "generate_order_status_reports", fake_super)
    _run(scenario())


def test_polymarket_factory_configures_v2_http_proxy(monkeypatch):
    """PM CLOB REST 必须吃项目 proxy_url,避免 WS/REST 走不同出口。"""
    get_polymarket_http_client.cache_clear()

    clients = []
    transports = []

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
            clients.append(kwargs)

        def close(self):
            pass

    class _HttpxTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            transports.append(self)

    class _ClobClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)
    monkeypatch.setattr(pm_transport.httpx, "HTTPTransport", _HttpxTransport)
    monkeypatch.setattr("nautilus_trader.adapters.polymarket.factories.ClobClient", _ClobClient)
    monkeypatch.setenv("https_proxy", "http://env-should-lose:1")

    get_polymarket_http_client(
        api_key="K",
        api_secret="S",
        passphrase="P",
        private_key="0x" + "1" * 64,
        funder="0x" + "2" * 40,
        proxy_url="http://127.0.0.1:7890",
    )

    assert [transport.kwargs for transport in transports] == [{
        "http2": True,
        "proxy": "http://127.0.0.1:7890",
        "trust_env": False,
        "retries": 1,
    }]
    assert clients == [{
        "transport": transports[0],
        "trust_env": False,
        "timeout": pm_transport.httpx.Timeout(5.0, connect=15.0),
    }]
    assert old_client.closed


def test_polymarket_transport_direct_when_unconfigured(monkeypatch):
    """#276:proxy_url 未配置 → 直连,不读代理环境变量(即使 env 有值)。"""
    old_client = SimpleNamespace(close=lambda: None)
    transports = []
    clients = []

    class _HttpxTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            transports.append(self)

    class _HttpxClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

    monkeypatch.setattr(pm_transport.clob_http_helpers, "_http_client", old_client)
    monkeypatch.setattr(pm_transport, "_configured_proxy_url", pm_transport._UNCONFIGURED)
    monkeypatch.setattr(pm_transport.httpx, "HTTPTransport", _HttpxTransport)
    monkeypatch.setattr(pm_transport.httpx, "Client", _HttpxClient)
    monkeypatch.setenv("https_proxy", "http://env-should-be-ignored:1")

    pm_transport.configure_clob_http_transport(None)

    assert [transport.kwargs for transport in transports] == [{
        "http2": True,
        "proxy": None,
        "trust_env": False,
        "retries": 1,
    }]
    assert clients == [{
        "transport": transports[0],
        "trust_env": False,
        "timeout": pm_transport.httpx.Timeout(5.0, connect=15.0),
    }]


def test_relayer_transport_explicit_proxy_or_direct(monkeypatch):
    """#276:relayer SDK requests swap —— 显式 proxy 进 Session,未配置直连且 trust_env=False。"""
    import requests as real_requests
    from py_builder_relayer_client.http_helpers import helpers as relayer_helpers

    original = relayer_helpers.requests
    try:
        monkeypatch.setattr(pm_transport, "_relayer_configured_proxy_url", pm_transport._UNCONFIGURED)

        pm_transport.configure_relayer_http_transport("http://127.0.0.1:7890")
        shim = relayer_helpers.requests
        session = shim.request.__self__
        assert session.trust_env is False
        assert session.proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        assert shim.RequestException is real_requests.RequestException
        assert shim.JSONDecodeError is real_requests.JSONDecodeError

        pm_transport.configure_relayer_http_transport(None)
        session = relayer_helpers.requests.request.__self__
        assert session.trust_env is False
        assert session.proxies == {}
    finally:
        relayer_helpers.requests = original


def test_polymarket_balance_query_failure_is_not_retried():
    calls = []

    def fail_balance(_params):
        calls.append("attempt")
        raise PolyApiException(error_msg="transport unavailable")

    async def scenario():
        client = SimpleNamespace(
            _log=_Log(),
            _config=SimpleNamespace(signature_type=2),
            _http_client=SimpleNamespace(get_balance_allowance=fail_balance),
        )

        with pytest.raises(PolyApiException, match="transport unavailable"):
            await PolymarketExecutionClient._update_account_state(client)

    _run(scenario())
    assert calls == ["attempt"]


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
