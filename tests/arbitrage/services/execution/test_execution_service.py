import asyncio

import pytest

from src.arbitrage.services.debug import MockCategory, debug_manager
from src.arbitrage.services.execution.config import ExecutionConfig
from src.arbitrage.services.execution.models import (
    CancelResult,
    ExecutionResult,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Venue,
)
from src.arbitrage.services.execution.orchestrator import ExecutionOrchestrator
from src.arbitrage.services.execution.planner import (
    ExecutionPlan,
    OperationType,
    OrderOperation,
    OperationVenue,
)
from src.arbitrage.services.execution.recovery import RecoveryCalculator
from src.arbitrage.services.execution.service import ExecutionService
from src.arbitrage.services.execution.session import ExecutionSession, OutcomeProbabilities
from src.arbitrage.services.execution.tracker import (
    BatchTrackingResult,
    OrderTracker,
    TrackingResult,
    TrackingStatus,
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def debug_reset():
    debug_manager.disable()
    for name in (
        "use_mock_exchange",
        "skip_execution",
        "execution_delay",
        "polymarket_price",
        "orbitexch_price",
        "order_size",
        "polymarket_size",
        "orbitexch_size",
    ):
        debug_manager.set_override(name, enabled=False)
    debug_manager.config.mock_data = {}
    yield
    debug_manager.disable()
    for name in (
        "use_mock_exchange",
        "skip_execution",
        "execution_delay",
        "polymarket_price",
        "orbitexch_price",
        "order_size",
        "polymarket_size",
        "orbitexch_size",
    ):
        debug_manager.set_override(name, enabled=False)
    debug_manager.config.mock_data = {}


@pytest.fixture
def debug_mock_exchange(debug_reset):
    debug_manager.enable()
    debug_manager.set_override("use_mock_exchange", enabled=True, value=True)
    return debug_manager


def _make_order(
    venue: Venue,
    market_type: str = "home",
    price: float = 0.5,
    size: float = 10.0,
    scenario: str | None = None,
) -> Order:
    order = Order(
        venue=venue,
        pair_id="pair-1",
        market_type=market_type,
        side=OrderSide.BUY if venue == Venue.POLYMARKET else OrderSide.BACK,
        price=price,
        size=size,
        order_type=OrderType.GTC,
    )
    if scenario:
        order.metadata["test_scenario"] = scenario
    return order


def _order_info_getter(pair_id: str, market_type: str) -> dict:
    return {
        "polymarket": {
            "token_id": f"token-{market_type}",
            "condition_id": f"condition-{market_type}",
        },
        "orbitexch": {
            "market_id": f"market-{market_type}",
            "selection_id": f"selection-{market_type}",
        },
    }


def _probabilities_getter(pair_id: str) -> dict[str, float]:
    return {"home": 0.4, "draw": 0.2, "away": 0.4}


class FakeTracker:
    def __init__(self, batches: list[BatchTrackingResult]):
        self._batches = list(batches)
        self.take_snapshot_calls: list[list] = []
        self.track_calls: list[list] = []
        self._polymarket_client = None
        self._orbitexch_client = None

    def take_snapshot(self, operations):
        self.take_snapshot_calls.append(list(operations))

    async def track_operations(self, operations, operation_results):
        self.track_calls.append(list(operations))
        if self._batches:
            return self._batches.pop(0)
        results = [
            TrackingResult(
                operation=op,
                status=TrackingStatus.CONFIRMED,
                venue_order_id="v-1",
                size_matched=op.size,
                size_remaining=0.0,
            )
            for op in operations
        ]
        return BatchTrackingResult(results=results, all_confirmed=True)


async def _ok_execute(order: Order) -> ExecutionResult:
    return ExecutionResult(success=True, order=order, message="ok")


async def _ok_cancel(order_id: str) -> CancelResult:
    return CancelResult(success=True, order_id=order_id, message="ok")


def _build_orchestrator(tracker: FakeTracker) -> ExecutionOrchestrator:
    orchestrator = ExecutionOrchestrator(
        config=ExecutionConfig(),
        order_executor=_ok_execute,
        order_canceller=_ok_cancel,
        order_info_getter=_order_info_getter,
        probabilities_getter=_probabilities_getter,
    )
    orchestrator._tracker = tracker
    return orchestrator


class _FakePolyOrder:
    def __init__(self, order_id, asset_id, original_size, size_matched, timestamp):
        self.order_id = order_id
        self.asset_id = asset_id
        self.original_size = original_size
        self.size_matched = size_matched
        self.timestamp = timestamp


class _FakePolymarketClient:
    def __init__(self, before_orders, after_orders):
        self._before = before_orders
        self._after = after_orders
        self._use_after = False

    async def fetch_open_orders(self):
        self._use_after = True

    def get_current_orders(self, condition_id):
        if self._use_after:
            return self._after.get(condition_id, [])
        return self._before.get(condition_id, [])


class _FakeOrbitExchClient:
    def __init__(self, before_bets, after_bets):
        self._before = before_bets
        self._after = after_bets
        self._use_after = False

    async def refresh_page(self):
        self._use_after = True

    def get_current_bets(self, market_id):
        if self._use_after:
            return self._after.get(market_id, [])
        return self._before.get(market_id, [])


def test_recovery_calculator_mean_rebate():
    calculator = RecoveryCalculator()
    probabilities = {"home": 0.5, "draw": 0.3, "away": 0.2}
    filled = {"home": 10.0, "draw": 0.0, "away": 2.0}
    result = calculator.calculate_from_dicts(probabilities, filled)

    validation = calculator.validate_mean_rebate(
        probabilities=OutcomeProbabilities.from_dict(probabilities),
        shares=result.target_shares,
    )
    assert validation["is_mean_rebate"] is True


def test_recovery_calculator_minimal_additional_shares():
    calculator = RecoveryCalculator()
    probabilities = {"home": 0.5, "draw": 0.3, "away": 0.2}
    filled = {"home": 10.0, "draw": 0.0, "away": 2.0}
    result = calculator.calculate_from_dicts(probabilities, filled)

    ratios = {
        "home": filled["home"] / probabilities["home"],
        "draw": filled["draw"] / probabilities["draw"],
        "away": filled["away"] / probabilities["away"],
    }
    base_value = max(ratios.values())
    expected_target = {
        outcome: base_value * probabilities[outcome]
        for outcome in probabilities
    }
    expected_additional = {
        outcome: max(0.0, expected_target[outcome] - filled[outcome])
        for outcome in probabilities
    }

    assert result.target_shares.to_dict() == expected_target
    assert result.additional_shares.to_dict() == expected_additional


def test_tracker_timeout_refresh_confirms_place(event_loop):
    tracker = OrderTracker(timeout=0.01)
    condition_id = "condition-home"
    token_id = "token-home"
    before_orders = {condition_id: []}
    after_orders = {
        condition_id: [
            _FakePolyOrder(
                order_id="order-1",
                asset_id=token_id,
                original_size=10.0,
                size_matched=10.0,
                timestamp=1,
            )
        ]
    }
    tracker.set_polymarket_client(_FakePolymarketClient(before_orders, after_orders))
    op = OrderOperation(
        operation_type=OperationType.PLACE,
        venue=OperationVenue.POLYMARKET,
        market_type="home",
        size=10.0,
        price=0.5,
        token_id=token_id,
        condition_id=condition_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.CONFIRMED
    assert result.results[0].size_remaining == 0.0


def test_tracker_timeout_refresh_marks_cancel_failed(event_loop):
    tracker = OrderTracker(timeout=0.01)
    condition_id = "condition-away"
    token_id = "token-away"
    existing_order = _FakePolyOrder(
        order_id="order-2",
        asset_id=token_id,
        original_size=8.0,
        size_matched=0.0,
        timestamp=1,
    )
    before_orders = {condition_id: [existing_order]}
    after_orders = {condition_id: [existing_order]}
    tracker.set_polymarket_client(_FakePolymarketClient(before_orders, after_orders))
    op = OrderOperation(
        operation_type=OperationType.CANCEL,
        venue=OperationVenue.POLYMARKET,
        market_type="away",
        order_id="order-2",
        token_id=token_id,
        condition_id=condition_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.FAILED


def test_tracker_timeout_refresh_orbitexch_place_success(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-1"
    selection_id = "111"
    before_bets = {market_id: []}
    after_bets = {
        market_id: [
            {
                "offerId": "offer-1",
                "selectionId": selection_id,
                "side": "BACK",
                "sizeMatched": 5.0,
                "sizeRemaining": 0.0,
            }
        ]
    }
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.PLACE,
        venue=OperationVenue.ORBITEXCH,
        market_type="home",
        size=5.0,
        price=2.0,
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.CONFIRMED
    assert result.results[0].venue_order_id == "offer-1"


def test_tracker_timeout_refresh_orbitexch_place_failed(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-2"
    selection_id = "222"
    before_bets = {market_id: []}
    after_bets = {market_id: []}
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.PLACE,
        venue=OperationVenue.ORBITEXCH,
        market_type="away",
        size=3.0,
        price=2.5,
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.FAILED


def test_tracker_timeout_refresh_orbitexch_cancel_success(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-3"
    selection_id = "333"
    existing_bet = {
        "offerId": "offer-3",
        "selectionId": selection_id,
        "side": "BACK",
        "sizeMatched": 0.0,
        "sizeRemaining": 4.0,
    }
    before_bets = {market_id: [existing_bet]}
    after_bets = {market_id: []}
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.CANCEL,
        venue=OperationVenue.ORBITEXCH,
        market_type="draw",
        order_id="offer-3",
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.CONFIRMED


def test_tracker_timeout_refresh_orbitexch_cancel_failed(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-4"
    selection_id = "444"
    existing_bet = {
        "offerId": "offer-4",
        "selectionId": selection_id,
        "side": "BACK",
        "sizeMatched": 0.0,
        "sizeRemaining": 4.0,
    }
    before_bets = {market_id: [existing_bet]}
    after_bets = {market_id: [existing_bet]}
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.CANCEL,
        venue=OperationVenue.ORBITEXCH,
        market_type="draw",
        order_id="offer-4",
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.FAILED


def test_tracker_timeout_refresh_orbitexch_modify_success(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-5"
    selection_id = "555"
    before_bets = {
        market_id: [
            {
                "offerId": "offer-5",
                "selectionId": selection_id,
                "side": "BACK",
                "sizeMatched": 1.0,
                "sizeRemaining": 3.0,
            }
        ]
    }
    after_bets = {
        market_id: [
            {
                "offerId": "offer-5",
                "selectionId": selection_id,
                "side": "BACK",
                "sizeMatched": 2.0,
                "sizeRemaining": 2.0,
            }
        ]
    }
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.MODIFY,
        venue=OperationVenue.ORBITEXCH,
        market_type="home",
        size=2.0,
        price=2.0,
        order_id="offer-5",
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.CONFIRMED


def test_tracker_timeout_refresh_orbitexch_modify_failed(event_loop):
    tracker = OrderTracker(timeout=0.01)
    market_id = "market-6"
    selection_id = "666"
    before_bets = {
        market_id: [
            {
                "offerId": "offer-6",
                "selectionId": selection_id,
                "side": "BACK",
                "sizeMatched": 1.0,
                "sizeRemaining": 3.0,
            }
        ]
    }
    after_bets = {
        market_id: [
            {
                "offerId": "offer-6",
                "selectionId": selection_id,
                "side": "BACK",
                "sizeMatched": 1.0,
                "sizeRemaining": 3.0,
            }
        ]
    }
    tracker.set_orbitexch_client(_FakeOrbitExchClient(before_bets, after_bets))
    op = OrderOperation(
        operation_type=OperationType.MODIFY,
        venue=OperationVenue.ORBITEXCH,
        market_type="home",
        size=2.0,
        price=2.0,
        order_id="offer-6",
        market_id=market_id,
        selection_id=selection_id,
    )
    tracker.take_snapshot([op])
    result = event_loop.run_until_complete(tracker.track_operations([op], [{}]))

    assert result.results[0].status == TrackingStatus.FAILED


def test_execute_order_fails_when_not_initialized(debug_reset, event_loop):
    service = ExecutionService(config=ExecutionConfig())
    order = _make_order(Venue.POLYMARKET)
    result = event_loop.run_until_complete(service.execute_order(order))
    assert result.success is False
    assert result.message == "Service not initialized"


def test_execute_order_skip_execution_override(debug_reset, event_loop):
    debug_manager.enable()
    debug_manager.set_override("skip_execution", enabled=True, value=True)
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True

    order = _make_order(Venue.POLYMARKET)
    result = event_loop.run_until_complete(service.execute_order(order))

    assert result.success is True
    assert order.status == OrderStatus.SUBMITTED
    assert order.metadata.get("debug_skipped") is True


def test_execute_order_mock_exchange_filled(debug_mock_exchange, event_loop):
    debug_manager.add_mock(
        item_id="exec_full_fill",
        category=MockCategory.EXECUTION,
        name="full fill",
        data={
            "success": True,
            "status": "submitted",
            "venue_order_id": "venue-1",
            "timeline": [
                {"delay": 0, "status": "filled", "filled_size": 10},
            ],
        },
        enabled=True,
        conditions={"test_scenario": "full_fill"},
    )
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True

    order = _make_order(Venue.POLYMARKET, size=10.0, scenario="full_fill")
    result = event_loop.run_until_complete(service.execute_order(order))
    event_loop.run_until_complete(asyncio.sleep(0.01))

    assert result.success is True
    assert order.status == OrderStatus.FILLED
    assert service.get_active_orders() == []


def test_execute_order_mock_exchange_rejected(debug_mock_exchange, event_loop):
    debug_manager.add_mock(
        item_id="exec_reject",
        category=MockCategory.EXECUTION,
        name="reject",
        data={
            "success": False,
            "status": "rejected",
            "message": "rejected",
        },
        enabled=True,
        conditions={"test_scenario": "reject"},
    )
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True

    order = _make_order(Venue.ORBITEXCH, scenario="reject")
    result = event_loop.run_until_complete(service.execute_order(order))

    assert result.success is False
    assert order.status == OrderStatus.REJECTED


def test_cancel_order_not_found(debug_reset, event_loop):
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True
    result = event_loop.run_until_complete(service.cancel_order("missing-order"))
    assert result.success is False
    assert result.message == "Order not found"


def test_take_remaining_missing_order(debug_reset, event_loop):
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True
    result = event_loop.run_until_complete(service.take_remaining_at_market("missing-order"))
    assert result.success is False
    assert result.message == "Order not found"


def test_cancel_all_orders_only_active(debug_mock_exchange, event_loop):
    debug_manager.add_mock(
        item_id="exec_keep_active",
        category=MockCategory.EXECUTION,
        name="keep active",
        data={
            "success": True,
            "status": "submitted",
            "venue_order_id": "venue-keep",
            "live_delay": 0,
            "partial_fill_ratio": 0,
            "final_fill_delay": 1000,
        },
        enabled=True,
        conditions={"test_scenario": "keep_active"},
    )
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True

    order = _make_order(Venue.POLYMARKET, scenario="keep_active")
    event_loop.run_until_complete(service.execute_order(order))
    results = event_loop.run_until_complete(service.cancel_all_orders())

    assert len(results) == 1
    assert results[0].success is True
    assert service.get_active_orders() == []


def test_cancel_all_orbitexch_unmatched(debug_mock_exchange, event_loop):
    debug_manager.add_mock(
        item_id="exec_keep_active_orbit",
        category=MockCategory.EXECUTION,
        name="keep active orbit",
        data={
            "success": True,
            "status": "submitted",
            "venue_order_id": "venue-orbit",
            "live_delay": 0,
            "partial_fill_ratio": 0,
            "final_fill_delay": 1000,
        },
        enabled=True,
        conditions={"test_scenario": "keep_active_orbit"},
    )
    service = ExecutionService(config=ExecutionConfig())
    service._initialized = True

    orbit_order = _make_order(Venue.ORBITEXCH, scenario="keep_active_orbit")
    poly_order = _make_order(Venue.POLYMARKET, scenario="keep_active_orbit")

    event_loop.run_until_complete(service.execute_order(orbit_order))
    event_loop.run_until_complete(service.execute_order(poly_order))

    result = event_loop.run_until_complete(service.cancel_all_orbitexch_unmatched())
    remaining = service.get_active_orders()

    assert result.success is True
    assert any(o.venue == Venue.POLYMARKET for o in remaining)
    assert all(o.venue != Venue.ORBITEXCH for o in remaining)


def test_orchestrator_initial_plan_completed_by_operations(event_loop):
    legs = [
        {"venue": "orbitexch", "market_type": "home", "probability": 50, "raw_odds": 10},
    ]
    target_shares = {"home": 10, "draw": 0, "away": 0}
    probabilities = {"home": 0.5, "draw": 0.25, "away": 0.25}

    tracker = FakeTracker(
        batches=[
            BatchTrackingResult(
                results=[],
                all_confirmed=True,
            ),
        ]
    )
    orchestrator = _build_orchestrator(tracker)

    async def track_filled(operations, operation_results):
        results = [
            TrackingResult(
                operation=operations[0],
                status=TrackingStatus.CONFIRMED,
                venue_order_id="v-1",
                size_matched=operations[0].size,
                size_remaining=0.0,
            )
        ]
        return BatchTrackingResult(results=results, all_confirmed=True)

    orchestrator._tracker.track_operations = track_filled

    session = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-1",
        opportunity_id="opp-1",
        legs=legs,
        target_shares=target_shares,
        probabilities=probabilities,
    ))

    assert session.end_reason.value == "target_met"
    assert session.filled.home != session.initial_target.home


def test_orchestrator_initial_zero_fill(event_loop):
    legs = [
        {"venue": "polymarket", "market_type": "away", "probability": 60, "raw_odds": 1.5},
    ]
    target_shares = {"home": 0, "draw": 0, "away": 10}
    probabilities = {"home": 0.0, "draw": 0.0, "away": 0.6}

    async def track_zero(operations, operation_results):
        results = [
            TrackingResult(
                operation=operations[0],
                status=TrackingStatus.CONFIRMED,
                venue_order_id="v-1",
                size_matched=0.0,
                size_remaining=0.0,
            )
        ]
        return BatchTrackingResult(results=results, all_confirmed=True)

    tracker = FakeTracker(batches=[])
    orchestrator = _build_orchestrator(tracker)
    orchestrator._tracker.track_operations = track_zero

    session = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-2",
        opportunity_id="opp-2",
        legs=legs,
        target_shares=target_shares,
        probabilities=probabilities,
    ))

    assert session.end_reason.value == "zero_fill"


def test_orchestrator_recovery_cancel_then_replan(event_loop):
    legs = [
        {"venue": "polymarket", "market_type": "home", "probability": 50, "raw_odds": 2.0},
    ]
    target_shares = {"home": 10, "draw": 0, "away": 0}
    probabilities = {"home": 0.5, "draw": 0.25, "away": 0.25}

    orchestrator = _build_orchestrator(FakeTracker(batches=[]))

    async def track_sequence(operations, operation_results):
        if operations[0].operation_type == OperationType.CANCEL:
            results = [
                TrackingResult(
                    operation=op,
                    status=TrackingStatus.CONFIRMED,
                    venue_order_id="v-1",
                    size_matched=0.0,
                    size_remaining=0.0,
                )
                for op in operations
            ]
            return BatchTrackingResult(results=results, all_confirmed=True)
        if len(operations) == 1 and operations[0].market_type == "home":
            result = TrackingResult(
                operation=operations[0],
                status=TrackingStatus.CONFIRMED,
                venue_order_id="v-1",
                size_matched=5.0,
                size_remaining=5.0,
            )
            return BatchTrackingResult(results=[result], all_confirmed=True, has_partial=True)
        results = [
            TrackingResult(
                operation=op,
                status=TrackingStatus.CONFIRMED,
                venue_order_id="v-2",
                size_matched=op.size,
                size_remaining=0.0,
            )
            for op in operations
        ]
        return BatchTrackingResult(results=results, all_confirmed=True)

    orchestrator._tracker.track_operations = track_sequence

    session = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-3",
        opportunity_id="opp-3",
        legs=legs,
        target_shares=target_shares,
        probabilities=probabilities,
    ))

    assert session.end_reason.value == "target_met"


def test_orchestrator_replan_on_new_fill_before_operate(event_loop):
    legs = [
        {"venue": "polymarket", "market_type": "home", "probability": 50, "raw_odds": 2.0},
    ]
    target_shares = {"home": 10, "draw": 0, "away": 0}
    probabilities = {"home": 0.5, "draw": 0.25, "away": 0.25}

    tracker = FakeTracker(batches=[])
    orchestrator = _build_orchestrator(tracker)

    call_state = {"track_calls": 0, "recovery_plans": 0}

    async def track_partial(operations, operation_results):
        call_state["track_calls"] += 1
        op = operations[0]
        # Initial plan has large size (>=10) → partial fill 5.0
        # Recovery plan has smaller size (6.0) → full fill
        size_matched = 5.0 if op.size >= 10 else op.size
        size_remaining = 0.0
        result = TrackingResult(
            operation=op,
            status=TrackingStatus.CONFIRMED,
            venue_order_id="v-1",
            size_matched=size_matched,
            size_remaining=size_remaining,
        )
        return BatchTrackingResult(results=[result], all_confirmed=True)

    orchestrator._tracker.track_operations = track_partial

    def counted_plan_recovery(*args, **kwargs):
        call_state["recovery_plans"] += 1
        operation = OrderOperation(
            operation_type=OperationType.PLACE,
            venue=OperationVenue.POLYMARKET,
            market_type="draw",
            size=6.0,
            price=0.25,
            token_id="token-draw",
            condition_id="condition-draw",
        )
        return ExecutionPlan(
            operations=[operation],
            has_places=True,
            description="test recovery plan",
        )

    orchestrator._planner.plan_recovery = counted_plan_recovery

    refresh_state = {"calls": 0}

    async def refresh_once(all_results, session_start_ms):
        refresh_state["calls"] += 1
        if refresh_state["calls"] == 2:
            for result in all_results:
                result.size_matched = min(result.operation.size - 1, result.size_matched + 1)
                result.size_remaining = 0.0

    orchestrator._refresh_tracking_results = refresh_once

    session = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-4",
        opportunity_id="opp-4",
        legs=legs,
        target_shares=target_shares,
        probabilities=probabilities,
    ))

    assert call_state["recovery_plans"] >= 2
    assert call_state["track_calls"] == 2


