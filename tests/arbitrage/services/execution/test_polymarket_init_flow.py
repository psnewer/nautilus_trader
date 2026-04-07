import asyncio
import logging

from src.arbitrage.services.execution.config import ExecutionConfig
from src.arbitrage.services.execution.service import ExecutionService
from src.arbitrage.services.odds_subscription.config import OddsSubscriptionConfig
from src.arbitrage.services.odds_subscription.service import OddsSubscriptionService


def test_odds_service_ensure_polymarket_client_ready_initializes_once(monkeypatch):
    service = OddsSubscriptionService(
        config=OddsSubscriptionConfig(polymarket_private_key="test-private-key")
    )
    calls = {"count": 0}

    def fake_initialize(self):
        calls["count"] += 1
        self._clob_client = object()
        return True

    monkeypatch.setattr(
        "src.arbitrage.services.odds_subscription.polymarket_client.PolymarketOddsClient.initialize_clob_client",
        fake_initialize,
    )

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(service.ensure_polymarket_client_ready()) is True
        assert loop.run_until_complete(service.ensure_polymarket_client_ready()) is True
    finally:
        loop.close()

    assert calls["count"] == 1
    assert service.get_polymarket_client() is not None


class _ReadyPolymarketClient:
    def __init__(self):
        self._clob_client = object()
        self._api_lock = asyncio.Lock()


class _ReadyOddsService:
    def __init__(self):
        self._polymarket_client = _ReadyPolymarketClient()

    def get_polymarket_client(self):
        return self._polymarket_client

    def get_orbitexch_client(self):
        return object()

    def register_polymarket_order_callback(self, callback):
        return None

    def register_orbitexch_bets_callback(self, callback):
        return None


def test_execution_service_initializes_with_ready_polymarket_client(caplog):
    service = ExecutionService(config=ExecutionConfig())
    service.set_odds_service(_ReadyOddsService())

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.INFO):
            result = loop.run_until_complete(service.initialize())
    finally:
        loop.close()

    assert result is True
    assert "Polymarket executor initialized" in caplog.text
    assert "Polymarket executor initialization failed" not in caplog.text
