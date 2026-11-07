"""Flashscore live feed 解析、映射与首局发球方测试。"""

from types import SimpleNamespace

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.flashscore import map_flashscore_events
from src.arbitrage.matching.flashscore import parse_flashscore_feed
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


def _event(event_id, home, away, *, status=2, serve=1, sets=((0, 0),)):
    fields = [
        f"~AA÷{event_id}",
        f"AB÷{status}",
        f"AE÷{home}",
        f"AF÷{away}",
        f"WC÷{serve}",
    ]
    for (home_key, away_key), (home_score, away_score) in zip(
        (("BA", "BB"), ("BC", "BD"), ("BE", "BF"), ("BG", "BH"), ("BI", "BJ")),
        sets,
        strict=False,
    ):
        fields.extend((f"{home_key}÷{home_score}", f"{away_key}÷{away_score}"))
    return "¬".join(fields)


def test_parser_reads_abbreviated_names_stable_event_id_and_score():
    payload = "SA÷2¬" + _event(
        "n9P7Yw5K",
        "Hontama M.",
        "Linette M.",
        serve=2,
        sets=((6, 4), (5, 5)),
    )

    event = parse_flashscore_feed(payload)[0]

    assert event.event_id == "n9P7Yw5K"
    assert event.home_team == "Hontama M."
    assert event.away_team == "Linette M."
    assert event.score == "6-4, 5-5"
    assert event.raw["WC"] == "2"


def test_parser_keeps_only_live_events():
    payload = "¬".join(
        (
            "SA÷2",
            _event("scheduled", "Gill F.", "Walters M.", status=1),
            _event("live", "Zaytseva K.", "Feistel G.", status=2),
            _event("finished", "Struff J-L.", "Gea A.", status=3),
        ),
    )

    events = parse_flashscore_feed(payload)

    assert [event.event_id for event in events] == ["live"]


def test_parser_keeps_doubles_abbreviations_without_player_profile_requests():
    payload = _event(
        "doubles",
        "Adams J./Szabo B.",
        "Bohrer Martins C./Burgic E.",
        sets=((6, 4), (6, 6)),
    )

    event = parse_flashscore_feed(payload)[0]

    assert event.home_team == "Adams J./Szabo B."
    assert event.away_team == "Bohrer Martins C./Burgic E."


def test_mapping_reuses_same_direction_team_similarity_for_abbreviations():
    anchors = [
        _anchor(1, "Felix Gill", "Marcus Walters"),
        _anchor(2, "Ksenia Zaytseva", "Gina Feistel"),
        _anchor(3, "Jan-Lennard Struff", "Arthur Gea"),
    ]
    payload = "¬".join(
        (
            _event("one", "Gill F.", "Walters M."),
            _event("two", "Zaytseva K.", "Feistel G."),
            _event("three", "Struff J-L.", "Gea A."),
        ),
    )

    mapped = map_flashscore_events(anchors, parse_flashscore_feed(payload), MatchEngine())

    assert {game_id: event.event_id for game_id, event in mapped.items()} == {
        1: "one",
        2: "two",
        3: "three",
    }


def test_mapping_does_not_try_home_away_swap():
    anchors = [_anchor(1, "Felix Gill", "Marcus Walters")]
    payload = _event("one", "Walters M.", "Gill F.")

    mapped = map_flashscore_events(anchors, parse_flashscore_feed(payload), MatchEngine())

    assert mapped == {}


def test_first_serve_is_inferred_from_current_server_and_completed_game_parity():
    payload = "¬".join(
        (
            _event("even", "A", "B", serve=2, sets=((6, 4), (3, 5))),
            _event("odd", "C", "D", serve=2, sets=((6, 4), (3, 4))),
        ),
    )

    events = {event.event_id: event for event in parse_flashscore_feed(payload)}

    assert events["even"].first_serve == "away"
    assert events["odd"].first_serve == "home"


def test_first_serve_is_not_inferred_during_active_tiebreak():
    payload = "¬".join(
        (
            _event("active", "A", "B", serve=2, sets=((6, 6),)),
            _event("completed", "C", "D", serve=1, sets=((7, 6), (0, 0))),
        ),
    )

    events = {event.event_id: event for event in parse_flashscore_feed(payload)}

    assert events["active"].first_serve is None
    assert events["completed"].first_serve == "away"
