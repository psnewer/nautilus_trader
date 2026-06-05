"""
MatchEngine —— 跨 venue 队名相似度匹配(平移自旧 `services/market_matching/engine.py`)。

算法逻辑原样保留(P2 领域 IP 保留):
1. 按 `(sport, competition)` 完全相等分组
2. 组内对每个 PM 事件,在 OE 候选中找最佳队名相似度匹配(`get_similar`)
3. 贪心:每个 OE 事件最多被匹配一次
4. `min_similarity` 阈值过滤;`competition_max_matches[comp]` 单联赛上限

**输入改为 `NormalizedEvent[]`**(matching/normalizer.py,从 NT instruments 反推)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from src.arbitrage.common.utils import get_similar
from src.arbitrage.matching.normalizer import NormalizedEvent


@dataclass
class MatchResult:
    """单次 PM↔OE 配对的详细信息(供 MatchedPair 生成 + 调试)。"""

    polymarket_event: NormalizedEvent
    orbitexch_event: NormalizedEvent
    home_similarity: int
    away_similarity: int
    home_matched_chars: int
    away_matched_chars: int
    total_similarity: int
    total_matched_chars: int

    @property
    def is_valid(self) -> bool:
        return self.home_similarity > 0 and self.away_similarity > 0


class MatchEngine:
    def __init__(
        self,
        min_similarity: int = 1,
        competition_max_matches: dict[str, int] | None = None,
    ) -> None:
        self._min_similarity = min_similarity
        self._competition_max_matches = competition_max_matches or {}

    # ── public ────────────────────────────────────────────────────────
    def match_events(
        self,
        poly_events: list[NormalizedEvent],
        orbit_events: list[NormalizedEvent],
    ) -> list[MatchResult]:
        """全量匹配:按 group_key 分组 → 组内匹配 → 合并所有 results。"""
        poly_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        orbit_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        for ev in poly_events:
            poly_groups[ev.group_key].append(ev)
        for ev in orbit_events:
            orbit_groups[ev.group_key].append(ev)

        all_results: list[MatchResult] = []
        for key in set(poly_groups.keys()) & set(orbit_groups.keys()):
            all_results.extend(self._match_within_group(poly_groups[key], orbit_groups[key]))
        return all_results

    # ── internal ──────────────────────────────────────────────────────
    def _match_within_group(
        self,
        poly_events: list[NormalizedEvent],
        orbit_events: list[NormalizedEvent],
    ) -> list[MatchResult]:
        if not poly_events or not orbit_events:
            return []

        results: list[MatchResult] = []
        used_orbit: set[int] = set()
        competition = poly_events[0].competition
        cap = self._competition_max_matches.get(competition)

        for poly in poly_events:
            if cap is not None and len(results) >= cap:
                break
            candidates: list[tuple[int, MatchResult]] = []
            for oi, orbit in enumerate(orbit_events):
                if oi in used_orbit:
                    continue
                result = self._pair(poly, orbit)
                if result.is_valid and result.total_similarity >= self._min_similarity:
                    candidates.append((oi, result))
            if not candidates:
                continue
            # 先 total_similarity 降序,再 total_matched_chars 降序(平移自旧 _select_best_match)
            candidates.sort(key=lambda x: (x[1].total_similarity, x[1].total_matched_chars), reverse=True)
            best_oi, best_result = candidates[0]
            results.append(best_result)
            used_orbit.add(best_oi)
        return results

    @staticmethod
    def _pair(poly: NormalizedEvent, orbit: NormalizedEvent) -> MatchResult:
        home_sim, home_chars = _team_similarity(poly.home_team_normalized, orbit.home_team_normalized)
        away_sim, away_chars = _team_similarity(poly.away_team_normalized, orbit.away_team_normalized)
        return MatchResult(
            polymarket_event=poly,
            orbitexch_event=orbit,
            home_similarity=home_sim,
            away_similarity=away_sim,
            home_matched_chars=home_chars,
            away_matched_chars=away_chars,
            total_similarity=home_sim + away_sim,
            total_matched_chars=home_chars + away_chars,
        )


def _team_similarity(t1: str, t2: str) -> tuple[int, int]:
    """get_similar(shorten=False) + 匹配字符数估计(平移自旧 _calculate_team_similarity)。"""
    similarity = get_similar(False, t1, t2)
    matched_chars = min(len(t1), len(t2)) if similarity > 0 else 0
    return similarity, matched_chars
