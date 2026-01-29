# -*- coding: utf-8 -*-
"""
Tests for Market Discovery Service
"""

import pytest
from dataclasses import asdict
from datetime import datetime
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root / "src" / "arbitrage"))

from services.market_discovery.service import (
    MarketDiscoveredEvent,
    DiscoveryService,
)
from services.market_discovery.config import (
    MarketDiscoveryConfig,
    VenueConfig,
    PolymarketVenueConfig,
    OrbitExchVenueConfig,
    SportConfig,
)


class TestMarketDiscoveredEvent:
    """Tests for MarketDiscoveredEvent dataclass"""

    def test_create_event(self):
        """Test creating a MarketDiscoveredEvent instance"""
        event = MarketDiscoveredEvent(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="English Premier League",
            home_team="Manchester United",
            away_team="Liverpool",
            event_id="event_123",
            action="ADDED",
        )

        assert event.venue_id == "POLYMARKET"
        assert event.sport == "soccer"
        assert event.competition == "English Premier League"
        assert event.home_team == "Manchester United"
        assert event.away_team == "Liverpool"
        assert event.action == "ADDED"
        assert isinstance(event.timestamp, datetime)

    def test_event_with_metadata(self):
        """Test event with metadata"""
        event = MarketDiscoveredEvent(
            venue_id="ORBITEXCH",
            sport="basketball",
            competition="NBA",
            home_team="Lakers",
            away_team="Celtics",
            event_id="nba_123",
            action="UPDATED",
            metadata={"source": "scraper", "odds": 1.5},
        )

        assert event.metadata["source"] == "scraper"
        assert event.metadata["odds"] == 1.5


class TestMarketDiscoveryConfig:
    """Tests for MarketDiscoveryConfig"""

    def test_default_config(self):
        """Test default configuration"""
        config = MarketDiscoveryConfig()

        assert config.enabled is True
        assert config.poll_interval_sec == 60
        assert isinstance(config.venues, VenueConfig)

    def test_from_dict(self):
        """Test creating config from dict"""
        data = {
            "enabled": True,
            "poll_interval_sec": 120,
            "venues": {
                "polymarket": {
                    "enabled": True,
                    "sports": [
                        {"sport": "soccer", "competitions": ["EPL", "La Liga"]}
                    ],
                },
                "orbitexch": {
                    "enabled": False,
                    "sports": [],
                },
            },
        }

        config = MarketDiscoveryConfig.from_dict(data)

        assert config.enabled is True
        assert config.poll_interval_sec == 120
        assert config.venues.polymarket.enabled is True
        assert config.venues.orbitexch.enabled is False
        assert len(config.venues.polymarket.sports) == 1
        assert config.venues.polymarket.sports[0].sport == "soccer"
        assert "EPL" in config.venues.polymarket.sports[0].competitions


