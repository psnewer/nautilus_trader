"""ArbPolymarketInstrumentProvider —— PM series-based 发现纯函数测试(#55/#57)。

队名解析(`_parse_team_names`)+ selection_role(`_role_for_token`)+ moneyline instrument info 写入
是离线纯逻辑,可单测;完整 load_all_async(真 gamma API + httpx)经 /live-test 验。
"""

from types import SimpleNamespace

from nautilus_trader.adapters.polymarket.arb_provider import ArbPolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.arb_provider import _parse_team_names
from nautilus_trader.adapters.polymarket.arb_provider import _role_for_token
from nautilus_trader.adapters.polymarket.arb_provider import _teams_from_event
from nautilus_trader.adapters.polymarket.arb_provider import _ticker_abbrs


# ─── _teams_from_event(#56:权威队名源)────────────────────────────


def _team(name, abbr, ordering):
    return {"name": name, "abbreviation": abbr, "ordering": ordering}


def test_teams_from_event_home_away():
    ev = {"teams": [_team("Marko Topo", "topo", "home"), _team("Emilio Nava", "nava", "away")]}
    assert _teams_from_event(ev) == ("Marko Topo", "Emilio Nava", "topo", "nava")


def test_teams_from_event_order_independent():
    """teams 列表顺序不影响 —— 靠 ordering 字段判 home/away。"""
    ev = {"teams": [_team("Emilio Nava", "nava", "away"), _team("Marko Topo", "topo", "home")]}
    assert _teams_from_event(ev) == ("Marko Topo", "Emilio Nava", "topo", "nava")


def test_teams_from_event_abbr_lowercased():
    ev = {"teams": [_team("Burnley FC", "BUR", "home"), _team("Wolves", "WOL", "away")]}
    h, a, ha, aa = _teams_from_event(ev)
    assert ha == "bur" and aa == "wol"


def test_teams_from_event_missing_returns_none():
    assert _teams_from_event({}) is None
    assert _teams_from_event({"teams": []}) is None


def test_teams_from_event_incomplete_returns_none():
    """只有 home 没 away → None(调用方 fallback title)。"""
    ev = {"teams": [_team("Marko Topo", "topo", "home")]}
    assert _teams_from_event(ev) is None


def test_teams_from_event_missing_name_returns_none():
    ev = {"teams": [_team("", "topo", "home"), _team("Nava", "nava", "away")]}
    assert _teams_from_event(ev) is None


# ─── _parse_team_names ────────────────────────────────────────────


def test_parse_vs_dot():
    assert _parse_team_names("Burnley FC vs. Wolverhampton FC") == ("Burnley FC", "Wolverhampton FC")


def test_parse_vs_spaces():
    assert _parse_team_names("Marko Topo vs Emilio Nava") == ("Marko Topo", "Emilio Nava")


def test_parse_with_competition_prefix_cleaned():
    """`"Heilbronn: Marko Topo vs Emilio Nava"` → 主队去 `Heilbronn:` 前缀。"""
    home, away = _parse_team_names("Heilbronn: Marko Topo vs Emilio Nava")
    assert home == "Marko Topo"
    assert away == "Emilio Nava"


def test_parse_away_strips_trailing_after_dash():
    """客队 `"Emilio Nava - Total Corners"` → keep_before `-` → "Emilio Nava"。"""
    home, away = _parse_team_names("Marko Topo vs Emilio Nava - Total Corners")
    assert away == "Emilio Nava"


def test_parse_who_will_win_prefix():
    home, away = _parse_team_names("Who will win Spain vs France?")
    assert home == "Spain"
    assert away == "France"


def test_parse_scheduled_suffix_removed():
    home, away = _parse_team_names("Spain vs France, scheduled for 2026-06-01")
    assert (home, away) == ("Spain", "France")


def test_parse_no_vs_returns_none():
    assert _parse_team_names("2026 French Open Winner") is None


def test_parse_regex_vs_uppercase():
    assert _parse_team_names("Spain VS France") == ("Spain", "France")


# ─── _ticker_abbrs ────────────────────────────────────────────────


def test_ticker_abbrs():
    assert _ticker_abbrs("epl-bur-wol-2026-05-24") == ("bur", "wol")
    assert _ticker_abbrs("atp-topo-nava-2026-06-03") == ("topo", "nava")


def test_ticker_abbrs_short():
    assert _ticker_abbrs("foo") == ("", "")


# ─── _role_for_token: 2-way(slug == ticker;ordering 决定映射)──────


def test_2way_ordering_home():
    """ordering=home(如 ATP)→ outcomes=[home, away]。"""
    t = "atp-topo-nava-2026-06-03"
    outcomes = ["Marko Topo", "Emilio Nava"]
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Marko Topo", outcomes=outcomes, ordering="home", home_abbr="topo", away_abbr="nava") == "home"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Emilio Nava", outcomes=outcomes, ordering="home", home_abbr="topo", away_abbr="nava") == "away"