def test_orchestrator_rejects_active_session(event_loop):
    tracker = FakeTracker(batches=[])
    orchestrator = _build_orchestrator(tracker)

    session = orchestrator.get_active_session_for_pair("pair-5")
    assert session is None

    active = ExecutionSession.create(
        pair_id="pair-5",
        opportunity_id="opp-5",
        target_shares={"home": 1, "draw": 0, "away": 0},
        probabilities={"home": 0.5, "draw": 0.0, "away": 0.0},
    )
    orchestrator._active_pair_sessions["pair-5"] = active.session_id
    orchestrator._sessions[active.session_id] = active

    result = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-5",
        opportunity_id="opp-5",
        legs=[],
        target_shares={"home": 0, "draw": 0, "away": 0},
        probabilities={"home": 0, "draw": 0, "away": 0},
    ))

    assert result is None


def test_orchestrator_max_retries_exceeded(event_loop):
    legs = [
        {"venue": "polymarket", "market_type": "home", "probability": 50, "raw_odds": 2.0},
    ]
    target_shares = {"home": 10, "draw": 0, "away": 0}
    probabilities = {"home": 0.5, "draw": 0.25, "away": 0.25}

    tracker = FakeTracker(batches=[])
    orchestrator = _build_orchestrator(tracker)
    orchestrator._config.max_failure_retries = 1

    async def track_partial(operations, operation_results):
        op = operations[0]
        result = TrackingResult(
            operation=op,
            status=TrackingStatus.FAILED,
            venue_order_id="v-1",
            size_matched=5.0 if op.size > 1 else op.size,
            size_remaining=0.0 if op.size > 1 else max(0.0, op.size - op.size),
        )
        return BatchTrackingResult(results=[result], all_confirmed=True)

    orchestrator._tracker.track_operations = track_partial

    def force_recovery_plan(*args, **kwargs):
        operation = OrderOperation(
            operation_type=OperationType.PLACE,
            venue=OperationVenue.POLYMARKET,
            market_type="draw",
            size=1.0,
            price=0.25,
            token_id="token-draw",
            condition_id="condition-draw",
        )
        return ExecutionPlan(
            operations=[operation],
            has_places=True,
            description="force recovery",
        )

    orchestrator._planner.plan_recovery = force_recovery_plan

    session = event_loop.run_until_complete(orchestrator.execute_opportunity(
        pair_id="pair-6",
        opportunity_id="opp-6",
        legs=legs,
        target_shares=target_shares,
        probabilities=probabilities,
    ))

    assert session.end_reason.value == "max_failure_retries"
