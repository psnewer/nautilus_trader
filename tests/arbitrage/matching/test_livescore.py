"""LiveScore EID 映射与第一局发球方解析。"""

from types import SimpleNamespace

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.livescore import map_livescore_events
from src.arbitrage.matching.livescore import parse_livescore_payloads
from src.arbitrage.matching.normalizer import NormalizedEvent


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


def _event(eid, home, away, *, serve=1, sets=((0, 0),)):
    event = {
        "Eid": str(eid),
        "T1": [{"Nm": home}],
        "T2": [{"Nm": away}],
        "Esrv": serve,
    }
    for index, (left, right) in enumerate(sets, 1):
        event[f"Tr1S{index}"] = str(left)
        event[f"Tr2S{index}"] = str(right)
    return event


def test_mapping_reuses_same_direction_team_similarity_for_name_variants():
    anchors = [
        _anchor(1, "Joaquin Aguilar", "Juan Pablo Ficovich"),
        _anchor(2, "Gustavo Heide", "Juan Carlos Prado"),
        _anchor(3, "Federico Aguilar", "Nicolas Kicker"),
        _anchor(4, "Lautaro Midon", "Tomas Barrios"),
    ]
    payload = {"Stages": [{"Events": [
        _event(101, "Joaquin Aguilar Cardozo", "Juan Pablo Ficovich"),
        _event(102, "Gustavo Heide", "Juan Carlos Prado Angelo"),
        _event(103, "Federico Aguilar Cardozo", "Nicolas Kicker"),
        _event(104, "Lautaro Midon", "Marcelo Barrios"),
    ]}]}

    mapped = map_livescore_events(anchors, parse_livescore_payloads([payload]), MatchEngine())

    assert {game_id: event.eid for game_id, event in mapped.items()} == {
        1: "101", 2: "102", 3: "103", 4: "104",
    }


def test_mapping_does_not_try_home_away_swap():
    anchors = [_anchor(1, "Felix Gill", "Marcus Walters")]
    payload = {"Stages": [{"Events": [
        _event(101, "Marcus Walters", "Felix Gill"),
    ]}]}

    mapped = map_livescore_events(anchors, parse_livescore_payloads([payload]), MatchEngine())

    assert mapped == {}


def test_first_serve_is_inferred_from_current_server_and_completed_game_parity():
    payload = {"Stages": [{"Events": [
        _event(101, "A", "B", serve=2, sets=((6, 4), (3, 5))),  # 18 局:第一局同为 away
        _event(102, "C", "D", serve=2, sets=((6, 4), (3, 4))),  # 17 局:第一局为 home
    ]}]}

    events = {event.eid: event for event in parse_livescore_payloads([payload])}

    assert events["101"].score == "6-4, 3-5"
    assert events["101"].first_serve == "away"
    assert events["102"].first_serve == "home"


def test_first_serve_is_not_inferred_during_active_tiebreak():
    payload = {"Stages": [{"Events": [
        _event(101, "A", "B", serve=2, sets=((6, 6),)),
        _event(102, "C", "D", serve=1, sets=((7, 6), (0, 0))),
    ]}]}

    events = {event.eid: event for event in parse_livescore_payloads([payload])}

    assert events["101"].first_serve is None
    assert events["102"].first_serve == "away"
