"""
市场匹配引擎

核心匹配逻辑：
1. 按 sport + competition 分组（完全相等）
2. 组内使用 get_similar 匹配队名
3. 选择最佳匹配
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from src.arbitrage.common.utils import get_similar

from .config import MarketMatchingConfig
from .normalizer import NormalizedEvent


@dataclass
class MatchResult:
    """
    匹配结果

    记录单次队名匹配的详细信息。
    """
    polymarket_event: NormalizedEvent
    orbitexch_event: NormalizedEvent
    home_similarity: int      # 主队相似度分数
    away_similarity: int      # 客队相似度分数
    home_matched_chars: int   # 主队匹配字符数
    away_matched_chars: int   # 客队匹配字符数
    total_similarity: int     # 总相似度
    total_matched_chars: int  # 总匹配字符数

    @property
    def is_valid(self) -> bool:
        """匹配是否有效（两队都匹配成功）"""
        return self.home_similarity > 0 and self.away_similarity > 0


class MatchEngine:
    """
    市场匹配引擎

    负责执行匹配逻辑，将 Polymarket 和 OrbitExch 的事件进行配对。
    """

    def __init__(
        self,
        config: MarketMatchingConfig,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self._log = logger or logging.getLogger(self.__class__.__name__)

    def _calculate_team_similarity(
        self,
        team1: str,
        team2: str,
    ) -> tuple[int, int]:
        """
        计算两个队名的相似度

        Args:
            team1: 队名1（已预处理）
            team2: 队名2（已预处理）

        Returns:
            (相似度分数, 匹配字符数)
        """
        # 使用 get_similar 计算相似度
        # shorten=False 使用子串匹配
        similarity = get_similar(False, team1, team2)

        # 计算匹配字符数：取较短队名的长度作为估计
        matched_chars = 0
        if similarity > 0:
            # 简化计算：使用两个队名中较短的长度
            matched_chars = min(len(team1), len(team2))

        return similarity, matched_chars

    def _match_single_pair(
        self,
        poly_event: NormalizedEvent,
        orbit_event: NormalizedEvent,
    ) -> MatchResult:
        """
        匹配单对事件

        Args:
            poly_event: Polymarket 事件
            orbit_event: OrbitExch 事件

        Returns:
            匹配结果
        """
        # 匹配主队
        home_sim, home_chars = self._calculate_team_similarity(
            poly_event.home_team_normalized,
            orbit_event.home_team_normalized,
        )

        # 匹配客队
        away_sim, away_chars = self._calculate_team_similarity(
            poly_event.away_team_normalized,
            orbit_event.away_team_normalized,
        )

        return MatchResult(
            polymarket_event=poly_event,
            orbitexch_event=orbit_event,
            home_similarity=home_sim,
            away_similarity=away_sim,
            home_matched_chars=home_chars,
            away_matched_chars=away_chars,
            total_similarity=home_sim + away_sim,
            total_matched_chars=home_chars + away_chars,
        )

    def _select_best_match(
        self,
        candidates: list[MatchResult],
    ) -> MatchResult | None:
        """
        从候选匹配中选择最佳匹配

        选择规则（来自 requirements.md）：
        1. 先按 total_similarity（get_similar 返回值总和）降序
        2. 相等时按 total_matched_chars（匹配字符数总和）降序

        Args:
            candidates: 候选匹配结果列表

        Returns:
            最佳匹配结果，如果没有有效匹配则返回 None
        """
        # 过滤无效匹配
        valid_candidates = [c for c in candidates if c.is_valid]

        if not valid_candidates:
            return None

        # 排序：先按 total_similarity 降序，再按 total_matched_chars 降序
        valid_candidates.sort(
            key=lambda x: (x.total_similarity, x.total_matched_chars),
            reverse=True,
        )

        return valid_candidates[0]

    def match_within_group(
        self,
        poly_events: list[NormalizedEvent],
        orbit_events: list[NormalizedEvent],
    ) -> list[MatchResult]:
        """
        在同一 sport+competition 分组内进行匹配

        每个 Polymarket 事件最多匹配一个 OrbitExch 事件，
        每个 OrbitExch 事件最多被匹配一次。

        Args:
            poly_events: 同组的 Polymarket 事件
            orbit_events: 同组的 OrbitExch 事件

        Returns:
            匹配结果列表
        """
        results: list[MatchResult] = []
        used_orbit_indices: set[int] = set()

        # 获取该 competition 的最大匹配数量限制
        max_matches = None
        if poly_events:
            competition = poly_events[0].competition
            max_matches = self.config.competition_max_matches.get(competition)
            if max_matches is not None:
                self._log.debug(f"Competition '{competition}' has max_matches limit: {max_matches}")

        for poly_event in poly_events:
            # 检查是否达到该 competition 的最大匹配数量
            if max_matches is not None and len(results) >= max_matches:
                self._log.info(
                    f"Reached max_matches limit ({max_matches}) for competition '{poly_event.competition}', "
                    f"stopping matching"
                )
                break
            # 收集所有候选匹配
            candidates: list[tuple[int, MatchResult]] = []

            for orbit_idx, orbit_event in enumerate(orbit_events):
                if orbit_idx in used_orbit_indices:
                    continue

                result = self._match_single_pair(poly_event, orbit_event)
                if result.is_valid and result.total_similarity >= self.config.min_similarity:
                    candidates.append((orbit_idx, result))

            if not candidates:
                continue

            # 选择最佳匹配
            candidates.sort(
                key=lambda x: (x[1].total_similarity, x[1].total_matched_chars),
                reverse=True,
            )

            best_orbit_idx, best_result = candidates[0]
            results.append(best_result)
            used_orbit_indices.add(best_orbit_idx)

            self._log.debug(
                f"Matched: {poly_event.home_team} vs {poly_event.away_team} "
                f"<-> {best_result.orbitexch_event.home_team} vs {best_result.orbitexch_event.away_team} "
                f"(sim={best_result.total_similarity}, chars={best_result.total_matched_chars})"
            )

        return results

    def match_events(
        self,
        poly_events: list[NormalizedEvent],
        orbit_events: list[NormalizedEvent],
    ) -> list[MatchResult]:
        """
        匹配所有事件

        流程：
        1. 按 sport+competition 分组
        2. 只在相同分组内进行匹配
        3. 返回所有匹配结果

        Args:
            poly_events: 标准化后的 Polymarket 事件
            orbit_events: 标准化后的 OrbitExch 事件

        Returns:
            所有匹配结果
        """
        # 按 group_key 分组
        poly_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        orbit_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)

        for event in poly_events:
            poly_groups[event.group_key].append(event)

        for event in orbit_events:
            orbit_groups[event.group_key].append(event)

        # 找出公共的分组
        common_keys = set(poly_groups.keys()) & set(orbit_groups.keys())

        self._log.info(
            f"Found {len(common_keys)} common sport-competition groups "
            f"(Polymarket: {len(poly_groups)}, OrbitExch: {len(orbit_groups)})"
        )

        # 在每个分组内进行匹配
        all_results: list[MatchResult] = []

        for key in common_keys:
            poly_group = poly_groups[key]
            orbit_group = orbit_groups[key]

            self._log.debug(
                f"Matching group '{key}': "
                f"{len(poly_group)} Polymarket events, "
                f"{len(orbit_group)} OrbitExch events"
            )

            group_results = self.match_within_group(poly_group, orbit_group)
            all_results.extend(group_results)

        self._log.info(f"Total matched pairs: {len(all_results)}")
        return all_results
