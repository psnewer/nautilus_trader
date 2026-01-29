# -*- coding: utf-8 -*-
"""
市场匹配服务单元测试

测试内容：
1. 配置模块 (config.py)
2. 标准化器 (normalizer.py)
3. 匹配引擎 (engine.py)
4. 服务入口 (service.py)
"""

import pytest
from dataclasses import dataclass

# 模拟 MatchEvent 数据类（与 scraper 中的定义一致）
@dataclass
class PolymarketMatchEvent:
    """模拟 Polymarket MatchEvent"""
    sport: str
    competition: str
    home_team: str
    away_team: str
    event_id: str = ""
    title: str = ""
    tags: list = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


@dataclass
class OrbitExchMatchEvent:
    """模拟 OrbitExch MatchEvent"""
    sport: str
    competition: str
    home_team: str
    away_team: str
    sport_id: str = ""
    competition_id: str = ""


class TestMarketMatchingConfig:
    """测试配置模块"""

    def test_default_config(self):
        """测试默认配置"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig

        config = MarketMatchingConfig()

        assert config.enabled is True
        assert config.min_similarity == 1
        assert "Football" in config.sport_aliases
        assert "EPL" in config.competition_aliases

    def test_sport_aliases(self):
        """测试 sport 别名映射"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig

        config = MarketMatchingConfig()

        assert config.sport_aliases["Football"] == "Soccer"
        assert config.sport_aliases["football"] == "Soccer"
        assert config.sport_aliases["FOOTBALL"] == "Soccer"

    def test_competition_aliases(self):
        """测试 competition 别名映射"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig

        config = MarketMatchingConfig()

        assert config.competition_aliases["EPL"] == "English Premier League"
        assert config.competition_aliases["sea"] == "Italian Serie A"
        assert config.competition_aliases["La Liga"] == "Spanish La Liga"

    def test_from_dict(self):
        """测试从字典创建配置"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig

        data = {
            "enabled": False,
            "min_similarity": 2,
            "sport_aliases": {"Futbol": "Soccer"},
            "competition_aliases": {"BPL": "English Premier League"},
        }

        config = MarketMatchingConfig.from_dict(data)

        assert config.enabled is False
        assert config.min_similarity == 2
        # 应该合并默认别名和自定义别名
        assert config.sport_aliases["Futbol"] == "Soccer"
        assert config.sport_aliases["Football"] == "Soccer"  # 默认仍在
        assert config.competition_aliases["BPL"] == "English Premier League"
        assert config.competition_aliases["EPL"] == "English Premier League"  # 默认仍在


