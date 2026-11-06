"""SofaScore event ID 映射与第一局发球方解析。"""

from types import SimpleNamespace

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.normalizer import NormalizedEvent
from src.arbitrage.matching.sofascore import map_sofascore_events
from src.arbitrage.matching.sofascore import parse_sofascore_payload


def _anchor(game_id, home, away):
    return NormalizedEvent(
        venue="PMSPORTS",
        sport="Tennis",
        competition="Tennis",
        home_team=home,
        away_team=away,
        home_team_normalized=home,
        away_team_normalized=away,
        legs=[SimpleNamespace(info={"game_id": game_id})],
    )


def _event(event_id, home, away, *, serve=1, sets=((0, 0),)):
    event = {
        "id": event_id,
        "homeTeam": {"id": event_id * 10, "name": home, "subTeams": []},
        "awayTeam": {"id": event_id * 10 + 1, "name": away, "subTeams": []},
        "firstToServe": serve,
        "homeScore": {},
        "awayScore": {},
    }
    for index, (left, right) in enumerate(sets, 1):
        event["homeScore"][f"period{index}"] = left
        event["awayScore"][f"period{index}"] = right
    return event


def test_parser_reads_full_names_stable_event_id_and_score():
    payload = {
        "events": [
            _event(101, "Mai Hontama", "Magda Linette", serve=2, sets=((6, 4), (5, 5))),
        ],
    }

    event = parse_sofascore_payload(payload)[0]

    assert event.event_id == "101"
    assert event.home_team == "Mai Hontama"
    assert event.away_team == "Magda Linette"
    assert event.score == "6-4, 5-5"
    assert event.raw["homeTeam"]["id"] == 1010


def test_parser_uses_full_subteam_names_for_doubles():
    event = _event(101, "Miedler L / Oberleitner N", "Nouza P / Venus M")
    event["homeTeam"]["subTeams"] = [{"name": "Lucas Miedler"}, {"name": "Neil Oberleitner"}]
    event["awayTeam"]["subTeams"] = [{"name": "Petr Nouza"}, {"name": "Michael Venus"}]

    parsed = parse_sofascore_payload({"events": [event]})[0]

    assert parsed.home_team == "Lucas Miedler / Neil Oberleitner"
    assert parsed.away_team == "Petr Nouza / Michael Venus"


def test_mapping_reuses_same_direction_team_similarity_for_name_variants():
    anchors = [
        _anchor(1, "Joaquin Aguilar", "Juan Pablo Ficovich"),
        _anchor(2, "Gustavo Heide", "Juan Carlos Prado"),
        _anchor(3, "Federico Aguilar", "Nicolas Kicker"),
        _anchor(4, "Lautaro Midon", "Tomas Barrios"),
    ]
    payload = {"events": [
        _event(101, "Joaquin Aguilar Cardozo", "Juan Pablo Ficovich"),
        _event(102, "Gustavo Heide", "Juan Carlos Prado Angelo"),
        _event(103, "Federico Aguilar Cardozo", "Nicolas Kicker"),
        _event(104, "Lautaro Midon", "Marcelo Barrios"),
    ]}

    mapped = map_sofascore_events(anchors, parse_sofascore_payload(payload), MatchEngine())

    assert {game_id: event.event_id for game_id, event in mapped.items()} == {
        1: "101", 2: "102", 3: "103", 4: "104",
    }


def test_mapping_does_not_try_home_away_swap():
    anchors = [_anchor(1, "Felix Gill", "Marcus Walters")]
    payload = {"events": [_event(101, "Marcus Walters", "Felix Gill")]}

    mapped = map_sofascore_events(anchors, parse_sofascore_payload(payload), MatchEngine())

    assert mapped == {}


def test_first_serve_is_inferred_from_current_server_and_completed_game_parity():
    payload = {"events": [
        _event(101, "A", "B", serve=2, sets=((6, 4), (3, 5))),  # 18 局:第一局同为 away
        _event(102, "C", "D", serve=2, sets=((6, 4), (3, 4))),  # 17 局:第一局为 home
    ]}

    events = {event.event_id: event for event in parse_sofascore_payload(payload)}

    assert events["101"].first_serve == "away"
    assert events["102"].first_serve == "home"


def test_first_serve_is_not_inferred_during_active_tiebreak():
    payload = {"events": [
        _event(101, "A", "B", serve=2, sets=((6, 6),)),
        _event(102, "C", "D", serve=1, sets=((7, 6), (0, 0))),
    ]}

    events = {event.event_id: event for event in parse_sofascore_payload(payload)}

    assert events["101"].first_serve is None
    assert events["102"].first_serve == "away"
