"""
事件标准化预处理器

负责：
1. 标准化 sport 名称
2. 标准化 competition 名称
3. 预处理队名（去掉 / 等特殊字符）
"""

from dataclasses import dataclass
from typing import Any

from .config import MarketMatchingConfig


@dataclass
class NormalizedEvent:
    """
    标准化后的比赛事件

    包含原始数据和标准化后的数据，用于匹配和后续处理。
    """
    venue: str                    # 平台标识 (polymarket / orbitexch)
    sport: str                    # 标准化后的 sport
    competition: str              # 标准化后的 competition
    home_team: str                # 原始主队名
    away_team: str                # 原始客队名
    home_team_normalized: str     # 预处理后的主队名（用于匹配）
    away_team_normalized: str     # 预处理后的客队名（用于匹配）
    original_data: dict[str, Any]  # 原始事件数据

    @property
    def group_key(self) -> str:
        """用于分组的键：sport + competition"""
        return f"{self.sport}::{self.competition}"


class EventNormalizer:
    """
    事件标准化器

    将不同平台的原始事件数据转换为标准化格式。
    """

    def __init__(self, config: MarketMatchingConfig):
        self.config = config

    def normalize_sport(self, sport: str) -> str:
        """
        标准化 sport 名称

        Args:
            sport: 原始 sport 名称

        Returns:
            标准化后的 sport 名称
        """
        # 先查找别名映射
        if sport in self.config.sport_aliases:
            return self.config.sport_aliases[sport]

        # 如果没有别名，返回首字母大写形式
        return sport.strip().title()

    def normalize_competition(self, competition: str) -> str:
        """
        标准化 competition 名称

        Args:
            competition: 原始 competition 名称

        Returns:
            标准化后的 competition 名称
        """
        # 先查找别名映射
        if competition in self.config.competition_aliases:
            return self.config.competition_aliases[competition]

        # 如果没有别名，返回原值（去掉首尾空格）
        return competition.strip()

    def normalize_team_name(self, team_name: str) -> str:
        """
        预处理队名用于匹配

        规则：
        - 去掉 / 符号（应对多队员情况，如 Muhammad/Routliffe）
        - 去掉首尾空格

        Args:
            team_name: 原始队名

        Returns:
            预处理后的队名
        """
        # 去掉 / 符号
        normalized = team_name.replace("/", "")
        # 去掉首尾空格
        return normalized.strip()

    def normalize_polymarket_event(self, event: Any) -> NormalizedEvent:
        """
        标准化 Polymarket 事件

        Args:
            event: PolymarketScraper.MatchEvent 实例

        Returns:
            标准化后的事件
        """
        sport = self.normalize_sport(event.sport)
        competition = self.normalize_competition(event.competition)
        home_normalized = self.normalize_team_name(event.home_team)
        away_normalized = self.normalize_team_name(event.away_team)

        return NormalizedEvent(
            venue="polymarket",
            sport=sport,
            competition=competition,
            home_team=event.home_team,
            away_team=event.away_team,
            home_team_normalized=home_normalized,
            away_team_normalized=away_normalized,
            original_data={
                "event_id": getattr(event, "event_id", ""),
                "title": getattr(event, "title", ""),
                "tags": getattr(event, "tags", []),
            },
        )

    def normalize_orbitexch_event(self, event: Any) -> NormalizedEvent:
        """
        标准化 OrbitExch 事件

        Args:
            event: OrbitExchScraper.MatchEvent 实例

        Returns:
            标准化后的事件
        """
        sport = self.normalize_sport(event.sport)
        competition = self.normalize_competition(event.competition)
        home_normalized = self.normalize_team_name(event.home_team)
        away_normalized = self.normalize_team_name(event.away_team)

        return NormalizedEvent(
            venue="orbitexch",
            sport=sport,
            competition=competition,
            home_team=event.home_team,
            away_team=event.away_team,
            home_team_normalized=home_normalized,
            away_team_normalized=away_normalized,
            original_data={
                "sport_id": getattr(event, "sport_id", ""),
                "competition_id": getattr(event, "competition_id", ""),
            },
        )

    def normalize_events(
        self,
        polymarket_events: list,
        orbitexch_events: list,
    ) -> tuple[list[NormalizedEvent], list[NormalizedEvent]]:
        """
        批量标准化事件

        Args:
            polymarket_events: Polymarket 事件列表
            orbitexch_events: OrbitExch 事件列表

        Returns:
            (标准化后的 Polymarket 事件, 标准化后的 OrbitExch 事件)
        """
        poly_normalized = [
            self.normalize_polymarket_event(e) for e in polymarket_events
        ]
        orbit_normalized = [
            self.normalize_orbitexch_event(e) for e in orbitexch_events
        ]
        return poly_normalized, orbit_normalized
