"""OE DiscoveryClient 测试 —— 解析 `sport/details` API 响应。

2026-07-03: 迁移到 `sport/details` API,与 SE 对齐。
"""

import asyncio

from nautilus_trader.adapters.orbitexch.discovery_client import (
    OrbitExchDiscoveryClient,
    OrbitExchMarketEvent,
    OrbitExchRunner,
    OrbitExchSportDetailsRequest,
    events_from_sport_details,
    sport_details_request,
)


# ── sport_details_request 构造 ────────────────────────────────────


def test_sport_details_request_default_tennis():
    """无 sport_config 时默认 Tennis(sport_id=2)。"""
    req = sport_details_request("https://www.orbitexch.com", None)
    assert req.method == "POST"
    assert req.url == "https://www.orbitexch.com/customer/api/sport/details"
    assert req.body["id"] == "2"  # Tennis
    assert req.body["viewBy"] == "TIME"
    assert req.body["marketTypeFilter"] == "MATCH_ODDS_AND_EXTRA_TIME"


def test_sport_details_request_with_sport_name():
    """sport_config 为字符串时按名称映射 sport_id。"""
    req = sport_details_request("https://www.orbitexch.com", "Soccer")
    assert req.body["id"] == "1"


def test_sport_details_request_with_sport_id():
    """sport_config 为数字字符串时直接用作 sport_id。"""
    req = sport_details_request("https://www.orbitexch.com", "7522")
    assert req.body["id"] == "7522"


def test_sport_details_request_with_sport_config_object():
    """sport_config 有 .sport 属性时读取。"""
    from types import SimpleNamespace
    cfg = SimpleNamespace(sport="Basketball", competitions=["NBA"])
    req = sport_details_request("https://www.orbitexch.com", cfg)
    assert req.body["id"] == "7522"  # Basketball
    assert req.target_competitions == ("NBA",)


def test_sport_details_request_pagination():
    """分页参数正确传递。"""
    req = sport_details_request("https://www.orbitexch.com", None, page=2, size=30)
    assert req.params["page"] == "2"
    assert req.params["size"] == "30"


# ── events_from_sport_details 解析 ────────────────────────────────


def _sample_payload():
    """模拟 OE sport/details 响应。"""
    return {
        "sportInfo": {"id": "2", "name": "Tennis"},
        "marketCatalogueList": {
            "content": [
                {
                    "marketId": "1-123456",
                    "marketName": "Match Odds",
                    "marketStartTime": 1704067200000,  # 2024-01-01 00:00:00 UTC
                    "competition": {"id": "100", "name": "ATP"},
                    "event": {
                        "name": "Player A v Player B",
                        "homeTeam": "Player A",
                        "awayTeam": "Player B",
                        "openDate": 1704067200000,
                    },
                    "eventType": {"id": "2", "name": "Tennis"},
                    "runners": [
                        {"selectionId": "1", "runnerName": "Player A", "handicap": 0},
                        {"selectionId": "2", "runnerName": "Player B", "handicap": 0},
                    ],
                    "inPlay": False,
                },
            ],
            "totalElements": 1,
            "last": True,
        },
    }


def test_events_from_sport_details_parses_market():
    """解析 Match Odds market 为 MarketEvent。"""
    events = events_from_sport_details(_sample_payload())
    assert len(events) == 1
    ev = events[0]
    assert ev.sport == "Tennis"
    assert ev.competition == "ATP"
    assert ev.home_team == "Player A"
    assert ev.away_team == "Player B"
    assert ev.market_id == "1-123456"
    assert ev.start_ts == 1704067200000 * 1_000_000  # millis to nanos
    assert len(ev.runners) == 2


def test_events_from_sport_details_assigns_roles():
    """runner 按名称匹配 home/away role。"""
    events = events_from_sport_details(_sample_payload())
    runners = events[0].runners
    roles = {r.role for r in runners}
    assert roles == {"home", "away"}


def test_events_from_sport_details_filters_by_competition():
    """target_competitions 过滤不匹配的 competition。"""
    events = events_from_sport_details(_sample_payload(), target_competitions=["WTA"])
    assert len(events) == 0  # ATP 不在 target_competitions


def test_events_from_sport_details_accepts_matching_competition():
    """target_competitions 包含匹配的 competition 时返回。"""
    events = events_from_sport_details(_sample_payload(), target_competitions=["ATP"])
    assert len(events) == 1


def test_events_from_sport_details_no_filter_returns_all():
    """无 target_competitions 时返回所有 Match Odds market。"""
    events = events_from_sport_details(_sample_payload())
    assert len(events) == 1


def test_events_from_sport_details_skips_non_match_odds():
    """非 Match Odds market 被跳过。"""
    payload = _sample_payload()
    payload["marketCatalogueList"]["content"][0]["marketName"] = "Over/Under 2.5"
    events = events_from_sport_details(payload)
    assert len(events) == 0


