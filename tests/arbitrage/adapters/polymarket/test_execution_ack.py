"""PM ack:OrderAccepted 只来自 WS(order 消息 PLACEMENT/CANCELLATION/UPDATE,或 trade 消息
MATCHED/MINED/CONFIRMED 任一先到达),不再由 HTTP 下单回执触发(#PM-ack-v2)。

背景:2026-07-18 实盘暴露过"HTTP 回执即 ack"路径本身的 0 价 inferred fill 问题;
复盘后用户判断 —— taker 单没有 PLACEMENT,但 MATCHED/MINED/CONFIRMED 任一到达即证明
订单已被 venue 接收,不需要额外信任一次性的 HTTP 响应作为 ack 信号,PLACEMENT/CANCELLATION/
UPDATE 同理(#256 追加)。HTTP 回执现在只做 venue_order_id 预索引(`cache.add_venue_order_id`),
不再触发 `generate_order_accepted`;去重仍靠 `_accepted_emitted`(五个来源共用同一张表,
谁先到谁 ack)。
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.common.enums import PolymarketEventType
from nautilus_trader.adapters.polymarket.common.enums import PolymarketLiquiditySide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderStatus
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderType
from nautilus_trader.adapters.polymarket.common.enums import PolymarketTradeStatus
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserOrder
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TradeId
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
    client.generate_order_filled = MagicMock()
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


# ── HTTP 回执:只索引 venue_order_id,不 ack ──────────────────────────


def test_post_signed_order_success_indexes_venue_order_id_but_no_accepted():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._retry_manager_pool = _FakeRetryPool({"success": True, "orderID": "0xabc"})

        _run(client._post_signed_order(order, MagicMock()))

        client.generate_order_accepted.assert_not_called()
        assert client._cache.client_order_id(VenueOrderId("0xabc")) == order.client_order_id
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


def test_process_batch_response_indexes_venue_order_id_but_no_accepted():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)

        client._process_batch_response(
            [order],
            [{"success": True, "orderID": "0xabc"}],
        )

        client.generate_order_accepted.assert_not_called()
        assert client._cache.client_order_id(VenueOrderId("0xabc")) == order.client_order_id
    finally:
        loop.close()


# ── WS PLACEMENT 幂等 ────────────────────────────────────────────────


def _order_msg(
    venue_order_id: str,
    event_type: PolymarketEventType,
    order_status: PolymarketOrderStatus = PolymarketOrderStatus.LIVE,
) -> PolymarketUserOrder:
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
        status=order_status,
        timestamp="1700000000000",
        type=event_type,
    )


def _placement_msg(venue_order_id: str) -> PolymarketUserOrder:
    return _order_msg(venue_order_id, PolymarketEventType.PLACEMENT)


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


def test_ws_placement_skips_when_already_acked():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)  # 事件异步 apply,状态可能还没翻到 ACCEPTED
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))
        client._mark_accepted_emitted(order.client_order_id)  # 已经被别的来源(如 trade 消息)ack 过

        client._handle_ws_order_msg(_placement_msg("0xabc"), wait_for_ack=False)

        client.generate_order_accepted.assert_not_called()
    finally:
        loop.close()


# ── WS UPDATE/CANCELLATION 也 ack(#256 追加)────────────────────────


def test_update_acks_submitted_order():
    """UPDATE 消息(非 MATCHED,如普通状态刷新)本身也证明订单已被接收,应该 ack。"""
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_ws_order_msg(
            _order_msg("0xabc", PolymarketEventType.UPDATE, PolymarketOrderStatus.LIVE),
            wait_for_ack=False,
        )

        client.generate_order_accepted.assert_called_once()
    finally:
        loop.close()


def test_cancellation_acks_submitted_order_before_cancel_event():
    """CANCELLATION 到达时若订单还没 ack(taker 单无 PLACEMENT、且没赶上任何 trade 消息就被撤),
    也要先补 ack,再走既有的撤单终态流程。"""
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        client.generate_order_canceled = MagicMock()
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_ws_order_msg(
            _order_msg("0xabc", PolymarketEventType.CANCELLATION),
            wait_for_ack=False,
        )

        client.generate_order_accepted.assert_called_once()
        client.generate_order_canceled.assert_called_once()
    finally:
        loop.close()


def test_update_skips_ack_when_already_acked():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))
        client._mark_accepted_emitted(order.client_order_id)

        client._handle_ws_order_msg(
            _order_msg("0xabc", PolymarketEventType.UPDATE, PolymarketOrderStatus.LIVE),
            wait_for_ack=False,
        )

        client.generate_order_accepted.assert_not_called()
    finally:
        loop.close()


# ── WS trade 消息(MATCHED/MINED/CONFIRMED)ack ───────────────────────


def _trade_msg(order_id: str, status: PolymarketTradeStatus) -> PolymarketUserTrade:
    """taker 侧 trade 消息(trader_side=TAKER):`get_filled_user_order_ids` 直接返回 `[taker_order_id]`,
    不依赖 `maker_orders`(taker 单没有 PLACEMENT,靠这条消息本身证明订单已被接收)。"""
    return PolymarketUserTrade(
        asset_id=_ASSET_ID,
        bucket_index=0,
        fee_rate_bps="0",
        id="trade-1",
        last_update="1700000000000",
        maker_address=_WALLET,
        maker_orders=[],
        market=_MARKET,
        match_time="1700000000",
        outcome="Yes",
        owner="owner",
        price="0.5",
        side=PolymarketOrderSide.BUY,
        size="10",
        status=status,
        taker_order_id=order_id,
        timestamp="1700000000000",
        trade_owner="owner",
        trader_side=PolymarketLiquiditySide.TAKER,
        type=PolymarketEventType.TRADE,
    )


def test_matched_trade_acks_taker_order_without_placement():
    """taker 单没有 PLACEMENT;MATCHED(非 finalized)本身就是唯一的接收证明,须 ack。"""
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_user_trade_in_ws_trade_msg(
            _trade_msg("0xabc", PolymarketTradeStatus.MATCHED),
            trade_id=None,
            wait_for_ack=False,
            order_id="0xabc",
        )

        client.generate_order_accepted.assert_called_once()
        kwargs = client.generate_order_accepted.call_args.kwargs
        assert kwargs["client_order_id"] == order.client_order_id
        assert kwargs["venue_order_id"] == VenueOrderId("0xabc")
        client.generate_order_filled.assert_not_called()  # MATCHED 非 finalized,不生成 fill
    finally:
        loop.close()


def test_mined_trade_acks_once_matched_already_acked():
    """MATCHED 已 ack 过;MINED 到达时不重复 ack(同一张去重表)。"""
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))
        client._mark_accepted_emitted(order.client_order_id)  # MATCHED 已 ack 过

        client._handle_user_trade_in_ws_trade_msg(
            _trade_msg("0xabc", PolymarketTradeStatus.MINED),
            trade_id=None,
            wait_for_ack=False,
            order_id="0xabc",
        )

        client.generate_order_accepted.assert_not_called()
    finally:
        loop.close()


def test_confirmed_trade_acks_and_generates_fill_when_not_yet_acked():
    """taker 单 WS 若丢了 MATCHED/MINED、直接来 CONFIRMED:ack 依旧要在 fill 之前补上。"""
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_user_trade_in_ws_trade_msg(
            _trade_msg("0xabc", PolymarketTradeStatus.CONFIRMED),
            trade_id=TradeId("trade-1"),
            wait_for_ack=False,
            order_id="0xabc",
        )

        client.generate_order_accepted.assert_called_once()
        client.generate_order_filled.assert_called_once()
    finally:
        loop.close()


def test_placement_then_matched_does_not_double_ack():
    loop = asyncio.new_event_loop()
    try:
        client = _make_client(loop)
        instrument = _instrument()
        client._cache.add_instrument(instrument)
        order = _submitted_order(client, instrument)
        client._cache.add_venue_order_id(order.client_order_id, VenueOrderId("0xabc"))

        client._handle_ws_order_msg(_placement_msg("0xabc"), wait_for_ack=False)
        client._handle_user_trade_in_ws_trade_msg(
            _trade_msg("0xabc", PolymarketTradeStatus.MATCHED),
            trade_id=None,
            wait_for_ack=False,
            order_id="0xabc",
        )

        client.generate_order_accepted.assert_called_once()
    finally:
        loop.close()
