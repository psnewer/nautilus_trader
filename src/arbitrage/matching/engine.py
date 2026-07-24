"""
MatchEngine —— 跨 venue 队名相似度匹配。

算法逻辑原样保留(P2 领域 IP 保留):
1. 按 `(sport, competition)` 完全相等分组
2. 组内计算所有 anchor×tradable 候选的队名 confidence(`get_similar` 命中数 / 较长 token 数)
3. 全候选按 total_confidence 贪心:每个 anchor/tradable 事件最多被匹配一次
4. home/away/total confidence 均 > 0 才有效;`competition_max_matches[comp]` 单联赛上限

**输入改为 `NormalizedEvent[]`**(matching/normalizer.py,从 NT instruments 反推)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

from src.arbitrage.common.utils import get_similar
from src.arbitrage.matching.normalizer import NormalizedEvent


@dataclass
class MatchResult:
    """单次 anchor↔tradable 配对的详细信息(供 MatchedPair 生成 + 调试)。"""

    anchor_event: NormalizedEvent
    tradable_event: NormalizedEvent
    home_confidence: float
    away_confidence: float
    home_matched_chars: int
    away_matched_chars: int
    total_confidence: float
    total_matched_chars: int

    @property
    def is_valid(self) -> bool:
        return self.home_confidence > 0 and self.away_confidence > 0 and self.total_confidence > 0


class MatchEngine:
    def __init__(
        self,
        competition_max_matches: dict[str, int] | None = None,
    ) -> None:
        self._competition_max_matches = competition_max_matches or {}

    # ── public ────────────────────────────────────────────────────────
    def match_events(
        self,
        anchor_events: list[NormalizedEvent],
        tradable_events: list[NormalizedEvent],
        *,
        skip_cap: bool = False,
    ) -> list[MatchResult]:
        """全量匹配:按 group_key 分组 → 组内匹配 → 合并所有 results。

        Args:
            skip_cap: 跳过 competition_max_matches 限制(供 non-tradable anchor 聚合路径使用,
                      cap 改在 actor 聚合后应用)。
        """
        anchor_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        tradable_groups: dict[str, list[NormalizedEvent]] = defaultdict(list)
        for ev in anchor_events:
            anchor_groups[ev.group_key].append(ev)
        for ev in tradable_events:
            tradable_groups[ev.group_key].append(ev)

        all_results: list[MatchResult] = []
        for key in set(anchor_groups.keys()) & set(tradable_groups.keys()):
            all_results.extend(self._match_within_group(anchor_groups[key], tradable_groups[key], skip_cap=skip_cap))
        return all_results

    # ── internal ──────────────────────────────────────────────────────
    def _match_within_group(
        self,
        anchor_events: list[NormalizedEvent],
        tradable_events: list[NormalizedEvent],
        *,
        skip_cap: bool = False,
    ) -> list[MatchResult]:
        if not anchor_events or not tradable_events:
            return []

        results: list[MatchResult] = []
        competition = anchor_events[0].competition
        cap = None if skip_cap else self._competition_max_matches.get(competition)

        candidates: list[tuple[int, int, MatchResult]] = []
        for ai, anchor in enumerate(anchor_events):
            for ti, tradable in enumerate(tradable_events):
                result = self._pair(anchor, tradable)
                if result.is_valid:
                    candidates.append((ai, ti, result))
        candidates.sort(key=lambda x: (x[2].total_confidence, x[2].total_matched_chars), reverse=True)

        used_anchor: set[int] = set()
        used_tradable: set[int] = set()
        for ai, ti, result in candidates:
            if cap is not None and len(results) >= cap:
                break
            if ai in used_anchor or ti in used_tradable:
                continue
            results.append(result)
            used_anchor.add(ai)
            used_tradable.add(ti)
        return results

    @staticmethod
    def _pair(anchor: NormalizedEvent, tradable: NormalizedEvent) -> MatchResult:
        home_conf, home_chars = _team_confidence(anchor.home_team_normalized, tradable.home_team_normalized)
        away_conf, away_chars = _team_confidence(anchor.away_team_normalized, tradable.away_team_normalized)
        return MatchResult(
            anchor_event=anchor,
            tradable_event=tradable,
            home_confidence=home_conf,
            away_confidence=away_conf,
            home_matched_chars=home_chars,
            away_matched_chars=away_chars,
            total_confidence=home_conf + away_conf,
            total_matched_chars=home_chars + away_chars,
        )


def _team_confidence(t1: str, t2: str) -> tuple[float, int]:
    """返回归一化 confidence + 匹配字符数估计。

    confidence = get_similar 命中 token 数 / 两侧拆分后较长 token 数。
    """
    matched_tokens = get_similar(False, t1, t2)
    denominator = max(_token_count(t1), _token_count(t2))
    confidence = (matched_tokens / denominator) if denominator > 0 else 0.0
    matched_chars = min(len(t1), len(t2)) if confidence > 0 else 0
    return confidence, matched_chars


def _token_count(value: str) -> int:
    return len([elem for elem in re.split(r"[^a-zA-Z0-9]", value) if elem])