def test_2way_ordering_away_reversed():
    """ordering=away(如 MLB)→ outcomes=[away, home];competition 特异,下标反排仍正确。"""
    t = "mlb-mil-stl-2026-05-05"
    outcomes = ["Milwaukee Brewers", "St. Louis Cardinals"]  # [away, home]
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Milwaukee Brewers", outcomes=outcomes, ordering="away", home_abbr="stl", away_abbr="mil") == "away"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="St. Louis Cardinals", outcomes=outcomes, ordering="away", home_abbr="stl", away_abbr="mil") == "home"


def test_2way_outcome_not_in_list():
    t = "atp-topo-nava-2026-06-03"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Nobody", outcomes=["A", "B"], ordering="home", home_abbr="topo", away_abbr="nava") == ""


def test_3way_single_market_ordering_home():
    """3-way 多结果主市场(slug==ticker,ordering=home → [home,draw,away])。"""
    t = "epl-bur-wol-2026-05-24"
    outcomes = ["Burnley", "Draw", "Wolves"]
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Burnley", outcomes=outcomes, ordering="home", home_abbr="bur", away_abbr="wol") == "home"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Draw", outcomes=outcomes, ordering="home", home_abbr="bur", away_abbr="wol") == "draw"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Wolves", outcomes=outcomes, ordering="home", home_abbr="bur", away_abbr="wol") == "away"


def test_3way_single_market_ordering_away_reversed():
    """ordering=away → 单市场 3-outcome 反排 [away,draw,home],draw 仍居中。"""
    t = "x-aa-bb-2026-01-01"
    outcomes = ["AwayTeam", "Draw", "HomeTeam"]
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="AwayTeam", outcomes=outcomes, ordering="away", home_abbr="bb", away_abbr="aa") == "away"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="Draw", outcomes=outcomes, ordering="away", home_abbr="bb", away_abbr="aa") == "draw"
    assert _role_for_token(market_slug=t, event_ticker=t, outcome="HomeTeam", outcomes=outcomes, ordering="away", home_abbr="bb", away_abbr="aa") == "home"


# ─── _role_for_token: 3-way binary(slug == ticker-{abbr};不受 ordering 影响)──


def test_3way_binary_home_yes():
    t = "epl-bur-wol-2026-05-24"
    assert _role_for_token(market_slug=f"{t}-bur", event_ticker=t, outcome="Yes", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == "home"


def test_3way_binary_away_yes():
    t = "epl-bur-wol-2026-05-24"
    assert _role_for_token(market_slug=f"{t}-wol", event_ticker=t, outcome="Yes", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == "away"


def test_3way_binary_draw_yes():
    t = "epl-bur-wol-2026-05-24"
    assert _role_for_token(market_slug=f"{t}-draw", event_ticker=t, outcome="Yes", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == "draw"


def test_3way_binary_no_token_skipped():
    """"No" token 不取 role(只买 Yes 方向)。"""
    t = "epl-bur-wol-2026-05-24"
    assert _role_for_token(market_slug=f"{t}-bur", event_ticker=t, outcome="No", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == ""


def test_unknown_suffix_skipped():
    """非 home/away/draw 后缀(防御)→ 跳过。"""
    t = "epl-bur-wol-2026-05-24"
    assert _role_for_token(market_slug=f"{t}-foobar", event_ticker=t, outcome="Yes", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == ""


def test_empty_ticker_returns_empty():
    assert _role_for_token(market_slug="x", event_ticker="", outcome="Yes", outcomes=["Yes", "No"], ordering="home", home_abbr="bur", away_abbr="wol") == ""


# ─── _load_moneyline_market: PM 6-key + game_id 写入 ──────────────


def test_load_moneyline_market_writes_matching_info_keys():
    provider = object.__new__(ArbPolymarketInstrumentProvider)
    provider._clock = SimpleNamespace(timestamp_ns=lambda: 0)
    added = []
    provider.add = lambda inst: added.append(inst)

    market = {
        "conditionId": "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
        "questionID": "q",
        "question": "ATP: A vs B",
        "description": "",
        "slug": "atp-aaa-bbb-2026-06-03",
        "enableOrderBook": True,
        "orderPriceMinTickSize": 0.001,
        "orderMinSize": 5,
        "acceptingOrders": True,
        "active": True,
        "closed": False,
        "archived": False,
        "endDateIso": "2026-06-03T00:00:00Z",
        "startDateIso": "2026-06-03T10:00:00Z",
        "marketMakerAddress": "",
        "outcomes": '["A","B"]',
        "outcomePrices": '["0.5","0.5"]',
        "clobTokenIds": '["1","2"]',
        "sportsMarketType": "moneyline",
    }

    count = provider._load_moneyline_market(
        market=market,
        event_ticker="atp-aaa-bbb-2026-06-03",
        comp_name="ATP",
        sport="Tennis",
        home_team="A",
        away_team="B",
        home_abbr="aaa",
        away_abbr="bbb",
        event_start="2026-06-03T10:00:00Z",
        ordering="home",
        game_id=5843495,
    )

    assert count == 2
    assert [inst.info["selection_role"] for inst in added] == ["home", "away"]
    for inst in added:
        assert inst.info["sport"] == "Tennis"
        assert inst.info["competition"] == "ATP"
        assert inst.info["home_team"] == "A"
        assert inst.info["away_team"] == "B"
        assert inst.info["start_ts"] == 1780480800000000000
        assert inst.info["game_id"] == 5843495
