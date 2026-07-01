"""SharpExch discovery parser 测试。

第一阶段只验证 SE sport/details payload → `SharpExchMarketEvent` 的纯解析。
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from nautilus_trader.adapters.sharpexch.discovery_client import SharpExchDiscoveryClient
from nautilus_trader.adapters.sharpexch.discovery_client import events_from_sport_details
from nautilus_trader.adapters.sharpexch.discovery_client import sport_details_request


def _payload() -> dict:
    return {
        "sportInfo": {"id": "2", "name": "Tennis"},
        "marketCatalogueList": {
            "content": [
                {
                    "marketId": "1.259502313",
                    "marketName": "Match Odds",
                    "marketStartTime": 1782768600000,
                    "eventType": {"id": "2", "name": "Tennis"},
                    "competition": {"id": "12597512", "name": "Men's Wimbledon 2026"},
                    "event": {
                        "id": "5843495",
                        "name": "Rafael Jodar v Felix Gill",
                        "homeTeam": "Rafael Jodar",
                        "awayTeam": "Felix Gill",
                    },
                    "runners": [
                        {"selectionId": 111, "runnerName": "Rafael Jodar"},
                        {"selectionId": 222, "runnerName": "Felix Gill"},
                    ],
                    "inPlay": False,
                },
                {
                    "marketId": "1.111",
                    "marketName": "Correct Score",
                    "competition": {"id": "12597512", "name": "Men's Wimbledon 2026"},
                    "event": {"name": "A v B"},
                    "runners": [],
                },
            ],
        },
    }


def _payload_with_market(market_id: str, *, competition: str = "Men's Wimbledon 2026") -> dict:
    payload = _payload()
    market = payload["marketCatalogueList"]["content"][0]
    market["marketId"] = market_id
    market["competition"] = {"id": "12597512", "name": competition}
    payload["marketCatalogueList"]["content"] = [market]
    return payload


def test_events_from_sport_details_parses_two_way_match_odds():
    events = events_from_sport_details(_payload())
    assert len(events) == 1
    event = events[0]
    assert event.sport == "Tennis"
    assert event.sport_id == "2"
    assert event.competition == "Men's Wimbledon 2026"
    assert event.competition_id == "12597512"
    assert event.market_id == "1.259502313"
    assert event.home_team == "Rafael Jodar"
    assert event.away_team == "Felix Gill"
    assert event.start_ts == 1782768600000 * 1_000_000
    assert [runner.role for runner in event.runners] == ["home", "away"]
    assert [runner.selection_id for runner in event.runners] == ["111", "222"]


def test_events_from_sport_details_filters_competition():
    assert events_from_sport_details(_payload(), target_competitions=["ATP"]) == []
    assert len(events_from_sport_details(_payload(), target_competitions=["Men's Wimbledon 2026"])) == 1


def test_events_from_sport_details_parses_draw_runner_by_name():
    payload = _payload()
    market = payload["marketCatalogueList"]["content"][0]
    market["eventType"] = {"id": "1", "name": "Soccer"}
    market["competition"] = {"id": "9", "name": "EPL"}
    market["event"] = {"name": "Arsenal v Chelsea"}
    market["runners"] = [
        {"selectionId": 1, "runnerName": "Arsenal"},
        {"selectionId": 2, "runnerName": "The Draw"},
        {"selectionId": 3, "runnerName": "Chelsea"},
    ]
    event = events_from_sport_details(payload)[0]
    assert (event.home_team, event.away_team) == ("Arsenal", "Chelsea")
    assert [runner.role for runner in event.runners] == ["home", "draw", "away"]


def test_discovery_client_uses_injected_provider():
    async def _run():
        client = SharpExchDiscoveryClient(lambda _: _payload(), target_competitions=["Men's Wimbledon 2026"])
        return await client.discover_events()

    events = asyncio.run(_run())
    assert len(events) == 1
    assert events[0].market_id == "1.259502313"


def test_sport_details_request_maps_tennis_to_wimbledon_api_body():
    request = sport_details_request(
        "https://portal.sharpxch.com/",
        SimpleNamespace(sport="Tennis", competitions=["Men's Wimbledon 2026"]),
    )
    assert request.method == "POST"
    assert request.url == "https://portal.sharpxch.com/customer/api/sport/details"
    assert request.params == {"page": "0", "size": "60"}
    assert request.body == {
        "viewBy": "POPULARITY",
        "timeFilter": "ALL",
        "id": "2",
        "contextFilter": "EVENT_TYPE",
    }
    assert request.target_competitions == ("Men's Wimbledon 2026",)


def test_sport_details_request_accepts_page_and_size():
    request = sport_details_request(
        "https://portal.sharpxch.com/",
        SimpleNamespace(sport="Tennis", competitions=[]),
        page=2,
        size=100,
    )

    assert request.params == {"page": "2", "size": "100"}


def test_discovery_client_uses_injected_json_fetcher_and_config_competitions():
    calls = []

    async def _fetch(request):
        calls.append(request)
        return _payload_with_market("1.259502313")

    async def _run():
        client = SharpExchDiscoveryClient(base_url="https://portal.sharpxch.com", json_fetcher=_fetch)
        return await client.discover_events([
            SimpleNamespace(sport="Tennis", competitions=["Men's Wimbledon 2026"]),
        ])

    events = asyncio.run(_run())
    assert len(events) == 1
    assert events[0].competition == "Men's Wimbledon 2026"
    assert len(calls) == 1
    assert calls[0].body["id"] == "2"


def test_discovery_client_paginates_json_fetcher_until_short_page():
    calls = []

    async def _fetch(request):
        calls.append(request.params["page"])
        if request.params["page"] == "0":
            payload = _payload_with_market("1.1")
            market = payload["marketCatalogueList"]["content"][0]
            payload["marketCatalogueList"]["content"] = [deepcopy(market) for _ in range(60)]
            for idx, market in enumerate(payload["marketCatalogueList"]["content"]):
                market["marketId"] = f"1.{idx}"
            return payload
        return _payload_with_market("2.1")

    async def _run():
        client = SharpExchDiscoveryClient(base_url="https://portal.sharpxch.com", json_fetcher=_fetch)
        return await client.discover_events([
            SimpleNamespace(sport="Tennis", competitions=["Men's Wimbledon 2026"]),
        ])

    events = asyncio.run(_run())

    assert calls == ["0", "1"]
    assert len(events) == 61
    assert {event.market_id for event in events} >= {"1.0", "2.1"}


def test_discovery_client_stops_when_next_page_repeats_same_markets():
    calls = []

    async def _fetch(request):
        calls.append(request.params["page"])
        payload = _payload_with_market("1.1")
        market = payload["marketCatalogueList"]["content"][0]
        payload["marketCatalogueList"]["content"] = [deepcopy(market) for _ in range(60)]
        return payload

    async def _run():
        client = SharpExchDiscoveryClient(base_url="https://portal.sharpxch.com", json_fetcher=_fetch)
        return await client.discover_events([
            SimpleNamespace(sport="Tennis", competitions=["Men's Wimbledon 2026"]),
        ])

    events = asyncio.run(_run())

    assert calls == ["0", "1"]
    assert len(events) == 60
