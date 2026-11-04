"""LiveScore 网球事件解析与 PMSPORTS anchor 映射。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.arbitrage.matching.engine import MatchEngine
from src.arbitrage.matching.normalizer import NormalizedEvent
from src.arbitrage.matching.normalizer import normalize_team_name


@dataclass(frozen=True, slots=True)
class LiveScoreEvent:
    eid: str
    home_team: str
    away_team: str
    score: str
    first_serve: str | None
    raw: dict[str, Any]


def parse_livescore_payloads(payloads: list[dict[str, Any]]) -> list[LiveScoreEvent]:
    """把日期接口 payload 展平为去重后的网球事件。"""
    by_eid: dict[str, LiveScoreEvent] = {}
    for payload in payloads:
        for stage in payload.get("Stages") or []:
            for event in stage.get("Events") or []:
                parsed = _parse_event(event)
                if parsed is not None:
                    by_eid[parsed.eid] = parsed
    return list(by_eid.values())


def map_livescore_events(
    anchor_events: list[NormalizedEvent],
    livescore_events: list[LiveScoreEvent],
    engine: MatchEngine,
) -> dict[int, LiveScoreEvent]:
    """
    复用 PM/OE 的同向队名算法,将 PMS ``game_id`` 映射到 LiveScore EID。

    不尝试 home/away 互换。LiveScore 赛事名不参与分组,因为它与 Gamma 的命名并不一致;
    这里只把同一批候选放入 anchor 当前的 sport/competition 分组后调用既有 MatchEngine。
    """
    anchors_by_group: dict[str, list[NormalizedEvent]] = {}
    for anchor in anchor_events:
        anchors_by_group.setdefault(anchor.group_key, []).append(anchor)

    mapped: dict[int, LiveScoreEvent] = {}
    used_eids: set[str] = set()
    for group_key in sorted(anchors_by_group):
        anchors = anchors_by_group[group_key]
        if not anchors:
            continue
        sport = anchors[0].sport
        competition = anchors[0].competition
        candidates = [
            NormalizedEvent(
                venue="LIVESCORE",
                sport=sport,
                competition=competition,
                home_team=event.home_team,
                away_team=event.away_team,
                home_team_normalized=normalize_team_name(event.home_team),
                away_team_normalized=normalize_team_name(event.away_team),
                legs=[event],
            )
            for event in livescore_events
            if event.eid not in used_eids
        ]
        for result in engine.match_events(anchors, candidates, skip_cap=True):
            game_id = _anchor_game_id(result.anchor_event)
            event = result.tradable_event.legs[0]
            if game_id is None or not isinstance(event, LiveScoreEvent):
                continue
            mapped[game_id] = event
            used_eids.add(event.eid)
    return mapped


def _parse_event(event: dict[str, Any]) -> LiveScoreEvent | None:
    eid = str(event.get("Eid") or "")
    home_team = _team_name(event.get("T1"))
    away_team = _team_name(event.get("T2"))
    if not eid or not home_team or not away_team:
        return None

    units = _score_units(event)
    score = ", ".join(f"{home}-{away}" for home, away in units)
    current_serve = event.get("Esrv")
    first_serve = None
    if units and units[-1] != (6, 6) and current_serve in (1, 2):
        current_role = "home" if current_serve == 1 else "away"
        first_serve = current_role if sum(a + b for a, b in units) % 2 == 0 else _opposite(current_role)
    return LiveScoreEvent(
        eid=eid,
        home_team=home_team,
        away_team=away_team,
        score=score,
        first_serve=first_serve,
        raw=event,
    )


def _team_name(team: Any) -> str:
    if not isinstance(team, list):
        return ""
    names = [str(player.get("Nm") or "").strip() for player in team if isinstance(player, dict)]
    return " / ".join(name for name in names if name)


def _score_units(event: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    units = []
    for index in range(1, 6):
        home = event.get(f"Tr1S{index}")
        away = event.get(f"Tr2S{index}")
        if home is None and away is None:
            continue
        try:
            units.append((int(home), int(away)))
        except (TypeError, ValueError):
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
