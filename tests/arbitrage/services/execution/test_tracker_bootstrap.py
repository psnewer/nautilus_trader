import asyncio

from src.arbitrage.services.execution.planner import (
    OperationType,
    OperationVenue,
    OrderOperation,
)
from src.arbitrage.services.execution.tracker import OrderTracker, TrackingStatus


class _FakeOrbitExchClient:
    def __init__(self, bets):
        self._bets = bets

    def get_current_bets(self, market_id=None):
        if market_id is None:
            return list(self._bets)
        return [bet for bet in self._bets if str(bet.get("marketId", "")) == market_id]


def test_track_operations_bootstraps_from_orbitexch_cached_bets():
    tracker = OrderTracker(timeout=0.01)
    tracker.set_orbitexch_client(
        _FakeOrbitExchClient(
            bets=[
                {
                    "offerId": "216791854",
                    "marketId": "1.256411738",
                    "selectionId": "16149495",
                    "side": "BACK",
                    "sizeMatched": "8.10",
                    "sizeRemaining": "0.00",
                }
            ]
        )
    )

    operation = OrderOperation(
        operation_type=OperationType.PLACE,
        venue=OperationVenue.ORBITEXCH,
        market_type="away",
        size=8.1,
        price=1.01,
        market_id="1.256411738",
        selection_id="16149495",
    )
    operation_results = [
        {
            "success": True,
            "venue_order_id": "216791854",
        }
    ]

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(tracker.track_operations([operation], operation_results))
    finally:
        loop.close()

    assert result.all_confirmed is True
    assert result.results[0].status == TrackingStatus.CONFIRMED
    assert result.results[0].size_matched == 8.1
    assert result.results[0].size_remaining == 0.0
