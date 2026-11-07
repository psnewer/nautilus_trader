"""Flashscore 网球 live feed 解析与 PMSPORTS anchor 映射。"""

from __future__ import annotations

from dataclasses import dataclass

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.normalizer import NormalizedEvent
from src.arbitrage.matching.normalizer import normalize_team_name


@dataclass(frozen=True, slots=True)
class FlashscoreEvent:
    event_id: str
    home_team: str
    away_team: str
    score: str
    first_serve: str | None
    raw: dict[str, str]


def parse_flashscore_feed(payload: str) -> list[FlashscoreEvent]:
    """解析 Flashscore 当日网球 feed，只返回正在进行的赛事。"""
    by_event_id: dict[str, FlashscoreEvent] = {}
    for record in payload.split("~AA÷")[1:]:
        parsed = _parse_event(record)
        if parsed is not None:
            by_event_id[parsed.event_id] = parsed
    return list(by_event_id.values())


def map_flashscore_events(
    anchor_events: list[NormalizedEvent],
    flashscore_events: list[FlashscoreEvent],
    engine: MatchEngine,
) -> dict[int, FlashscoreEvent]:
    """复用 PM/OE 的同向队名算法，将 PMS ``game_id`` 映射到 Flashscore event ID。"""
    anchors_by_group: dict[str, list[NormalizedEvent]] = {}
    for anchor in anchor_events:
        anchors_by_group.setdefault(anchor.group_key, []).append(anchor)

    mapped: dict[int, FlashscoreEvent] = {}
    used_event_ids: set[str] = set()
    for group_key in sorted(anchors_by_group):
        anchors = anchors_by_group[group_key]
        if not anchors:
            continue
        sport = anchors[0].sport
        competition = anchors[0].competition
        candidates = [
            NormalizedEvent(
                venue="FLASHSCORE",
                sport=sport,
                competition=competition,
                home_team=event.home_team,
                away_team=event.away_team,
                home_team_normalized=normalize_team_name(event.home_team),
                away_team_normalized=normalize_team_name(event.away_team),
                legs=[event],
            )
            for event in flashscore_events
            if event.event_id not in used_event_ids
        ]
        for result in engine.match_events(anchors, candidates, skip_cap=True):
            game_id = _anchor_game_id(result.anchor_event)
            event = result.tradable_event.legs[0]
            if game_id is None or not isinstance(event, FlashscoreEvent):
                continue
            mapped[game_id] = event
            used_event_ids.add(event.event_id)
    return mapped


def _parse_event(record: str) -> FlashscoreEvent | None:
    event_id, separator, remainder = record.partition("¬")
    if not separator or not event_id:
        return None
    fields = {"AA": event_id}
    for token in remainder.split("¬"):
        key, separator, value = token.partition("÷")
        if separator:
            fields[key] = value

    if fields.get("AB") != "2":
        return None
    home_team = fields.get("AE", "").strip()
    away_team = fields.get("AF", "").strip()
    if not home_team or not away_team:
        return None

    units = _score_units(fields)
    score = ", ".join(f"{home}-{away}" for home, away in units)
    current_serve = fields.get("WC")
    first_serve = None
    if units and units[-1] != (6, 6) and current_serve in {"1", "2"}:
        current_role = "home" if current_serve == "1" else "away"
        first_serve = (
            current_role
            if sum(home + away for home, away in units) % 2 == 0
            else _opposite(current_role)
        )
    return FlashscoreEvent(
        event_id=event_id,
        home_team=home_team,
        away_team=away_team,
        score=score,
        first_serve=first_serve,
        raw=fields,
    )


def _score_units(fields: dict[str, str]) -> tuple[tuple[int, int], ...]:
    units = []
    score_keys = (("BA", "BB"), ("BC", "BD"), ("BE", "BF"), ("BG", "BH"), ("BI", "BJ"))
    for home_key, away_key in score_keys:
        home = fields.get(home_key)
        away = fields.get(away_key)
        if home is None and away is None:
            continue
        if home is None or away is None:
            return ()
        try:
            units.append((int(home), int(away)))
        except ValueError:
            return ()
    return tuple(units)


def _anchor_game_id(anchor: NormalizedEvent) -> int | None:
    if not anchor.legs:
        return None
    info = getattr(anchor.legs[0], "info", None)
    try:
        return int(info.get("game_id")) if isinstance(info, dict) else None
    except (TypeError, ValueError):
        return None


def _opposite(role: str) -> str:
    return "away" if role == "home" else "home"