class TestDiscoveryService:
    """Tests for DiscoveryService"""

    def _create_test_config(self, polymarket_enabled=False, orbitexch_enabled=False):
        """Helper to create test configuration"""
        return MarketDiscoveryConfig(
            enabled=True,
            poll_interval_sec=10,
            venues=VenueConfig(
                polymarket=PolymarketVenueConfig(
                    enabled=polymarket_enabled,
                    sports=[SportConfig(sport="soccer", competitions=["EPL"])],
                ),
                orbitexch=OrbitExchVenueConfig(
                    enabled=orbitexch_enabled,
                    sports=[SportConfig(sport="soccer", competitions=["EPL"])],
                ),
            ),
        )

    def test_service_init(self):
        """Test DiscoveryService initialization"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        assert service.config == config
        assert len(service._event_handlers) == 0
        assert len(service._discovered_markets) == 0
        assert service._running is False

    def test_register_event_handler(self):
        """Test registering an event handler"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        handler = MagicMock()
        service.on_market_discovered(handler)

        assert len(service._event_handlers) == 1
        assert service._event_handlers[0] == handler

    def test_generate_market_key(self):
        """Test market key generation"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        key = service._generate_market_key(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="EPL",
            home_team="Man United",
            away_team="Liverpool",
        )

        assert key == "POLYMARKET:soccer:EPL:Man United:Liverpool"

    def test_process_discovered_match_new(self):
        """Test processing a newly discovered match"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        handler = MagicMock()
        service.on_market_discovered(handler)

        service._process_discovered_match(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="EPL",
            home_team="Arsenal",
            away_team="Chelsea",
            event_id="match_1",
        )

        # Check that market was added
        assert len(service._discovered_markets) == 1

        # Check that handler was called
        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert isinstance(event, MarketDiscoveredEvent)
        assert event.venue_id == "POLYMARKET"
        assert event.home_team == "Arsenal"
        assert event.action == "ADDED"

    def test_process_discovered_match_duplicate(self):
        """Test that duplicate matches are not re-added"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        handler = MagicMock()
        service.on_market_discovered(handler)

        # Add the same match twice
        for _ in range(2):
            service._process_discovered_match(
                venue_id="POLYMARKET",
                sport="soccer",
                competition="EPL",
                home_team="Arsenal",
                away_team="Chelsea",
                event_id="match_1",
            )

        # Should only be added once
        assert len(service._discovered_markets) == 1
        assert handler.call_count == 1

    def test_get_active_markets(self):
        """Test getting active markets grouped by venue"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        # Add markets from different venues
        service._process_discovered_match(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="EPL",
            home_team="Arsenal",
            away_team="Chelsea",
        )
        service._process_discovered_match(
            venue_id="ORBITEXCH",
            sport="soccer",
            competition="EPL",
            home_team="Man City",
            away_team="Tottenham",
        )

        markets = service.get_active_markets()

        assert "POLYMARKET" in markets
        assert "ORBITEXCH" in markets
        assert len(markets["POLYMARKET"]) == 1
        assert len(markets["ORBITEXCH"]) == 1

    def test_get_markets_by_venue(self):
        """Test filtering markets by venue"""
        config = self._create_test_config()
        service = DiscoveryService(config)

        # Add markets from different venues
        service._process_discovered_match(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="EPL",
            home_team="Arsenal",
            away_team="Chelsea",
        )
        service._process_discovered_match(
            venue_id="POLYMARKET",
            sport="soccer",
            competition="La Liga",
            home_team="Barcelona",
            away_team="Real Madrid",
        )
        service._process_discovered_match(
            venue_id="ORBITEXCH",
            sport="soccer",
            competition="EPL",
            home_team="Man City",
            away_team="Tottenham",
        )

        poly_markets = service.get_markets_by_venue("POLYMARKET")
        orbit_markets = service.get_markets_by_venue("ORBITEXCH")

        assert len(poly_markets) == 2
        assert len(orbit_markets) == 1

    def test_disabled_service_does_not_start(self):
        """Test that disabled service doesn't start"""
        config = MarketDiscoveryConfig(enabled=False)
        service = DiscoveryService(config)

        import asyncio

        async def run_test():
            await service.start()
            return service._running

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result is False


@pytest.mark.asyncio(loop_scope="function")
class TestDiscoveryServiceAsync:
    """Async tests for DiscoveryService"""

    @pytest.fixture
    def config(self):
        """Test configuration fixture"""
        return MarketDiscoveryConfig(
            enabled=True,
            poll_interval_sec=10,
            venues=VenueConfig(
                polymarket=PolymarketVenueConfig(enabled=False),
                orbitexch=OrbitExchVenueConfig(enabled=False),
            ),
        )

    async def test_start_stop_service(self, config):
        """Test starting and stopping the service"""
        service = DiscoveryService(config)

        await service.start()
        assert service._running is True

        await service.stop()
        assert service._running is False

    async def test_discover_all_empty(self, config):
        """Test discover_all with no scrapers enabled"""
        service = DiscoveryService(config)
        await service.start()

        events = await service.discover_all()

        # No scrapers enabled, so no events
        assert events == []

        await service.stop()

    async def test_update_config(self, config):
        """Test updating service configuration"""
        service = DiscoveryService(config)

        new_config = MarketDiscoveryConfig(
            enabled=True,
            poll_interval_sec=30,
            venues=VenueConfig(
                polymarket=PolymarketVenueConfig(enabled=False),
                orbitexch=OrbitExchVenueConfig(enabled=False),
            ),
        )

        await service.update_config(new_config)

        assert service.config.poll_interval_sec == 30
