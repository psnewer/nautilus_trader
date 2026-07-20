"""PM 回执即 ack:HTTP 下单回执生成 OrderAccepted,与 WS PLACEMENT 双路去重。

背景(2026-07-18 实盘):taker 秒成交单不挂簿,PM 用户频道不发 PLACEMENT;
ack 只接 WS 时订单停在 SUBMITTED → inflight-check 兜底出 0 价 inferred fill,
session 预扣/终态跟踪全断。修复 = HTTP 成功回执处 ack + `_accepted_emitted` 去重。
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.common.enums import PolymarketEventType
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderStatus
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderType
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserOrder
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.stubs.execution import TestExecStubs


_MARKET = "0xmarket"
_ASSET_ID = "123"
_WALLET = "0x" + "1" * 40


def _instrument() -> BinaryOption:
    instrument_id = get_polymarket_instrument_id(_MARKET, _ASSET_ID)
    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=instrument_id.symbol,
        outcome="YES",
        description="Test Polymarket Instrument",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        size_precision=2,
        size_increment=Quantity.from_str("0.01"),
        activation_ns=0,
        expiration_ns=0,
        max_quantity=None,
        min_quantity=Quantity.from_int(1),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


def _make_client(loop: asyncio.AbstractEventLoop) -> PolymarketExecutionClient:
    http_client = MagicMock()
    http_client.get_address.return_value = _WALLET
    http_client.builder.funder = _WALLET
    http_client.creds.api_key = "test-api-key"
    client = PolymarketExecutionClient(
        loop=loop,
        http_client=http_client,
        msgbus=MessageBus(trader_id=TraderId("TESTER-001"), clock=LiveClock()),
        cache=Cache(),
        clock=LiveClock(),
        instrument_provider=MagicMock(spec=PolymarketInstrumentProvider),
        ws_auth=MagicMock(),
        config=PolymarketExecClientConfig(),
        name=None,
    )
    client.generate_order_accepted = MagicMock()
    client.generate_order_rejected = MagicMock()
    return client


class _FakeRetryManager:
    def __init__(self, response) -> None:
        self._response = response
        self.message = None

    async def run(self, name, details, func, *args):
        return self._response


class _FakeRetryPool:
    def __init__(self, response) -> None:
        self._response = response

    async def acquire(self):
        return _FakeRetryManager(self._response)

    async def release(self, retry_manager) -> None:
        pass


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _submitted_order(client: PolymarketExecutionClient, instrument: BinaryOption):
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(0.5),
        quantity=instrument.make_qty(10),
    )
    client._cache.add_order(order, None)
    TestExecStubs.make_submitted_order(order)
    client._cache.update_order(order)
    return order


# ── 去重标记 ──────────────────────────────────────────────────────────


def test_mark_accepted_emitted_first_true_then_false():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        coid = ClientOrderId("ARB-1")

        assert client._mark_accepted_emitted(coid) is True
        assert client._mark_accepted_emitted(coid) is False
        assert client._mark_accepted_emitted(ClientOrderId("ARB-2")) is True
    finally:
        loop.close()


def test_mark_accepted_emitted_truncates_oldest():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        limit = PolymarketExecutionClient.PROCESSED_TRADES_LIMIT
        for i in range(limit + 1):
            client._mark_accepted_emitted(ClientOrderId(f"ARB-{i}"))

        assert len(client._accepted_emitted) == limit
        assert ClientOrderId("ARB-0") not in client._accepted_emitted  # 最老的被挤出
        # 被挤出后重触发允许(上限保护 vs 严格一次性的权衡;10k 窗口内不会实际发生)
        assert client._mark_accepted_emitted(ClientOrderId("ARB-0")) is True
    finally:
        loop.close()


# ── HTTP 回执即 ack ──────────────────────────────────────────────────


def test_post_signed_order_success_generates_accepted_once():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._retry_manager_pool = _FakeRetryPool({"success": True, "orderID": "0xabc"})

        _run(client._post_signed_order(order, MagicMock()))

        client.generate_order_accepted.assert_called_once()
        kwargs = client.generate_order_accepted.call_args.kwargs
        assert kwargs["client_order_id"] == order.client_order_id
        assert kwargs["venue_order_id"] == VenueOrderId("0xabc")
    finally:
        loop.close()


def test_post_signed_order_skips_accepted_when_ws_acked_first():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._retry_manager_pool = _FakeRetryPool({"success": True, "orderID": "0xabc"})
        client._mark_accepted_emitted(order.client_order_id)  # WS PLACEMENT 先到已 ack

        _run(client._post_signed_order(order, MagicMock()))

        client.generate_order_accepted.assert_not_called()
    finally:
        loop.close()


def test_post_signed_order_failure_no_accepted():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._retry_manager_pool = _FakeRetryPool({"success": False, "errorMsg": "nope"})

        _run(client._post_signed_order(order, MagicMock()))

        client.generate_order_accepted.assert_not_called()
        client.generate_order_rejected.assert_called_once()
    finally:
        loop.close()


def test_process_batch_response_generates_accepted_once():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)

        client._process_batch_response(
            [order, order],
            [
                {"success": True, "orderID": "0xabc"},
                {"success": True, "orderID": "0xabc"},  # 重复回执不重复 ack
            ],
        )

        client.generate_order_accepted.assert_called_once()
    finally:
        loop.close()


# ── WS PLACEMENT 幂等 ────────────────────────────────────────────────


def _placement_msg(venue_order_id: str) -> PolymarketUserOrder:
    return PolymarketUserOrder(
        asset_id=_ASSET_ID,
        associate_trades=None,
        created_at="0",
        expiration=None,
        id=venue_order_id,
        maker_address=_WALLET,
        market=_MARKET,
        order_owner="owner",
        order_type=PolymarketOrderType.GTC,
        original_size="10",
        outcome="Yes",
        owner="owner",
        price="0.5",
        side=PolymarketOrderSide.BUY,
        size_matched="0",
        status=PolymarketOrderStatus.LIVE,
        timestamp="1700000000000",
        type=PolymarketEventType.PLACEMENT,
    )


def test_ws_placement_acks_submitted_order():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_ws_order_msg(_placement_msg("0xabc"), wait_for_ack=False)

        client.generate_order_accepted.assert_called_once()
    finally:
        loop.close()


def test_ws_placement_skips_when_http_acked_first():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)  # 事件异步 apply,状态可能还没翻到 ACCEPTED
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))
        client._mark_accepted_emitted(order.client_order_id)  # HTTP 回执已 ack

        client._handle_ws_order_msg(_placement_msg("0xabc"), wait_for_ack=False)

        client.generate_order_accepted.assert_not_called()
    finally:
        loop.close()
