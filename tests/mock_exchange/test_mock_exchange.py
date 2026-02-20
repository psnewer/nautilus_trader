import asyncio

import pytest

from src.arbitrage.services.debug import MockCategory, debug_manager
from src.arbitrage.services.debug.config import DebugConfig
from src.arbitrage.services.execution import (
    ExecutionService,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Venue,
)
from src.arbitrage.services.execution.mock_exchange import MockExchange


@pytest.fixture(autouse=True)
def restore_debug_config():
    original = debug_manager.config.to_dict()
    original_enabled = debug_manager.enabled
    yield
    debug_manager._config = DebugConfig.from_dict(original)
    debug_manager._config.enabled = original_enabled


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_mock_exchange_timeline(event_loop):
    debug_manager.enable()
    debug_manager.set_override("use_mock_exchange", enabled=True, value=True)
    debug_manager.add_mock(
        item_id="mock_exec_flow",
        category=MockCategory.EXECUTION,
        name="mock_flow",
        data={
            "success": True,
            "timeline": [
                {"delay": 0.01, "status": "live"},
                {"delay": 0.01, "status": "partially_filled", "filled_size": 5},
                {"delay": 0.01, "status": "filled", "filled_size": 10},
            ],
        },
        enabled=True,
        conditions={"venue": "polymarket"},
    )

    updates: list[tuple[OrderStatus, float]] = []

    def on_update(order: Order) -> None:
        updates.append((order.status, order.filled_size))

    mock_exchange = MockExchange(order_update_callback=on_update)
    order = Order(
        venue=Venue.POLYMARKET,
        pair_id="pair-1",
        market_type="home",
        side=OrderSide.BUY,
        price=0.55,
        size=10.0,
        order_type=OrderType.GTC,
    )

    result = event_loop.run_until_complete(mock_exchange.place_order(order))
    assert result.success is True

    event_loop.run_until_complete(asyncio.sleep(0.05))

    assert order.status == OrderStatus.FILLED
    assert order.filled_size == 10.0
    assert OrderStatus.LIVE in [status for status, _ in updates]
    assert OrderStatus.PARTIALLY_FILLED in [status for status, _ in updates]


def test_execution_service_uses_mock_exchange(event_loop):
    debug_manager.enable()
    debug_manager.set_override("use_mock_exchange", enabled=True, value=True)
    debug_manager.add_mock(
        item_id="mock_exec_reject",
        category=MockCategory.EXECUTION,
        name="mock_reject",
        data={
            "success": False,
            "status": "rejected",
            "message": "[MOCK] Insufficient balance",
        },
        enabled=True,
        conditions={"test_scenario": "reject", "venue": "polymarket"},
    )

    service = ExecutionService()
    service._initialized = True
    order = Order(
        venue=Venue.POLYMARKET,
        pair_id="pair-2",
        market_type="home",
        side=OrderSide.BUY,
        price=0.42,
        size=5.0,
        order_type=OrderType.GTC,
        metadata={"test_scenario": "reject"},
    )

    result = event_loop.run_until_complete(service.execute_order(order))

    assert result.success is False
    assert order.status == OrderStatus.REJECTED


def test_mock_exchange_cancel(event_loop):
    debug_manager.enable()
    debug_manager.set_override("use_mock_exchange", enabled=True, value=True)
    debug_manager.add_mock(
        item_id="mock_exec_cancel",
        category=MockCategory.EXECUTION,
        name="mock_cancel",
        data={
            "success": True,
            "timeline": [
                {"delay": 1.0, "status": "live"},
                {"delay": 1.0, "status": "filled", "filled_size": 10},
            ],
        },
        enabled=True,
        conditions={"venue": "polymarket"},
    )

    updates: list[OrderStatus] = []

    def on_update(order: Order) -> None:
        updates.append(order.status)

    mock_exchange = MockExchange(order_update_callback=on_update)
    order = Order(
        venue=Venue.POLYMARKET,
        pair_id="pair-3",
        market_type="home",
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        order_type=OrderType.GTC,
    )

    event_loop.run_until_complete(mock_exchange.place_order(order))
    cancel_result = event_loop.run_until_complete(mock_exchange.cancel_order(order))

    assert cancel_result.success is True
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_size == 0.0
    assert OrderStatus.CANCELLED in updates
