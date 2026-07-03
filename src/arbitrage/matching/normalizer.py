"""
Matching 归一:从 NT instruments(每条腿一个 instrument)反推每 venue 的事件视图。

NormalizedEvent 形态平移自旧 `services/market_matching/normalizer.py`,但
**输入改为 NT instruments**(读 instrument.info 6-key,Q9)。

`normalize_team_name`:队名预处理(去 / 去空格),平移自旧 `EventNormalizer.normalize_team_name`,
独立成模块函数(无需 venue-aware 配置对象,sport/competition 别名由 Provider 在填 info 时已应用)。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from src.arbitrage.common.venues import venue_id_from_instrument_id


def normalize_team_name(team_name: str | None) -> str:
    """队名预处理(平移自旧 EventNormalizer.normalize_team_name)。

    规则:
    - 去 '/' 符号(多队员场景:'Muhammad/Routliffe' → 'MuhammadRoutliffe')
    - 去首尾空格
    """
    if not team_name:
        return ""
    return team_name.replace("/", "").strip()


@dataclass
class NormalizedEvent:
    """从同 venue 的多腿(home/draw/away)反推的事件视图(matching 输入)。"""

    venue: str                          # 真实 venue id,如 "POLYMARKET" / "ORBITEXCH" / "SHARPEXCH"
    sport: str                          # 标准化 sport(Provider 填 info 时已 alias)
    competition: str                    # 联赛名(league;非 pair_id,#34)
    home_team: str                      # 原始队名
    away_team: str
    home_team_normalized: str           # 比对用预处理后队名
    away_team_normalized: str
    legs: list = field(default_factory=list)  # 该事件下所有 NT instrument(各 selection_role 一条)

    @property
    def group_key(self) -> str:
        """matching 分组键:同 (sport, competition) 才进入互比。"""
        return f"{self.sport}::{self.competition}"


def events_from_instruments(instruments) -> list[NormalizedEvent]:
    """instrument 列表 → NormalizedEvent 列表(同 (sport, competition, home_team, away_team) 聚为一事件)。

    Q9 契约:`instrument.info` 必含 6-key(sport/competition/home_team/away_team/start_ts/selection_role);
    缺 key 的 instrument 跳过(防御 Provider 漏填)。
    """
    by_event: dict[tuple, NormalizedEvent] = {}
    for instrument in instruments:
        info = getattr(instrument, "info", None)
        if not info:
            continue
        sport = info.get("sport")
        competition = info.get("competition")
        home_team = info.get("home_team")
        away_team = info.get("away_team")
        if not (sport and competition and home_team and away_team):
            continue  # info 不全的 instrument 不参与匹配
        venue = _venue_of(instrument)
        if not venue:
            continue
        key = (venue, sport, competition, home_team, away_team)
        ev = by_event.get(key)
        if ev is None:
            ev = NormalizedEvent(
                venue=venue,
                sport=sport,
                competition=competition,
                home_team=home_team,
                away_team=away_team,
                home_team_normalized=normalize_team_name(home_team),
                away_team_normalized=normalize_team_name(away_team),
            )
            by_event[key] = ev
        ev.legs.append(instrument)
    return list(by_event.values())


def _venue_of(instrument) -> str:
    instrument_id = getattr(instrument, "id", None)
    venue = venue_id_from_instrument_id(instrument_id) if instrument_id is not None else ""
    if venue:
        return venue

    raw_venue = getattr(instrument_id, "venue", None)
    if raw_venue:
        return str(raw_venue).upper()
    return ""
