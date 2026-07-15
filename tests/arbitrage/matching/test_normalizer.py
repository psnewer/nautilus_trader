"""matching/normalizer.py —— 队名预处理 + instrument 反推事件视图。"""

from types import SimpleNamespace

from src.arbitrage.matching.normalizer import NormalizedEvent
from src.arbitrage.matching.normalizer import events_from_instruments
from src.arbitrage.matching.normalizer import normalize_team_name


# ── normalize_team_name(平移自旧)──────────────────────────────────
def test_normalize_strips_slash_and_whitespace():
    """matching-1.team.1: '/' 去掉(多队员)+ 首尾空格去掉。"""
    assert normalize_team_name("Muhammad/Routliffe") == "MuhammadRoutliffe"
    assert normalize_team_name("  Chelsea  ") == "Chelsea"
    assert normalize_team_name(None) == ""
    assert normalize_team_name("") == ""


# ── events_from_instruments(从 NT instruments 反推)──────────────────
def _instrument(venue, sport, comp, home, away, role, iid):
    return SimpleNamespace(
        id=SimpleNamespace(venue=venue, __str__=lambda self=None: iid),
        info={
            "sport": sport, "competition": comp,
            "home_team": home, "away_team": away,
            "selection_role": role,
        },
    )


def test_events_from_instruments_groups_legs_per_event():
    """matching-1.event.1: 同 (venue, sport, competition, home, away) 多腿 → 一个 NormalizedEvent。"""
    legs = [
        _instrument("POLYMARKET", "Soccer", "EPL", "Arsenal", "Chelsea", "home", "A_h.PM"),
        _instrument("POLYMARKET", "Soccer", "EPL", "Arsenal", "Chelsea", "away", "A_a.PM"),
    ]
    events = events_from_instruments(legs)
    assert len(events) == 1
    e = events[0]
    assert e.venue == "POLYMARKET" and e.competition == "EPL"
    assert e.home_team == "Arsenal" and e.home_team_normalized == "Arsenal"
    assert len(e.legs) == 2


def test_events_from_instruments_different_matches_separate():
    """matching-1.event.2: 同 venue 两场不同比赛 → 两个事件(队名不同)。"""
    legs = [
        _instrument("POLYMARKET", "Soccer", "EPL", "Arsenal", "Chelsea", "home", "1"),
        _instrument("POLYMARKET", "Soccer", "EPL", "Liverpool", "City", "home", "2"),
    ]
    events = events_from_instruments(legs)
    assert len(events) == 2


def test_events_from_instruments_uses_registry_venue_from_instrument_id_suffix():
    """matching-1.event.3: 真实 InstrumentId 字符串后缀经 Venue Registry 解析为 venue id。"""
    leg = SimpleNamespace(
        id="123.SHARPEXCH",
        info={
            "sport": "Tennis", "competition": "Wimbledon",
            "home_team": "A", "away_team": "B",
            "selection_role": "home",
        },
    )

    events = events_from_instruments([leg])

    assert len(events) == 1
    assert events[0].venue == "SHARPEXCH"


def test_events_from_instruments_skips_missing_info():
    """matching-1.event.4: info 缺 matching 必需 key → 跳过该腿(防御 Provider 漏填)。"""
    legs = [
        _instrument("POLYMARKET", "Soccer", "EPL", "A", "B", "home", "1"),
        SimpleNamespace(id=SimpleNamespace(venue="POLYMARKET"), info=None),
        SimpleNamespace(id=SimpleNamespace(venue="POLYMARKET"), info={"sport": "Soccer"}),  # 4-key 缺
    ]
    events = events_from_instruments(legs)
    assert len(events) == 1


def test_events_from_instruments_skips_missing_venue():
    """matching-1.event.4b: venue 不来自 InstrumentId / id.venue 时跳过,不读 info['venue'] 兜底。"""
    leg = SimpleNamespace(
        id=SimpleNamespace(),
        info={
            "sport": "Tennis", "competition": "Wimbledon",
            "home_team": "A", "away_team": "B",
            "selection_role": "home",
            "venue": "SHARPEXCH",
        },
    )

    assert events_from_instruments([leg]) == []


def test_normalized_event_group_key_for_matching():
    """matching-1.event.5: group_key = "{sport}::{competition}",matching 分组用。"""
    ev = NormalizedEvent(
        venue="POLYMARKET", sport="Soccer", competition="EPL",
        home_team="A", away_team="B",
        home_team_normalized="A", away_team_normalized="B",
    )
    assert ev.group_key == "Soccer::EPL"
