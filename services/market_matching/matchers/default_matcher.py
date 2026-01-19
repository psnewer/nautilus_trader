# -*- coding: utf-8 -*-
"""
市场匹配服务 - 使用 utils 模块的相似度算法

规则:
1. Sport 匹配:
   - 先找完全相同的
   - 找不到则用 get_similar_for_matching，只选一个最佳匹配

2. Competition 匹配:
   - 先找完全相同的
   - 找不到则用 get_similar_for_matching
   - 当有多个匹配分数相同的最高分时，可以都选

3. Team 匹配:
   - 先找 home + away 都完全相同的
   - 找不到则用 get_similar_for_matching
   - 规则: if sim_home > 0 && sim_away > 0, 则匹配成功
   - 选择 home + away 匹配分数之和最大的那个
"""

import json
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from utils import get_similar_for_matching, split_elements, is_subsequence


@dataclass
class MatchResult:
    """匹配结果"""
    polymarket_event: dict
    orbitexch_event: dict
    match_scores: dict
    confidence: float

    def to_dict(self):
        return asdict(self)


class MarketMatcher:
    """市场匹配器"""

    # Sport 名称映射 (用于标准化)
    SPORT_MAPPING = {
        'Football': 'Soccer',
        'Association Football': 'Soccer',
        'American Football': 'American Football',
        'Gridiron': 'American Football',
    }

    def __init__(self, config: dict = None):
        self._log = logging.getLogger('MarketMatcher')
        self.config = config or {}
        self.min_confidence = self.config.get('min_confidence', 0.6)

    def _normalize_sport(self, sport: str) -> str:
        """标准化 sport 名称"""
        return self.SPORT_MAPPING.get(sport, sport)

    def match(self, poly_event: dict, orbit_event: dict, config: dict = None) -> Tuple[bool, Dict]:
        """
        匹配两个事件

        Args:
            poly_event: Polymarket 事件字典
            orbit_event: OrbitExch 事件字典
            config: 匹配配置

        Returns:
            (is_match, scores) - 是否匹配及匹配得分
        """
        # 阶段 1: Sport 匹配
        poly_sport = self._normalize_sport(poly_event.get('sport', ''))
        orbit_sport = self._normalize_sport(orbit_event.get('sport', ''))

        # 完全匹配或相似匹配
        if poly_sport == orbit_sport:
            sport_score = len(split_elements(poly_sport))
        else:
            sport_score = get_similar_for_matching(True, poly_sport, orbit_sport)

        if sport_score == 0:
            return False, {}

        # 阶段 2: Competition 匹配
        poly_comp = poly_event.get('competition', '')
        orbit_comp = orbit_event.get('competition', '')

        if poly_comp == orbit_comp:
            comp_score = len(split_elements(poly_comp))
        else:
            comp_score = get_similar_for_matching(True, poly_comp, orbit_comp)

        if comp_score == 0:
            return False, {}

        # 阶段 3: Team 匹配
        poly_home = poly_event.get('home_team', '')
        poly_away = poly_event.get('away_team', '')
        orbit_home = orbit_event.get('home_team', '')
        orbit_away = orbit_event.get('away_team', '')

        # 计算 home 和 away 的相似度
        sim_home = get_similar_for_matching(True, orbit_home, poly_home)
        sim_away = get_similar_for_matching(True, orbit_away, poly_away)

        team_swapped = False

        # 如果正常匹配失败，尝试交换主客队
        if sim_home == 0 or sim_away == 0:
            alt_sim_home = get_similar_for_matching(True, orbit_away, poly_home)
            alt_sim_away = get_similar_for_matching(True, orbit_home, poly_away)

            if alt_sim_home > 0 and alt_sim_away > 0:
                sim_home = alt_sim_home
                sim_away = alt_sim_away
                team_swapped = True
            else:
                return False, {}

        scores = {
            'sport': sport_score,
            'competition': comp_score,
            'home_team': sim_home,
            'away_team': sim_away,
            'team_swapped': team_swapped
        }

        return True, scores

    def match_events(self, poly_events: List[dict], orbit_events: List[dict]) -> List[MatchResult]:
        """匹配事件列表"""

        self._log.info(f'开始匹配: {len(poly_events)} Polymarket vs {len(orbit_events)} OrbitExch')

        matches = []
        matched_orbit_ids = set()

        # 跟踪已匹配的字段值 (防止重复匹配)
        matched_values = {
            'sports': set(),        # 已匹配的 orbit sport 值
            'competitions': set(),  # 已匹配的 orbit competition 值
            'teams': set()          # 已匹配的 orbit team 值
        }

        for poly_event in poly_events:
            best_match = self._find_best_match(poly_event, orbit_events, matched_orbit_ids, matched_values)

            if best_match:
                matches.append(best_match)
                orbit_event = best_match.orbitexch_event

                # 记录已匹配的事件 ID
                matched_orbit_ids.add(orbit_event['event_id'])

                # 记录已匹配的字段值
                matched_values['sports'].add(self._normalize_sport(orbit_event.get('sport', '')))
                matched_values['competitions'].add(orbit_event.get('competition', ''))
                matched_values['teams'].add(orbit_event.get('home_team', ''))
                matched_values['teams'].add(orbit_event.get('away_team', ''))

                self._log.debug(
                    f"匹配: {poly_event['event']} <-> {orbit_event['event']} "
                    f"(confidence: {best_match.confidence:.2f})"
                )

        self._log.info(f'匹配完成: {len(matches)} 个匹配')
        return matches

    def _find_best_match(
        self,
        poly_event: dict,
        orbit_events: List[dict],
        matched_ids: set,
        matched_values: dict = None
    ) -> Optional[MatchResult]:
        """查找最佳匹配"""

        matched_values = matched_values or {'sports': set(), 'competitions': set(), 'teams': set()}

        # 过滤已匹配的事件
        available = [e for e in orbit_events if e['event_id'] not in matched_ids]

        if not available:
            return None

        # 阶段 1: Sport 匹配
        sport_candidates = self._match_by_sport(poly_event, available, matched_values['sports'])

        if not sport_candidates:
            return None

        # 阶段 2: Competition 匹配 (可以返回多个)
        comp_candidates = self._match_by_competition(poly_event, sport_candidates, matched_values['competitions'])

        if not comp_candidates:
            return None

        # 阶段 3: Team 匹配 (只返回一个最佳)
        return self._select_best_team_match(poly_event, comp_candidates, matched_values['teams'])

    def _match_by_sport(
        self,
        poly_event: dict,
        candidates: List[dict],
        matched_sports: set = None
    ) -> List[dict]:
        """
        阶段 1: Sport 匹配

        规则:
        1. 过滤已匹配的 sport 值
        2. 先找完全相同的
        3. 找不到则用 get_similar_for_matching，只选一个最佳
        """

        matched_sports = matched_sports or set()
        poly_sport = self._normalize_sport(poly_event['sport'])

        # 过滤已匹配的 sport 值
        available = [
            e for e in candidates
            if self._normalize_sport(e['sport']) not in matched_sports
        ]

        if not available:
            return []

        # 1. 先找完全相同的
        exact_matches = [
            e for e in available
            if self._normalize_sport(e['sport']) == poly_sport
        ]

        if exact_matches:
            self._log.debug(f"Sport 完全匹配: {len(exact_matches)} 个候选")
            return exact_matches

        # 2. 用 get_similar_for_matching 找匹配的
        sport_candidates = []

        for orbit_event in available:
            orbit_sport = self._normalize_sport(orbit_event['sport'])
            score = get_similar_for_matching(True, poly_sport, orbit_sport)

            if score > 0:
                sport_candidates.append({
                    'event': orbit_event,
                    'sport_score': score
                })

        if sport_candidates:
            # 只选择分数最高的一个
            max_score = max(c['sport_score'] for c in sport_candidates)
            best_candidates = [c for c in sport_candidates if c['sport_score'] == max_score]

            self._log.debug(f"Sport get_similar 匹配: {len(best_candidates)} 个候选")
            return [c['event'] for c in best_candidates[:1]]  # 只返回一个

        return []

    def _match_by_competition(
        self,
        poly_event: dict,
        candidates: List[dict],
        matched_competitions: set = None
    ) -> List[dict]:
        """
        阶段 2: Competition 匹配

        规则:
        1. 过滤已匹配的 competition 值
        2. 先找完全相同的
        3. 找不到则用 get_similar_for_matching
        4. 当有多个匹配分数相同的最高分时，可以都选
        """

        matched_competitions = matched_competitions or set()
        poly_comp = poly_event['competition']

        # 过滤已匹配的 competition 值
        available = [
            e for e in candidates
            if e['competition'] not in matched_competitions
        ]

        if not available:
            return []

        # 1. 先找完全相同的
        exact_matches = [e for e in available if e['competition'] == poly_comp]

        if exact_matches:
            self._log.debug(f"Competition 完全匹配: {len(exact_matches)} 个候选")
            return exact_matches

        # 2. 用 get_similar_for_matching 找匹配的
        comp_candidates = []

        for orbit_event in available:
            score = get_similar_for_matching(True, poly_comp, orbit_event['competition'])

            if score > 0:
                comp_candidates.append({
                    'event': orbit_event,
                    'comp_score': score
                })

        if comp_candidates:
            # 选择分数最高的所有候选 (competition 可以多选)
            max_score = max(c['comp_score'] for c in comp_candidates)
            best_candidates = [c for c in comp_candidates if c['comp_score'] == max_score]

            self._log.debug(f"Competition get_similar 匹配: {len(best_candidates)} 个候选 (score={max_score})")
            return [c['event'] for c in best_candidates]  # 返回所有最高分

        return []

    def _select_best_team_match(
        self,
        poly_event: dict,
        candidates: List[dict],
        matched_teams: set = None
    ) -> Optional[MatchResult]:
        """
        阶段 3: Team 匹配

        规则:
        1. 过滤已匹配的队名
        2. 先找 home + away 都完全相同的
        3. 找不到则用 get_similar_for_matching
        4. if sim_home > 0 && sim_away > 0, 则匹配成功
        5. 选择 home + away 匹配分数之和最大的那个
        """

        matched_teams = matched_teams or set()
        poly_home = poly_event['home_team']
        poly_away = poly_event['away_team']

        # 过滤已匹配的队名 (主队或客队已被匹配过)
        available = [
            e for e in candidates
            if e['home_team'] not in matched_teams and e['away_team'] not in matched_teams
        ]

        if not available:
            return None

        # 1. 先找 home + away 都完全相同的
        exact_matches = [
            e for e in available
            if e['home_team'] == poly_home and e['away_team'] == poly_away
        ]

        if exact_matches:
            orbit_event = exact_matches[0]

            # 计算得分
            poly_sport = self._normalize_sport(poly_event['sport'])
            orbit_sport = self._normalize_sport(orbit_event['sport'])

            if poly_sport == orbit_sport:
                sport_score = len(split_elements(poly_sport))
            else:
                sport_score = get_similar_for_matching(True, poly_sport, orbit_sport)

            poly_comp = poly_event['competition']
            orbit_comp = orbit_event['competition']

            if poly_comp == orbit_comp:
                comp_score = len(split_elements(poly_comp))
            else:
                comp_score = get_similar_for_matching(True, poly_comp, orbit_comp)

            # 完全匹配的队名得分
            home_score = len(split_elements(poly_home))
            away_score = len(split_elements(poly_away))

            scores = {
                'sport': sport_score,
                'competition': comp_score,
                'home_team': home_score,
                'away_team': away_score,
                'team_swapped': False
            }

            confidence = self._calculate_confidence(scores)

            self._log.debug(f"Team 完全匹配")

            return MatchResult(
                polymarket_event=poly_event,
                orbitexch_event=orbit_event,
                match_scores=scores,
                confidence=confidence
            )

        # 2. 用 get_similar_for_matching 匹配
        team_candidates = []

        for orbit_event in available:
            sim_home = get_similar_for_matching(True, orbit_event['home_team'], poly_home)
            sim_away = get_similar_for_matching(True, orbit_event['away_team'], poly_away)

            team_swapped = False

            # 如果正常匹配失败，尝试交换
            if sim_home == 0 or sim_away == 0:
                alt_sim_home = get_similar_for_matching(True, orbit_event['away_team'], poly_home)
                alt_sim_away = get_similar_for_matching(True, orbit_event['home_team'], poly_away)

                if alt_sim_home > 0 and alt_sim_away > 0:
                    sim_home = alt_sim_home
                    sim_away = alt_sim_away
                    team_swapped = True

            # 规则: if sim_home > 0 && sim_away > 0, 则匹配成功
            if sim_home > 0 and sim_away > 0:
                total_team_score = sim_home + sim_away

                team_candidates.append({
                    'event': orbit_event,
                    'home_score': sim_home,
                    'away_score': sim_away,
                    'total_team_score': total_team_score,
                    'team_swapped': team_swapped
                })

        if not team_candidates:
            return None

        # 3. 选择 home + away 匹配分数之和最大的那个
        best_candidate = max(team_candidates, key=lambda x: x['total_team_score'])

        orbit_event = best_candidate['event']

        # 计算完整得分
        poly_sport = self._normalize_sport(poly_event['sport'])
        orbit_sport = self._normalize_sport(orbit_event['sport'])

        if poly_sport == orbit_sport:
            sport_score = len(split_elements(poly_sport))
        else:
            sport_score = get_similar_for_matching(True, poly_sport, orbit_sport)

        poly_comp = poly_event['competition']
        orbit_comp = orbit_event['competition']

        if poly_comp == orbit_comp:
            comp_score = len(split_elements(poly_comp))
        else:
            comp_score = get_similar_for_matching(True, poly_comp, orbit_comp)

        scores = {
            'sport': sport_score,
            'competition': comp_score,
            'home_team': best_candidate['home_score'],
            'away_team': best_candidate['away_score'],
            'team_swapped': best_candidate['team_swapped']
        }

        confidence = self._calculate_confidence(scores)

        self._log.debug(f"Team get_similar 匹配: {orbit_event['event']}")

        return MatchResult(
            polymarket_event=poly_event,
            orbitexch_event=orbit_event,
            match_scores=scores,
            confidence=confidence
        )

    def _calculate_confidence(self, scores: dict) -> float:
        """计算匹配置信度"""
        sport_score = scores.get('sport', 0)
        comp_score = scores.get('competition', 0)
        home_score = scores.get('home_team', 0)
        away_score = scores.get('away_team', 0)

        # 简单的置信度计算: 各项得分的加权平均
        total_score = sport_score + comp_score + home_score + away_score

        if total_score == 0:
            return 0.0

        # 假设满分为 10 (sport:2 + comp:3 + home:2.5 + away:2.5)
        max_score = 10.0
        confidence = min(total_score / max_score, 1.0)

        return round(confidence, 4)
