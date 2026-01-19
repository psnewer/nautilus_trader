# -*- coding: utf-8 -*-
"""
Tests for Market Discovery Service
"""

import pytest
from dataclasses import asdict
from datetime import datetime
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from services.market_discovery.service import StandardMarket, PlatformAdapter, MarketDiscoveryService


class TestStandardMarket:
    """Tests for StandardMarket dataclass"""

    def test_create_standard_market(self):
        """Test creating a StandardMarket instance"""
        market = StandardMarket(
            platform="POLYMARKET",
            market_id="market_123",
            event_id="event_456",
            event_name="Team A vs Team B",
            market_type="MATCH_ODDS",
            sport="Soccer",
            start_time="2024-01-15T15:00:00Z",
            discovered_at=datetime.now().isoformat(),
            status="OPEN",
            in_play=False,
            outcomes=[
                {"id": "1", "name": "Team A", "odds": 1.5},
                {"id": "2", "name": "Team B", "odds": 2.5},
            ],
            metadata={"source": "api"}
        )

        assert market.platform == "POLYMARKET"
        assert market.market_id == "market_123"
        assert market.sport == "Soccer"
        assert len(market.outcomes) == 2

    def test_standard_market_to_dict(self):
        """Test converting StandardMarket to dict"""
        market = StandardMarket(
            platform="ORBITEXCH",
            market_id="m1",
            event_id="e1",
            event_name="Test Event",
            market_type="MATCH_ODDS",
            sport="Basketball",
            start_time="2024-01-15T15:00:00Z",
            discovered_at="2024-01-15T10:00:00Z",
            status="OPEN",
            in_play=False,
            outcomes=[],
            metadata={}
        )

        data = market.to_dict()

        assert isinstance(data, dict)
        assert data["platform"] == "ORBITEXCH"
        assert data["sport"] == "Basketball"

    def test_standard_market_to_json(self):
        """Test converting StandardMarket to JSON"""
        market = StandardMarket(
            platform="POLYMARKET",
            market_id="m1",
            event_id="e1",
            event_name="Test",
            market_type="MATCH_ODDS",
            sport="Soccer",
            start_time="2024-01-15T15:00:00Z",
            discovered_at="2024-01-15T10:00:00Z",
            status="OPEN",
            in_play=False,
            outcomes=[],
            metadata={}
        )

        json_str = market.to_json()

        assert isinstance(json_str, str)
        assert "POLYMARKET" in json_str


class TestPlatformAdapter:
    """Tests for PlatformAdapter base class"""

    def test_platform_adapter_init(self):
        """Test PlatformAdapter initialization"""
        adapter = PlatformAdapter("TEST_PLATFORM")

        assert adapter.platform_name == "TEST_PLATFORM"

    def test_discover_markets_not_implemented(self):
        """Test that discover_markets raises NotImplementedError"""
        adapter = PlatformAdapter("TEST")

        with pytest.raises(NotImplementedError):
            import asyncio
            asyncio.run(adapter.discover_markets())


class TestMarketDiscoveryService:
    """Tests for MarketDiscoveryService"""

    def test_service_init(self, tmp_path):
        """Test MarketDiscoveryService initialization"""
        output_dir = tmp_path / "markets"
        service = MarketDiscoveryService(output_dir=str(output_dir))

        assert service.output_dir.exists()
        assert len(service.adapters) == 0

    def test_register_adapter(self, tmp_path):
        """Test registering a platform adapter"""
        output_dir = tmp_path / "markets"
        service = MarketDiscoveryService(output_dir=str(output_dir))

        adapter = PlatformAdapter("TEST_PLATFORM")
        service.register_adapter(adapter)

        assert "TEST_PLATFORM" in service.adapters
        assert service.adapters["TEST_PLATFORM"] == adapter

    def test_register_multiple_adapters(self, tmp_path):
        """Test registering multiple adapters"""
        output_dir = tmp_path / "markets"
        service = MarketDiscoveryService(output_dir=str(output_dir))

        adapter1 = PlatformAdapter("PLATFORM_A")
        adapter2 = PlatformAdapter("PLATFORM_B")

        service.register_adapter(adapter1)
        service.register_adapter(adapter2)

        assert len(service.adapters) == 2
        assert "PLATFORM_A" in service.adapters
        assert "PLATFORM_B" in service.adapters


class MockAdapter(PlatformAdapter):
    """Mock adapter for testing"""

    def __init__(self, platform_name: str, markets: list = None):
        super().__init__(platform_name)
        self._markets = markets or []

    async def discover_markets(self):
        return self._markets


class TestMarketDiscoveryServiceWithMock:
    """Tests for MarketDiscoveryService with mock adapters"""

    def test_save_markets(self, tmp_path):
        """Test saving discovered markets"""
        output_dir = tmp_path / "markets"
        service = MarketDiscoveryService(output_dir=str(output_dir))

        markets = [
            StandardMarket(
                platform="TEST",
                market_id="m1",
                event_id="e1",
                event_name="Test Event",
                market_type="MATCH_ODDS",
                sport="Soccer",
                start_time="2024-01-15T15:00:00Z",
                discovered_at="2024-01-15T10:00:00Z",
                status="OPEN",
                in_play=False,
                outcomes=[],
                metadata={}
            )
        ]

        service._save_markets(markets)

        # Check that files were created
        latest_file = output_dir / "latest_markets.json"
        assert latest_file.exists()
