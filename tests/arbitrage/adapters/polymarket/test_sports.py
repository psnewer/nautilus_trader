"""PM Sports 比分信号(#60)—— `parse_sport_result` 纯映射 + `SportsGameUpdate` roundtrip。

WS 连接(`PolymarketSportsDataClient._run_ws`)经 /live-test 验(公开 firehose)。
样本取自本会话实采(wnba / atp ended)。
"""

from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.adapters.polymarket.sports import parse_sport_result


def test_parse_live_sample():
    """实采 wnba 进行中样本 → 字段正确,live True/ended False。"""
    d = {"gameId": 13002300, "leagueAbbreviation": "wnba", "homeTeam": "MIN", "awayTeam": "GSV",
         "status": "InProgress", "score": "66-65", "elapsed": "", "period": "End Q3",
         "live": True, "ended": False}
    u = parse_sport_result(d, ts=123)
    assert u.game_id == 13002300 and u.league == "wnba"
    assert u.home_team == "MIN" and u.away_team == "GSV"
    assert u.live is True and u.ended is False
    assert u.ts_event == 123 and u.ts_init == 123
    assert u.finished_ts == ""


def test_parse_ended_sample():
    """ended → ended True + finished_ts 填(仅 ended 时有)。"""
    d = {"gameId": 5630312, "leagueAbbreviation": "atp", "homeTeam": "Norrie", "awayTeam": "Navone",
         "live": False, "ended": True, "finished_timestamp": "2026-05-20T18:00:00Z"}
    u = parse_sport_result(d, ts=9)
    assert u.ended is True and u.finished_ts == "2026-05-20T18:00:00Z"


def test_parse_missing_game_id_returns_none():
    assert parse_sport_result({"leagueAbbreviation": "x"}, ts=1) is None


def test_roundtrip_to_from_dict():
    d = {"gameId": 7, "leagueAbbreviation": "fif", "homeTeam": "Mexico", "awayTeam": "Serbia",
         "status": "InProgress", "score": "1-0", "elapsed": "80:00", "period": "2H",
         "live": True, "ended": False}
    u = parse_sport_result(d, ts=5)
    u2 = SportsGameUpdate.from_dict(u.to_dict())
    assert u2.game_id == 7 and u2.league == "fif" and u2.home_team == "Mexico"
    assert u2.period == "2H" and u2.live is True and u2.ended is False
