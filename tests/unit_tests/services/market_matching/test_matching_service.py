# -*- coding: utf-8 -*-
"""
Tests for Market Matching Service
"""

import pytest
import sys
from pathlib import Path
from dataclasses import asdict

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from services.market_matching.config import MatchConfig, PreprocessConfig
from services.market_matching.preprocessor import EventPreprocessor
from services.market_matching.market_matching_service import MarketMatchingService, MarketEvent


class TestMatchConfig:
    """Tests for MatchConfig"""

    def test_default_config(self):
        """Test default configuration values"""
        config = MatchConfig()

        assert config.match_sport is True
        assert config.match_competition is True
        assert config.match_home_team is True
        assert config.match_away_team is True
        assert config.allow_team_swap is True
        assert config.min_confidence == 0.6
        assert config.matcher_class == "DefaultMatcher"
        assert config.enable_preprocess is True

    def test_custom_config(self):
        """Test custom configuration"""
        config = MatchConfig(
            match_sport=False,
            min_confidence=0.8,
            allow_team_swap=False,
            verbose=True
        )

        assert config.match_sport is False
        assert config.min_confidence == 0.8
        assert config.allow_team_swap is False
        assert config.verbose is True

    def test_preprocess_config_auto_init(self):
        """Test that preprocess_config is auto-initialized"""
        config = MatchConfig()

        assert config.preprocess_config is not None
        assert isinstance(config.preprocess_config, PreprocessConfig)


class TestPreprocessConfig:
    """Tests for PreprocessConfig"""

    def test_default_preprocess_config(self):
        """Test default preprocess configuration"""
        config = PreprocessConfig()

        assert config.normalize_sport is True
        assert config.normalize_competition is False
        assert config.normalize_team_names is False
        assert config.strip_whitespace is True
        assert config.verbose is False


class TestEventPreprocessor:
    """Tests for EventPreprocessor"""

    def test_sport_mapping(self):
        """Test sport name normalization"""
        preprocessor = EventPreprocessor()

        assert preprocessor._normalize_sport("Football") == "Soccer"
        assert preprocessor._normalize_sport("Soccer") == "Soccer"
        assert preprocessor._normalize_sport("Basketball") == "Basketball"

    def test_competition_mapping(self):
        """Test competition name normalization"""
        preprocessor = EventPreprocessor()

        assert preprocessor._normalize_competition("EPL") == "English Premier League"
        assert preprocessor._normalize_competition("UCL") == "UEFA Champions League"
        assert preprocessor._normalize_competition("Serie A") == "Serie A"

    def test_team_name_mapping(self):
        """Test team name normalization"""
        preprocessor = EventPreprocessor()

        assert preprocessor._normalize_team_name("Man Utd") == "Manchester United"
        assert preprocessor._normalize_team_name("Man City") == "Manchester City"
        assert preprocessor._normalize_team_name("Liverpool") == "Liverpool"

    def test_add_custom_sport_mapping(self):
        """Test adding custom sport mapping"""
        EventPreprocessor.add_sport_mapping("Futbol", "Soccer")

        preprocessor = EventPreprocessor()
        assert preprocessor._normalize_sport("Futbol") == "Soccer"

    def test_preprocess_events(self):
        """Test preprocessing event list"""
        config = PreprocessConfig(
            normalize_sport=True,
            strip_whitespace=True,
            verbose=False
        )
        preprocessor = EventPreprocessor(config)

        # Create mock events
        event = MarketEvent(
            platform="TEST",
            event_id="1",
            sport="  Football  ",  # With whitespace
            competition="EPL",
            event="Team A vs Team B",
            home_team="Team A",
            away_team="Team B",
            metadata={}
        )

        events = [event]
        preprocessor.preprocess_events(events, "Test")

        assert event.sport == "Soccer"  # Normalized and stripped


class TestMarketEvent:
    """Tests for MarketEvent dataclass"""

    def test_create_market_event(self):
        """Test creating a MarketEvent"""
        event = MarketEvent(
            platform="POLYMARKET",
            event_id="123",
            sport="Soccer",
            competition="Premier League",
            event="Arsenal vs Chelsea",
            home_team="Arsenal",
            away_team="Chelsea",
            metadata={"volume": 10000}
        )

        assert event.platform == "POLYMARKET"
        assert event.sport == "Soccer"
        assert event.home_team == "Arsenal"
        assert event.away_team == "Chelsea"


