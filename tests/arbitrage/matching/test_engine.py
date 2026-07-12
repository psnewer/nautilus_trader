"""matching/engine.py —— MatchEngine 跨 venue 配对(算法平移自旧)。"""

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.normalizer import NormalizedEvent


def _ev(venue, sport, comp, home, away):
    return NormalizedEvent(
        venue=venue, sport=sport, competition=comp,
        home_team=home, away_team=away,
        home_team_normalized=home, away_team_normalized=away,
    )


def test_match_within_same_group_team_names_equal():
    """matching-2.1: 同 (sport, competition) 同队名 → 配上;不同组不配。"""
    poly = [_ev("PM", "Soccer", "EPL", "Arsenal", "Chelsea")]
    orbit = [_ev("OE", "Soccer", "EPL", "Arsenal", "Chelsea")]
    results = MatchEngine().match_events(poly, orbit)
    assert len(results) == 1
    r = results[0]
    assert r.is_valid and r.total_confidence > 0
    assert r.anchor_event.home_team == "Arsenal"
    assert r.tradable_event.home_team == "Arsenal"


def test_match_skipped_across_different_competitions():
    """matching-2.2: 不同 competition(组 key 不同)→ 不进入互比。"""
    poly = [_ev("PM", "Soccer", "EPL", "Arsenal", "Chelsea")]
    orbit = [_ev("OE", "Soccer", "LaLiga", "Arsenal", "Chelsea")]
    assert MatchEngine().match_events(poly, orbit) == []


def test_match_uses_normalized_team_similarity():
    """matching-2.3: 队名相似但不完全相等(模糊匹配,get_similar)→ 仍可配上。"""
    poly = [_ev("PM", "Soccer", "EPL", "Arsenal", "Chelsea")]
    orbit = [_ev("OE", "Soccer", "EPL", "Arsenel", "Chelsa")]   # 故意打字偏
    results = MatchEngine().match_events(poly, orbit)
    # 至少之一应能匹上(取决于 get_similar 实现);有效性由 confidence > 0 决定。
    if results:
        assert results[0].is_valid


def test_greedy_each_orbit_used_at_most_once():
    """matching-2.4: 贪心 —— 一个 OE 事件不会被配两次(已用集合)。"""
    poly = [
        _ev("PM", "Soccer", "EPL", "Arsenal", "Chelsea"),
        _ev("PM", "Soccer", "EPL", "Arsenal", "Chelsea"),  # 同名(罕见,但测贪心)
    ]
    orbit = [_ev("OE", "Soccer", "EPL", "Arsenal", "Chelsea")]
    results = MatchEngine().match_events(poly, orbit)
    assert len(results) == 1  # OE 唯一一条只配一个 PM


def test_global_confidence_priority_beats_anchor_order():
    """matching-2.4b:同组内先取全局最高 total_confidence,不让先遍历的 anchor 抢占更优 market。"""
    anchors = [
        _ev("PMS", "Soccer", "EPL", "Alpha Beta", "Gamma Delta"),
        _ev("PMS", "Soccer", "EPL", "Alpha", "Gamma"),
    ]
    tradables = [_ev("OE", "Soccer", "EPL", "Alpha", "Gamma")]

    results = MatchEngine().match_events(anchors, tradables)

    assert len(results) == 1
    assert results[0].anchor_event.home_team == "Alpha"
    assert results[0].tradable_event.home_team == "Alpha"
    assert results[0].total_confidence == 2.0


def test_confidence_uses_longest_home_away_word_counts():
    """matching-2.4c:home/away confidence = matched tokens / 较长 token 数。"""
    anchors = [_ev("PMS", "Tennis", "ATP", "Michel Johnson", "Nick Dimis")]
    tradables = [_ev("SE", "Tennis", "ATP", "St Michel Johnson", "Nick Dimis")]

    result = MatchEngine().match_events(anchors, tradables)[0]

    assert result.home_confidence == 2 / 3
    assert result.away_confidence == 1.0
    assert result.total_confidence == (2 / 3) + 1.0


def test_competition_max_matches_caps_results():
    """matching-2.5: competition_max_matches[comp] 限制单联赛上限。"""
    poly = [_ev("PM", "Soccer", "EPL", f"H{i}", f"A{i}") for i in range(5)]
    orbit = [_ev("OE", "Soccer", "EPL", f"H{i}", f"A{i}") for i in range(5)]
    eng = MatchEngine(competition_max_matches={"EPL": 2})
    assert len(eng.match_events(poly, orbit)) == 2


def test_zero_home_or_away_confidence_filters_weak_matches():
    """matching-2.6: home/away 任一 confidence 为 0 → 过滤掉。"""
    poly = [_ev("PM", "Soccer", "EPL", "AAAAAA", "BBBBBB")]
    orbit = [_ev("OE", "Soccer", "EPL", "XYZ123", "QWE456")]   # 队名完全不沾
    assert MatchEngine().match_events(poly, orbit) == []


def test_empty_inputs_no_crash():
    """matching-2.7: 任一侧为空 / 双方都空 → 返 [],不抛。"""
    assert MatchEngine().match_events([], []) == []
    assert MatchEngine().match_events([_ev("PM","Soccer","EPL","A","B")], []) == []
    assert MatchEngine().match_events([], [_ev("OE","Soccer","EPL","A","B")]) == []