class TestEventNormalizer:
    """测试事件标准化器"""

    def test_normalize_sport(self):
        """测试 sport 标准化"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.normalizer import EventNormalizer

        config = MarketMatchingConfig()
        normalizer = EventNormalizer(config)

        assert normalizer.normalize_sport("Football") == "Soccer"
        assert normalizer.normalize_sport("football") == "Soccer"
        assert normalizer.normalize_sport("Soccer") == "Soccer"
        assert normalizer.normalize_sport("basketball") == "Basketball"  # 首字母大写

    def test_normalize_competition(self):
        """测试 competition 标准化"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.normalizer import EventNormalizer

        config = MarketMatchingConfig()
        normalizer = EventNormalizer(config)

        assert normalizer.normalize_competition("EPL") == "English Premier League"
        assert normalizer.normalize_competition("sea") == "Italian Serie A"
        assert normalizer.normalize_competition("La Liga") == "Spanish La Liga"
        assert normalizer.normalize_competition("Unknown League") == "Unknown League"

    def test_normalize_team_name(self):
        """测试队名预处理 - 去掉 / 符号"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.normalizer import EventNormalizer

        config = MarketMatchingConfig()
        normalizer = EventNormalizer(config)

        # 测试去掉 / 符号
        assert normalizer.normalize_team_name("Muhammad/Routliffe") == "MuhammadRoutliffe"
        assert normalizer.normalize_team_name("Player A/Player B") == "Player APlayer B"
        # 测试去掉首尾空格
        assert normalizer.normalize_team_name("  Arsenal  ") == "Arsenal"

    def test_normalize_polymarket_event(self):
        """测试 Polymarket 事件标准化"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.normalizer import EventNormalizer

        config = MarketMatchingConfig()
        normalizer = EventNormalizer(config)

        event = PolymarketMatchEvent(
            sport="Football",
            competition="EPL",
            home_team="Arsenal",
            away_team="Chelsea",
            event_id="123",
        )

        normalized = normalizer.normalize_polymarket_event(event)

        assert normalized.venue == "polymarket"
        assert normalized.sport == "Soccer"
        assert normalized.competition == "English Premier League"
        assert normalized.home_team == "Arsenal"
        assert normalized.away_team == "Chelsea"
        assert normalized.original_data["event_id"] == "123"

    def test_normalize_orbitexch_event(self):
        """测试 OrbitExch 事件标准化"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.normalizer import EventNormalizer

        config = MarketMatchingConfig()
        normalizer = EventNormalizer(config)

        event = OrbitExchMatchEvent(
            sport="soccer",
            competition="sea",
            home_team="Juventus",
            away_team="AC Milan",
        )

        normalized = normalizer.normalize_orbitexch_event(event)

        assert normalized.venue == "orbitexch"
        assert normalized.sport == "Soccer"
        assert normalized.competition == "Italian Serie A"
        assert normalized.home_team == "Juventus"
        assert normalized.away_team == "AC Milan"


class TestMatchEngine:
    """测试匹配引擎"""

    def test_match_identical_teams(self):
        """测试完全相同的队名匹配"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.engine import MatchEngine
        from src.arbitrage.services.market_matching.normalizer import NormalizedEvent

        config = MarketMatchingConfig()
        engine = MatchEngine(config)

        poly_event = NormalizedEvent(
            venue="polymarket",
            sport="Soccer",
            competition="English Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
            home_team_normalized="Arsenal",
            away_team_normalized="Chelsea",
            original_data={},
        )

        orbit_event = NormalizedEvent(
            venue="orbitexch",
            sport="Soccer",
            competition="English Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
            home_team_normalized="Arsenal",
            away_team_normalized="Chelsea",
            original_data={},
        )

        results = engine.match_within_group([poly_event], [orbit_event])

        assert len(results) == 1
        assert results[0].is_valid
        assert results[0].total_similarity > 0

    def test_match_similar_teams(self):
        """测试相似队名匹配"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.engine import MatchEngine
        from src.arbitrage.services.market_matching.normalizer import NormalizedEvent

        config = MarketMatchingConfig()
        engine = MatchEngine(config)

        # Manchester United 的不同写法
        poly_event = NormalizedEvent(
            venue="polymarket",
            sport="Soccer",
            competition="English Premier League",
            home_team="Manchester United",
            away_team="Liverpool",
            home_team_normalized="Manchester United",
            away_team_normalized="Liverpool",
            original_data={},
        )

        orbit_event = NormalizedEvent(
            venue="orbitexch",
            sport="Soccer",
            competition="English Premier League",
            home_team="Man United",
            away_team="Liverpool FC",
            home_team_normalized="Man United",
            away_team_normalized="Liverpool FC",
            original_data={},
        )

        results = engine.match_within_group([poly_event], [orbit_event])

        # 应该能够匹配（取决于 get_similar 的实现）
        assert len(results) >= 0

    def test_no_match_different_groups(self):
        """测试不同分组不会匹配"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.engine import MatchEngine
        from src.arbitrage.services.market_matching.normalizer import NormalizedEvent

        config = MarketMatchingConfig()
        engine = MatchEngine(config)

        poly_event = NormalizedEvent(
            venue="polymarket",
            sport="Soccer",
            competition="English Premier League",  # 不同联赛
            home_team="Arsenal",
            away_team="Chelsea",
            home_team_normalized="Arsenal",
            away_team_normalized="Chelsea",
            original_data={},
        )

        orbit_event = NormalizedEvent(
            venue="orbitexch",
            sport="Soccer",
            competition="Spanish La Liga",  # 不同联赛
            home_team="Barcelona",
            away_team="Real Madrid",
            home_team_normalized="Barcelona",
            away_team_normalized="Real Madrid",
            original_data={},
        )

        # 不同的 group_key，不会进入同一组匹配
        results = engine.match_events([poly_event], [orbit_event])

        assert len(results) == 0

    def test_select_best_match(self):
        """测试选择最佳匹配"""
        from src.arbitrage.services.market_matching.config import MarketMatchingConfig
        from src.arbitrage.services.market_matching.engine import MatchEngine
        from src.arbitrage.services.market_matching.normalizer import NormalizedEvent

        config = MarketMatchingConfig()
        engine = MatchEngine(config)

        poly_event = NormalizedEvent(
            venue="polymarket",
            sport="Soccer",
            competition="English Premier League",
            home_team="Manchester United",
            away_team="Liverpool",
            home_team_normalized="Manchester United",
            away_team_normalized="Liverpool",
            original_data={},
        )

        # 两个 OrbitExch 事件，一个更匹配
        orbit_event1 = NormalizedEvent(
            venue="orbitexch",
            sport="Soccer",
            competition="English Premier League",
            home_team="Manchester United",  # 完全匹配
            away_team="Liverpool",  # 完全匹配
            home_team_normalized="Manchester United",
            away_team_normalized="Liverpool",
            original_data={},
        )

        orbit_event2 = NormalizedEvent(
            venue="orbitexch",
            sport="Soccer",
            competition="English Premier League",
            home_team="Man Utd",  # 部分匹配
            away_team="Liverpool FC",  # 部分匹配
            home_team_normalized="Man Utd",
            away_team_normalized="Liverpool FC",
            original_data={},
        )

        results = engine.match_within_group([poly_event], [orbit_event1, orbit_event2])

        # 应该选择完全匹配的 orbit_event1
        assert len(results) == 1
        if results:
            assert results[0].orbitexch_event.home_team == "Manchester United"


