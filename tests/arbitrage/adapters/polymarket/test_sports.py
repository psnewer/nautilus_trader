"""PM Sports 比分信号(#60)—— `parse_sport_result` 纯映射 + `SportsGameUpdate` roundtrip。

WS 连接(`PolymarketSportsDataClient._run_ws`)经 /live-test 验(公开 firehose)。
样本取自本会话实采(wnba / atp ended)。
"""

from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.polymarket import arb_factories as pm_factories
from nautilus_trader.adapters.polymarket.sports import PolymarketSportsInstrumentProvider
from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
from nautilus_trader.adapters.polymarket.sports import parse_sport_result
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.common.venues import SPORTS_CLIENT


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


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


def test_sports_provider_builds_non_tradable_anchor_instrument():
    provider = PolymarketSportsInstrumentProvider()
    event = {
        "id": "2701920",
        "gameId": 5843495,
        "title": "Wimbledon ATP: Rafael Jodar vs Felix Gill",
        "startDate": "2026-06-29T10:05:00Z",
        "teams": [
            {"name": "Rafael Jodar", "abbreviation": "jodar", "ordering": "home"},
            {"name": "Felix Gill", "abbreviation": "gill", "ordering": "away"},
        ],
    }

    assert provider._process_event(event, "ATP", "Tennis") == 1
    instruments = list(provider.get_all().values())

    assert len(instruments) == 1
    inst = instruments[0]
    assert str(inst.id.venue) == "PMSPORTS"
    assert inst.info["tradable"] is False
    assert inst.info["anchor"] is True
    assert inst.info["game_id"] == 5843495
    assert inst.info["competition"] == "ATP"
    assert inst.info["home_team"] == "Rafael Jodar"
    assert inst.info["away_team"] == "Felix Gill"


def test_sports_factory_uses_data_source_context(monkeypatch):
    """PMSPORTS factory 从 data-source keyed map 读取目标赛事。"""
    client = MagicMock(name="sports_dc")
    monkeypatch.setattr(pm_factories, "PolymarketSportsDataClient", client)
    bootstrap.prepare_arb_context(
        target_competitions_by_data_source={SPORTS_CLIENT: ["atp"]},
        competition_to_sport_by_data_source={SPORTS_CLIENT: {"atp": "Tennis"}},
        competition_aliases_by_venue={POLYMARKET: {"atp": "ATP"}},
    )

    pm_factories.PolymarketSportsLiveDataClientFactory.create(
        loop=MagicMock(),
        name="PMSPORTS",
        config=MagicMock(),
        msgbus=MagicMock(),
        cache=MagicMock(),
        clock=MagicMock(),
    )

    provider = client.call_args.kwargs["instrument_provider"]
    assert provider._target_competitions == {"atp"}
    assert provider._competition_to_sport == {"atp": "Tennis"}
    assert provider._competition_aliases == {"atp": "ATP"}