def test_events_from_sport_details_three_way_market():
    """三方向 market(含 draw)正确解析。"""
    payload = _sample_payload()
    payload["marketCatalogueList"]["content"][0]["runners"].append(
        {"selectionId": "3", "runnerName": "The Draw", "handicap": 0}
    )
    events = events_from_sport_details(payload)
    assert len(events[0].runners) == 3
    roles = {r.role for r in events[0].runners}
    assert roles == {"home", "draw", "away"}


def test_events_from_sport_details_fallback_role_by_index():
    """runner 名称不匹配时按索引回退 role。"""
    payload = _sample_payload()
    # 修改 runner 名称使其不匹配 home_team/away_team
    payload["marketCatalogueList"]["content"][0]["runners"] = [
        {"selectionId": "1", "runnerName": "Team X", "handicap": 0},
        {"selectionId": "2", "runnerName": "Team Y", "handicap": 0},
    ]
    events = events_from_sport_details(payload)
    roles = [r.role for r in events[0].runners]
    assert roles == ["home", "away"]  # 按索引回退


def test_events_from_sport_details_teams_from_event_name():
    """event.homeTeam/awayTeam 缺失时从 event.name 解析。"""
    payload = _sample_payload()
    del payload["marketCatalogueList"]["content"][0]["event"]["homeTeam"]
    del payload["marketCatalogueList"]["content"][0]["event"]["awayTeam"]
    events = events_from_sport_details(payload)
    assert events[0].home_team == "Player A"
    assert events[0].away_team == "Player B"


# ── OrbitExchDiscoveryClient ────────────────────────────────────


def test_discovery_client_with_json_fetcher():
    """json_fetcher 注入时正确调用并解析响应。"""
    async def mock_fetcher(request: OrbitExchSportDetailsRequest) -> dict:
        return _sample_payload()

    async def run():
        client = OrbitExchDiscoveryClient(json_fetcher=mock_fetcher)
        return await client.discover_events()

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0].sport == "Tennis"


def test_discovery_client_no_provider_returns_empty():
    """无 provider 和 fetcher 时返回空列表。"""
    async def run():
        client = OrbitExchDiscoveryClient()
        return await client.discover_events()

    events = asyncio.run(run())
    assert events == []


def test_discovery_client_with_sport_details_provider():
    """sport_details_provider 注入时正确调用。"""
    async def mock_provider(sport_config) -> dict:
        return _sample_payload()

    async def run():
        client = OrbitExchDiscoveryClient(sport_details_provider=mock_provider)
        return await client.discover_events()

    events = asyncio.run(run())
    assert len(events) == 1


def _full_page_payload(market_id="1-123456", page_size=60):
    """生成满页 payload(用于分页测试)。"""
    content = []
    for i in range(page_size):
        market = {
            "marketId": f"{market_id}-{i}",
            "marketName": "Match Odds",
            "marketStartTime": 1704067200000,
            "competition": {"id": "100", "name": "ATP"},
            "event": {
                "name": "Player A v Player B",
                "homeTeam": "Player A",
                "awayTeam": "Player B",
            },
            "eventType": {"id": "2", "name": "Tennis"},
            "runners": [
                {"selectionId": "1", "runnerName": "Player A"},
                {"selectionId": "2", "runnerName": "Player B"},
            ],
        }
        content.append(market)
    return {
        "sportInfo": {"id": "2", "name": "Tennis"},
        "marketCatalogueList": {
            "content": content,
            "totalElements": page_size * 2,
            "totalPages": 2,
            "last": False,
        },
    }


def test_discovery_client_pagination():
    """json_fetcher 分页正确处理。"""
    call_count = 0

    async def mock_fetcher(request: OrbitExchSportDetailsRequest) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _full_page_payload("page1", page_size=60)
        else:
            payload = _full_page_payload("page2", page_size=30)
            payload["marketCatalogueList"]["last"] = True
            return payload

    async def run():
        client = OrbitExchDiscoveryClient(json_fetcher=mock_fetcher)
        return await client.discover_events()

    events = asyncio.run(run())
    assert call_count == 2
    assert len(events) == 90  # 60 + 30


def test_discovery_client_stops_on_duplicate_markets():
    """第二页返回相同 market_id 时停止分页。"""
    call_count = 0

    async def mock_fetcher(request: OrbitExchSportDetailsRequest) -> dict:
        nonlocal call_count
        call_count += 1
        # 每次都返回相同的满页 payload
        return _full_page_payload("same", page_size=60)

    async def run():
        client = OrbitExchDiscoveryClient(json_fetcher=mock_fetcher)
        return await client.discover_events()

    events = asyncio.run(run())
    # 第二页无新 market_id,分页应该停止
    assert call_count == 2
    assert len(events) == 60  # 只有第一页的数据