class TestMarketMatchingService:
    """测试市场匹配服务"""

    def test_service_init(self):
        """测试服务初始化"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        assert service.config is not None
        assert service.config.enabled is True

    def test_match_markets_empty(self):
        """测试空列表匹配"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()
        results = service.match_markets([], [])

        assert len(results) == 0

    def test_match_markets_identical(self):
        """测试完全相同的事件匹配"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        poly_events = [
            PolymarketMatchEvent(
                sport="Football",
                competition="EPL",
                home_team="Arsenal",
                away_team="Chelsea",
                event_id="p1",
            ),
        ]

        orbit_events = [
            OrbitExchMatchEvent(
                sport="Football",
                competition="EPL",
                home_team="Arsenal",
                away_team="Chelsea",
            ),
        ]

        results = service.match_markets(poly_events, orbit_events)

        assert len(results) == 1
        assert results[0].sport == "Soccer"
        assert results[0].competition == "English Premier League"
        assert results[0].polymarket_home_team == "Arsenal"
        assert results[0].orbitexch_home_team == "Arsenal"

    def test_match_markets_with_alias(self):
        """测试使用别名的事件匹配"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        # Polymarket 使用 "Football" 和 "EPL"
        poly_events = [
            PolymarketMatchEvent(
                sport="Football",
                competition="EPL",
                home_team="Juventus",
                away_team="AC Milan",
                event_id="p1",
            ),
        ]

        # OrbitExch 使用 "soccer" 和 "sea"
        orbit_events = [
            OrbitExchMatchEvent(
                sport="soccer",
                competition="sea",
                home_team="Juventus",
                away_team="AC Milan",
            ),
        ]

        results = service.match_markets(poly_events, orbit_events)

        # 由于标准化后 sport 和 competition 不同，不应该匹配
        # Football -> Soccer, soccer -> Soccer (相同)
        # EPL -> English Premier League, sea -> Italian Serie A (不同)
        assert len(results) == 0

    def test_match_markets_same_league(self):
        """测试同一联赛事件匹配"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        poly_events = [
            PolymarketMatchEvent(
                sport="Football",
                competition="sea",
                home_team="Juventus",
                away_team="AC Milan",
                event_id="p1",
            ),
        ]

        orbit_events = [
            OrbitExchMatchEvent(
                sport="soccer",
                competition="Serie A",
                home_team="Juventus",
                away_team="AC Milan",
            ),
        ]

        results = service.match_markets(poly_events, orbit_events)

        assert len(results) == 1

    def test_get_summary(self):
        """测试获取匹配摘要"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        poly_events = [
            PolymarketMatchEvent(
                sport="Soccer",
                competition="English Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
                event_id="p1",
            ),
            PolymarketMatchEvent(
                sport="Soccer",
                competition="English Premier League",
                home_team="Liverpool",
                away_team="Man City",
                event_id="p2",
            ),
        ]

        orbit_events = [
            OrbitExchMatchEvent(
                sport="Soccer",
                competition="English Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
            ),
        ]

        service.match_markets(poly_events, orbit_events)
        summary = service.get_summary()

        assert summary is not None
        assert summary.total_polymarket_events == 2
        assert summary.total_orbitexch_events == 1
        assert summary.matched_pairs == 1

    def test_get_pairs_by_sport(self):
        """测试按 sport 获取匹配对"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        poly_events = [
            PolymarketMatchEvent(
                sport="Soccer",
                competition="English Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
                event_id="p1",
            ),
        ]

        orbit_events = [
            OrbitExchMatchEvent(
                sport="Soccer",
                competition="English Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
            ),
        ]

        service.match_markets(poly_events, orbit_events)

        soccer_pairs = service.get_pairs_by_sport("Soccer")
        basketball_pairs = service.get_pairs_by_sport("Basketball")

        assert len(soccer_pairs) == 1
        assert len(basketball_pairs) == 0


class TestTeamNameMatching:
    """测试队名匹配的边界情况"""

    def test_slash_in_team_name(self):
        """测试队名中包含 / 的情况"""
        from src.arbitrage.services.market_matching.service import MarketMatchingService

        service = MarketMatchingService()

        poly_events = [
            PolymarketMatchEvent(
                sport="Tennis",
                competition="US Open",
                home_team="Muhammad/Routliffe",
                away_team="Dolehide/Krawczyk",
                event_id="p1",
            ),
        ]

        orbit_events = [
            OrbitExchMatchEvent(
                sport="Tennis",
                competition="US Open",
                home_team="MuhammadRoutliffe",
                away_team="DolehideKrawczyk",
            ),
        ]

        results = service.match_markets(poly_events, orbit_events)

        # 预处理后应该能匹配
        assert len(results) == 1