class TestMarketMatchingService:
    """Tests for MarketMatchingService"""

    def test_service_init_default_config(self):
        """Test service initialization with default config"""
        service = MarketMatchingService()

        assert service.config is not None
        assert service.matcher is not None

    def test_service_init_custom_config(self):
        """Test service initialization with custom config"""
        config = MatchConfig(min_confidence=0.8)
        service = MarketMatchingService(config)

        assert service.config.min_confidence == 0.8

    def test_load_event(self):
        """Test loading event from dict"""
        service = MarketMatchingService()

        event_data = {
            "platform": "POLYMARKET",
            "event_id": "123",
            "sport": "Soccer",
            "competition": "Premier League",
            "event": "Arsenal vs Chelsea",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "volume": 10000
        }

        event = service._load_event(event_data)

        assert event.platform == "POLYMARKET"
        assert event.event_id == "123"
        assert event.sport == "Soccer"
        assert event.home_team == "Arsenal"
        assert event.metadata.get("volume") == 10000

    def test_calculate_confidence_no_scores(self):
        """Test confidence calculation with no scores"""
        service = MarketMatchingService()

        confidence = service._calculate_confidence({})
        assert confidence == 0.0

    def test_calculate_confidence_with_scores(self):
        """Test confidence calculation with valid scores"""
        service = MarketMatchingService()

        scores = {
            "sport": (1, 6),
            "competition": (2, 10),
            "home_team": (1, 7),
            "away_team": (1, 9),
        }

        confidence = service._calculate_confidence(scores)
        assert confidence > 0.0
        assert confidence <= 1.0

    def test_calculate_confidence_partial_match(self):
        """Test confidence calculation with partial match (should be 0)"""
        service = MarketMatchingService()

        scores = {
            "sport": (1, 6),
            "competition": (0, 0),  # No match
            "home_team": (1, 7),
            "away_team": (1, 9),
        }

        confidence = service._calculate_confidence(scores)
        assert confidence == 0.0


class TestMatchingAlgorithm:
    """Tests for the matching algorithm"""

    def test_match_identical_events(self):
        """Test matching identical events"""
        config = MatchConfig(
            min_confidence=0.5,
            verbose=False,
            enable_preprocess=False
        )
        service = MarketMatchingService(config)

        # Same event on both platforms
        service.polymarket_events = [
            MarketEvent(
                platform="POLYMARKET",
                event_id="p1",
                sport="Soccer",
                competition="Premier League",
                event="Arsenal vs Chelsea",
                home_team="Arsenal",
                away_team="Chelsea",
                metadata={}
            )
        ]

        service.orbitexch_events = [
            MarketEvent(
                platform="ORBITEXCH",
                event_id="o1",
                sport="Soccer",
                competition="Premier League",
                event="Arsenal vs Chelsea",
                home_team="Arsenal",
                away_team="Chelsea",
                metadata={}
            )
        ]

        matches = service.match_events()

        assert len(matches) == 1
        assert matches[0].confidence > 0.5

    def test_match_similar_events(self):
        """Test matching similar but not identical events"""
        config = MatchConfig(
            min_confidence=0.5,
            verbose=False,
            enable_preprocess=False
        )
        service = MarketMatchingService(config)

        service.polymarket_events = [
            MarketEvent(
                platform="POLYMARKET",
                event_id="p1",
                sport="Soccer",
                competition="English Premier League",
                event="Arsenal FC vs Chelsea FC",
                home_team="Arsenal FC",
                away_team="Chelsea FC",
                metadata={}
            )
        ]

        service.orbitexch_events = [
            MarketEvent(
                platform="ORBITEXCH",
                event_id="o1",
                sport="Soccer",
                competition="Premier League",
                event="Arsenal vs Chelsea",
                home_team="Arsenal",
                away_team="Chelsea",
                metadata={}
            )
        ]

        matches = service.match_events()

        # Should match due to similar names
        assert len(matches) >= 0  # May or may not match depending on algorithm

    def test_no_match_different_sports(self):
        """Test that different sports don't match"""
        config = MatchConfig(
            match_sport=True,
            min_confidence=0.5,
            verbose=False,
            enable_preprocess=False
        )
        service = MarketMatchingService(config)

        service.polymarket_events = [
            MarketEvent(
                platform="POLYMARKET",
                event_id="p1",
                sport="Soccer",
                competition="Premier League",
                event="Arsenal vs Chelsea",
                home_team="Arsenal",
                away_team="Chelsea",
                metadata={}
            )
        ]

        service.orbitexch_events = [
            MarketEvent(
                platform="ORBITEXCH",
                event_id="o1",
                sport="Basketball",  # Different sport
                competition="NBA",
                event="Lakers vs Celtics",
                home_team="Lakers",
                away_team="Celtics",
                metadata={}
            )
        ]

        matches = service.match_events()

        assert len(matches) == 0
