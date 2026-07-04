"""SharpExch sport details 发现与解析。"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Iterable

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharpExchRunner:
    """SE market runner。"""

    selection_id: str
    runner_name: str
    role: str
    handicap: float = 0.0


@dataclass(frozen=True)
class SharpExchMarketEvent:
    """SE Match Odds market 归一事件。"""

    sport: str
    competition: str
    home_team: str
    away_team: str
    sport_id: str
    competition_id: str
    market_id: str
    start_ts: int
    runners: tuple[SharpExchRunner, ...]
    in_play: bool = False


@dataclass(frozen=True)
class SharpExchSportDetailsRequest:
    """SE `sport/details` 请求描述。"""

    method: str
    url: str
    params: dict[str, str]
    body: dict[str, str]
    target_competitions: tuple[str, ...]


SportDetailsProvider = Callable[[Any], dict | Awaitable[dict]]
JsonFetcher = Callable[[SharpExchSportDetailsRequest], dict | Awaitable[dict]]


_DEFAULT_SPORT_IDS = {
    "tennis": "2",
    "soccer": "1",
    "football": "1",
}
_MAX_SPORT_DETAILS_PAGES = 100


class SharpExchDiscoveryClient:
    """SharpExch 发现客户端。

    `sport_details_provider` 是可注入 fetcher,便于单测与 Playwright/API runtime 复用同一解析器。
    """

    def __init__(
        self,
        sport_details_provider: SportDetailsProvider | None = None,
        *,
        base_url: str = "https://portal.sharpxch.com",
        json_fetcher: JsonFetcher | None = None,
        target_competitions: Iterable[str] | None = None,
    ) -> None:
        self._sport_details_provider = sport_details_provider
        self._base_url = base_url.rstrip("/")
        self._json_fetcher = json_fetcher
        self._target_competitions = set(target_competitions or [])

    async def discover_events(self, sport_configs: Iterable[Any] | None = None) -> list[SharpExchMarketEvent]:
        if self._sport_details_provider is None and self._json_fetcher is None:
            _log.warning("SE DiscoveryClient: no provider or fetcher configured")
            return []
        events: list[SharpExchMarketEvent] = []
        configs = list(sport_configs or [None])
        for sport_config in configs:
            if self._sport_details_provider is not None:
                request = sport_details_request(self._base_url, sport_config)
                payload = self._sport_details_provider(sport_config)
                if inspect.isawaitable(payload):
                    payload = await payload
                events.extend(
                    events_from_sport_details(
                        payload,
                        target_competitions=self._target_competitions or request.target_competitions,
                    ),
                )
                continue
            events.extend(await self._discover_events_from_json_fetcher(sport_config))
        return events

    async def _discover_events_from_json_fetcher(self, sport_config: Any) -> list[SharpExchMarketEvent]:
        page = 0
        size = 20  # SE API max page size
        events: list[SharpExchMarketEvent] = []
        seen_market_ids: set[str] = set()
        while page < _MAX_SPORT_DETAILS_PAGES:
            request = sport_details_request(self._base_url, sport_config, page=page, size=size)
            payload = self._json_fetcher(request)  # type: ignore[misc]
            if inspect.isawaitable(payload):
                payload = await payload

            content = _market_content(payload)
            new_market_ids = {
                str(market.get("marketId") or "")
                for market in content
                if isinstance(market, dict) and market.get("marketId") is not None
            } - seen_market_ids
            if not content or (page > 0 and not new_market_ids):
                break
            seen_market_ids.update(new_market_ids)
            page_events = events_from_sport_details(
                payload,
                target_competitions=self._target_competitions or request.target_competitions,
            )
            events.extend(page_events)
            if len(content) < size or not _sport_details_has_next_page(payload, page=page, size=size):
                break
            page += 1
        return events


def sport_details_request(
    base_url: str,
    sport_config: Any,
    *,
    page: int = 0,
    size: int = 20,  # SE API max page size
) -> SharpExchSportDetailsRequest:
    """根据 `SportConfig` 形状构造 SE `sport/details` 请求。

    `sport_config.sport` 可以是名称(`"Tennis"`)或已知 SE sport id(`"2"`);名称按内置表映射。
    """

    sport = _sport_value(sport_config)
    sport_id = _sport_id(sport)
    return SharpExchSportDetailsRequest(
        method="POST",
        url=f"{base_url.rstrip('/')}/customer/api/sport/details",
        params={"page": str(max(0, int(page))), "size": str(max(1, int(size)))},
        body={
            "viewBy": "POPULARITY",
            "timeFilter": "ALL",
            "id": sport_id,
            "contextFilter": "EVENT_TYPE",
        },
        target_competitions=tuple(_competitions(sport_config)),
    )


def events_from_sport_details(
    payload: dict,
    *,
    target_competitions: Iterable[str] | None = None,
) -> list[SharpExchMarketEvent]:
    """解析 SE `/customer/api/sport/details` 响应。

    只保留 Match Odds market;目标 competition 非空时按 competition name 过滤。
    """

    content = _market_content(payload)
    targets = set(target_competitions or [])
    events: list[SharpExchMarketEvent] = []
    for market in content:
        if not _is_match_odds(market):
            continue
        competition = market.get("competition") or {}
        competition_name = str(competition.get("name") or "")
        if targets and competition_name not in targets:
            continue
        event = market.get("event") or {}
        home_team, away_team = _teams_from_event(event)
        runners = _parse_runners(market.get("runners") or [], home_team, away_team)
        if not runners:
            continue
        sport_info = market.get("eventType") or payload.get("sportInfo") or {}
        events.append(
            SharpExchMarketEvent(
                sport=str(sport_info.get("name") or ""),
                competition=competition_name,
                home_team=home_team,
                away_team=away_team,
                sport_id=str(sport_info.get("id") or market.get("eventTypeId") or ""),
                competition_id=str(competition.get("id") or market.get("competitionId") or ""),
                market_id=str(market.get("marketId") or ""),
                start_ts=_millis_to_nanos(market.get("marketStartTime") or event.get("openDate")),
                runners=tuple(runners),
                in_play=bool(market.get("inPlay", False)),
            ),
        )
    return events


def _market_content(payload: dict) -> list[dict]:
    catalogue = payload.get("marketCatalogueList") or {}
    content = catalogue.get("content") if isinstance(catalogue, dict) else None
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def _sport_details_has_next_page(payload: dict, *, page: int, size: int) -> bool:
    catalogue = payload.get("marketCatalogueList") or {}
    if not isinstance(catalogue, dict):
        return False
    if catalogue.get("last") is True:
        return False
    if catalogue.get("hasNext") is False or catalogue.get("has_next") is False:
        return False
    total_pages = catalogue.get("totalPages")
    if total_pages is not None:
        try:
            return page + 1 < int(total_pages)
        except (TypeError, ValueError):
            pass
    total_elements = catalogue.get("totalElements") or catalogue.get("total")
    if total_elements is not None:
        try:
            return (page + 1) * size < int(total_elements)
        except (TypeError, ValueError):
            pass
    return len(_market_content(payload)) >= size


def _sport_value(sport_config: Any) -> str:
    if sport_config is None:
        return "Tennis"
    if isinstance(sport_config, str):
        return sport_config
    return str(getattr(sport_config, "sport", "") or "Tennis")


def _sport_id(sport: str) -> str:
    value = str(sport).strip()
    if value.isdigit():
        return value
    return _DEFAULT_SPORT_IDS.get(value.lower(), value)


def _competitions(sport_config: Any) -> list[str]:
    if sport_config is None or isinstance(sport_config, str):
        return []
    competitions = getattr(sport_config, "competitions", [])
    return [str(item) for item in competitions if item]


def _is_match_odds(market: dict) -> bool:
    market_name = str(market.get("marketName") or "").upper().replace(" ", "_")
    market_type = str((market.get("description") or {}).get("marketType") or "").upper()
    return market_name in {"MATCH_ODDS", "MATCH ODDS"} or market_type == "MATCH_ODDS"


def _teams_from_event(event: dict) -> tuple[str, str]:
    home_team = str(event.get("homeTeam") or "")
    away_team = str(event.get("awayTeam") or "")
    if home_team and away_team:
        return home_team, away_team
    name = str(event.get("name") or "")
    parts = re.split(r"\s+v(?:s\.?)?\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return home_team, away_team


def _parse_runners(raw_runners: list, home_team: str, away_team: str) -> list[SharpExchRunner]:
    parsed: list[SharpExchRunner] = []
    used_roles: set[str] = set()
    normalized_home = _normalize_name(home_team)
    normalized_away = _normalize_name(away_team)
    for idx, runner in enumerate(raw_runners):
        if not isinstance(runner, dict):
            continue
        name = str(runner.get("runnerName") or runner.get("name") or "")
        role = _role_for_runner(name, normalized_home, normalized_away)
        if not role:
            role = _fallback_role(idx, len(raw_runners))
        if not role or role in used_roles:
            continue
        selection_id = runner.get("selectionId") or runner.get("selection_id") or runner.get("id")
        if selection_id is None:
            continue
        used_roles.add(role)
        parsed.append(
            SharpExchRunner(
                selection_id=str(selection_id),
                runner_name=name,
                role=role,
                handicap=float(runner.get("handicap") or 0.0),
            ),
        )
    return parsed


def _role_for_runner(name: str, normalized_home: str, normalized_away: str) -> str:
    normalized = _normalize_name(name)
    if normalized and normalized == normalized_home:
        return "home"
    if normalized and normalized == normalized_away:
        return "away"
    if normalized in {"draw", "x", "the draw"}:
        return "draw"
    return ""


def _fallback_role(idx: int, count: int) -> str:
    if count == 2:
        return ("home", "away")[idx] if idx < 2 else ""
    if count >= 3:
        return ("home", "draw", "away")[idx] if idx < 3 else ""
    return ""


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _millis_to_nanos(value: Any) -> int:
    try:
        return int(value) * 1_000_000
    except (TypeError, ValueError):
        return 0
