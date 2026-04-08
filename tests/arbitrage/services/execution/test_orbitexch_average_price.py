import asyncio

from src.arbitrage.services.execution.config import ExecutionConfig
from src.arbitrage.services.execution.orchestrator import ExecutionOrchestrator
from src.arbitrage.services.execution.session import ExecutionSession


class _FakeOrbitExchClient:
    def __init__(self, bets):
        self._bets = bets

    def get_current_bets(self, market_id=None):
        if market_id is None:
            return list(self._bets)
        return [bet for bet in self._bets if str(bet.get("marketId", "")) == market_id]


def _build_orchestrator() -> ExecutionOrchestrator:
    async def _noop_execute(order):
        raise NotImplementedError

    async def _noop_cancel(order_id, venue=None):
        raise NotImplementedError

    def _order_info(pair_id, market_type):
        return {
            "orbitexch": {
                "market_id": "1.256411738",
                "selection_id": "16149495",
            }
        }

    return ExecutionOrchestrator(
        config=ExecutionConfig(),
        order_executor=_noop_execute,
        order_canceller=_noop_cancel,
        order_info_getter=_order_info,
        probabilities_getter=lambda pair_id: {"home": 0.5, "draw": 0.0, "away": 0.5},
        fx_getter=lambda: 1.33,
    )


def test_orbitexch_current_share_uses_average_price_only():
    orchestrator = _build_orchestrator()
    orchestrator._tracker.set_orbitexch_client(
        _FakeOrbitExchClient(
            bets=[
                {
                    "offerId": "216791854",
                    "marketId": "1.256411738",
                    "selectionId": "16149495",
                    "side": "BACK",
                    "sizeMatched": "8.10",
                    "sizeRemaining": "0.00",
                    "price": "1.01",
                    "averagePrice": "5.46",
                }
            ]
        )
    )

    session = ExecutionSession.create(
        pair_id="pair-1",
        opportunity_id="opp-1",
        target_shares={"home": 0.0, "draw": 0.0, "away": 0.0},
        probabilities={"home": 0.0, "draw": 0.0, "away": 1.0},
    )
    session.outcome_venues["away"] = "orbitexch"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            orchestrator._update_session_filled(
                session=session,
                position_snapshot={"home": 0.0, "draw": 0.0, "away": 0.0},
            )
        )
    finally:
        loop.close()

    assert session.filled.away == 8.1 * 5.46 * 1.33


def test_orbitexch_current_share_does_not_fallback_to_price_when_average_price_missing():
    orchestrator = _build_orchestrator()
    orchestrator._tracker.set_orbitexch_client(
        _FakeOrbitExchClient(
            bets=[
                {
                    "offerId": "216791854",
                    "marketId": "1.256411738",
                    "selectionId": "16149495",
                    "side": "BACK",
                    "sizeMatched": "8.10",
                    "sizeRemaining": "0.00",
                    "price": "1.01",
                }
            ]
        )
    )

    session = ExecutionSession.create(
        pair_id="pair-1",
        opportunity_id="opp-1",
        target_shares={"home": 0.0, "draw": 0.0, "away": 0.0},
        probabilities={"home": 0.0, "draw": 0.0, "away": 1.0},
    )
    session.outcome_venues["away"] = "orbitexch"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            orchestrator._update_session_filled(
                session=session,
                position_snapshot={"home": 0.0, "draw": 0.0, "away": 0.0},
            )
        )
    finally:
        loop.close()

    assert session.filled.away == 0.0
