"""
市场匹配服务

整合标准化器和匹配引擎，提供完整的市场匹配功能。
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from src.arbitrage.common.utils import generate_id

from .config import MarketMatchingConfig
from .engine import MatchEngine, MatchResult
from .normalizer import EventNormalizer, NormalizedEvent


@dataclass
class MatchedPair:
    """
    匹配的市场对

    表示 Polymarket 和 OrbitExch 上的同一场比赛。
    """
    pair_id: str                      # 唯一标识
    sport: str                        # 标准 sport
    competition: str                  # 标准 competition
    # Polymarket 侧
    polymarket_home_team: str
    polymarket_away_team: str
    polymarket_event_id: str
    polymarket_data: dict[str, Any]
    # OrbitExch 侧
    orbitexch_home_team: str
    orbitexch_away_team: str
    orbitexch_data: dict[str, Any]
    # 匹配信息
    home_similarity: int              # 主队相似度分数
    away_similarity: int              # 客队相似度分数
    total_similarity: int             # 总相似度
    confidence: float                 # 置信度 (0-1)

    @classmethod
    def from_match_result(cls, result: MatchResult) -> "MatchedPair":
        """从匹配结果创建 MatchedPair"""
        poly = result.polymarket_event
        orbit = result.orbitexch_event

        # 计算置信度：基于相似度的简单评估
        # 相似度越高，置信度越高
        max_possible_similarity = 10  # 假设最大可能相似度
        confidence = min(result.total_similarity / max_possible_similarity, 1.0)

        return cls(
            pair_id=generate_id("pair"),
            sport=poly.sport,
            competition=poly.competition,
            polymarket_home_team=poly.home_team,
            polymarket_away_team=poly.away_team,
            polymarket_event_id=poly.original_data.get("event_id", ""),
            polymarket_data=poly.original_data,
            orbitexch_home_team=orbit.home_team,
            orbitexch_away_team=orbit.away_team,
            orbitexch_data=orbit.original_data,
            home_similarity=result.home_similarity,
            away_similarity=result.away_similarity,
            total_similarity=result.total_similarity,
            confidence=confidence,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "pair_id": self.pair_id,
            "sport": self.sport,
            "competition": self.competition,
            "polymarket": {
                "home_team": self.polymarket_home_team,
                "away_team": self.polymarket_away_team,
                "event_id": self.polymarket_event_id,
                "data": self.polymarket_data,
            },
            "orbitexch": {
                "home_team": self.orbitexch_home_team,
                "away_team": self.orbitexch_away_team,
                "data": self.orbitexch_data,
            },
            "matching": {
                "home_similarity": self.home_similarity,
                "away_similarity": self.away_similarity,
                "total_similarity": self.total_similarity,
                "confidence": self.confidence,
            },
        }


@dataclass
class MatchingSummary:
    """匹配摘要信息"""
    total_polymarket_events: int
    total_orbitexch_events: int
    matched_pairs: int
    unmatched_polymarket: int
    unmatched_orbitexch: int
    by_sport: dict[str, int] = field(default_factory=dict)
    by_competition: dict[str, int] = field(default_factory=dict)


class MarketMatchingService:
    """
    市场匹配服务

    提供完整的市场匹配功能，包括：
    - 事件标准化
    - 市场匹配
    - 结果管理
    """

    def __init__(
        self,
        config: MarketMatchingConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config or MarketMatchingConfig()
        self._log = logger or logging.getLogger(self.__class__.__name__)

        self._normalizer = EventNormalizer(self.config)
        self._engine = MatchEngine(self.config, self._log)

        # 缓存最近的匹配结果
        self._matched_pairs: list[MatchedPair] = []
        self._last_summary: MatchingSummary | None = None

    def match_markets(
        self,
        polymarket_events: list,
        orbitexch_events: list,
    ) -> list[MatchedPair]:
        """
        执行市场匹配

        Args:
            polymarket_events: Polymarket 发现的比赛事件
            orbitexch_events: OrbitExch 发现的比赛事件

        Returns:
            匹配的市场对列表
        """
        if not self.config.enabled:
            self._log.warning("Market matching service is disabled")
            return []

        self._log.info(
            f"Starting market matching: "
            f"{len(polymarket_events)} Polymarket events, "
            f"{len(orbitexch_events)} OrbitExch events"
        )

        # Step 1: 标准化事件
        poly_normalized, orbit_normalized = self._normalizer.normalize_events(
            polymarket_events,
            orbitexch_events,
        )

        self._log.debug(
            f"Normalized: {len(poly_normalized)} Polymarket, "
            f"{len(orbit_normalized)} OrbitExch"
        )

        # Step 2: 执行匹配
        match_results = self._engine.match_events(poly_normalized, orbit_normalized)

        # Step 3: 转换为 MatchedPair
        self._matched_pairs = [
            MatchedPair.from_match_result(r) for r in match_results
        ]

        # Step 4: 生成摘要
        self._last_summary = self._generate_summary(
            poly_normalized,
            orbit_normalized,
            self._matched_pairs,
        )

        self._log.info(
            f"Matching complete: {len(self._matched_pairs)} pairs matched"
        )

        return self._matched_pairs

    def _generate_summary(
        self,
        poly_events: list[NormalizedEvent],
        orbit_events: list[NormalizedEvent],
        matched_pairs: list[MatchedPair],
    ) -> MatchingSummary:
        """生成匹配摘要"""
        # 统计已匹配的事件
        matched_poly_ids = {
            p.polymarket_event_id for p in matched_pairs
        }
        matched_orbit_keys = {
            f"{p.orbitexch_home_team}::{p.orbitexch_away_team}" for p in matched_pairs
        }

        # 按 sport 和 competition 统计
        by_sport: dict[str, int] = {}
        by_competition: dict[str, int] = {}

        for pair in matched_pairs:
            by_sport[pair.sport] = by_sport.get(pair.sport, 0) + 1
            by_competition[pair.competition] = by_competition.get(pair.competition, 0) + 1

        return MatchingSummary(
            total_polymarket_events=len(poly_events),
            total_orbitexch_events=len(orbit_events),
            matched_pairs=len(matched_pairs),
            unmatched_polymarket=len(poly_events) - len(matched_poly_ids),
            unmatched_orbitexch=len(orbit_events) - len(matched_orbit_keys),
            by_sport=by_sport,
            by_competition=by_competition,
        )

    def get_matched_pairs(self) -> list[MatchedPair]:
        """获取所有匹配的市场对"""
        return self._matched_pairs.copy()

    def get_pair_by_id(self, pair_id: str) -> MatchedPair | None:
        """根据 ID 获取匹配对"""
        for pair in self._matched_pairs:
            if pair.pair_id == pair_id:
                return pair
        return None

    def get_pairs_by_sport(self, sport: str) -> list[MatchedPair]:
        """根据 sport 获取匹配对"""
        return [p for p in self._matched_pairs if p.sport == sport]

    def get_pairs_by_competition(self, competition: str) -> list[MatchedPair]:
        """根据 competition 获取匹配对"""
        return [p for p in self._matched_pairs if p.competition == competition]

    def get_summary(self) -> MatchingSummary | None:
        """获取最近的匹配摘要"""
        return self._last_summary

    def clear_cache(self) -> None:
        """清除缓存"""
        self._matched_pairs = []
        self._last_summary = None
