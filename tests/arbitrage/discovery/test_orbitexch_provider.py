"""
OE InstrumentProvider 测试(用例见 README.md)。

OE 没有上游适配器 → 自写 `OrbitExchInstrumentProvider` 包 `OrbitExchScraper.discover_events`。
本测试用 mock scraper 验证:每场 MatchEvent → 各方向 BettingInstrument(只对有 selection_id 的
方向产腿)、info 6-key 完整、命名 + venue 正确。Browser/page 路径属 live(/live-test 验)。

对应章节: refactor.md §5.1.2, §6.2, §6.4;架构 architectures/discovery/architecture.md §3.2/§4.1
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nautilus_trader.adapters.orbitexch.discovery_scraper import MatchEvent

from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider


@pytest.fixture
def provider():
    return OrbitExchInstrumentProvider(SimpleNamespace())  # scraper 不被 _build_legs 触及


def _event(home_sel="1", draw_sel="2", away_sel="3"):
    return MatchEvent(
        sport="Soccer", competition="EPL", home_team="Arsenal", away_team="Chelsea",
        sport_id="1", competition_id="100", market_id="1-123456",
        home_selection_id=home_sel, draw_selection_id=draw_sel, away_selection_id=away_sel,
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_legs_three_way(provider):
    """discovery-1.4.a: 三方向(含 draw)各出一条腿,顺序 home/draw/away。"""
    legs = list(provider._build_legs(_event()))
    assert [leg.info["selection_role"] for leg in legs] == ["home", "draw", "away"]


def test_build_legs_two_way_drops_missing_draw(provider):
    """discovery-1.4.b: 无 draw_selection_id 时只产 home/away,不强插 draw。"""
    legs = list(provider._build_legs(_event(draw_sel="")))
    assert [leg.info["selection_role"] for leg in legs] == ["home", "away"]


def test_build_legs_fills_six_info_keys(provider):
    """discovery-1.4.c: info 必含 Q9 六统一 key(sport/competition/home_team/away_team/start_ts/selection_role)。"""
    leg = next(iter(provider._build_legs(_event())))
    required = {"sport", "competition", "home_team", "away_team", "start_ts", "selection_role"}
    assert required <= set(leg.info.keys())
    assert leg.info["competition"] == "EPL"
    assert leg.info["sport"] == "Soccer"
    assert leg.info["home_team"] == "Arsenal" and leg.info["away_team"] == "Chelsea"
    assert leg.info["start_ts"] == 0  # TODO Step 1:scraper 抽开赛时间


def test_build_legs_instrument_id_carries_market_and_selection(provider):
    """discovery-1.4.d: InstrumentId 形如 `{market_id}-{selection_id}*.ORBITEXCH`(Q1)。"""
    legs = list(provider._build_legs(_event()))
    for leg, sel in zip(legs, ["1", "2", "3"]):
        s = str(leg.id)
        assert s.endswith(".ORBITEXCH"), s
        assert "1-123456" in s and sel in s


def test_build_legs_sets_orbitexch_min_stake(provider):
    """OE 最小下单额是 stake 7 GBP,应落到 BettingInstrument.min_notional。"""
    leg = next(iter(provider._build_legs(_event())))
    assert leg.min_notional.as_double() == 7.0
    assert str(leg.min_notional.currency) == "GBP"


def test_load_all_async_invokes_scraper_and_adds_instruments():
    """discovery-1.4.e: load_all_async → scraper.discover_events → 基类 add。"""
    scraper = SimpleNamespace()
    scraper.discover_events = AsyncMock(return_value=[_event()])
    prov = OrbitExchInstrumentProvider(scraper)

    _run(prov.load_all_async())

    scraper.discover_events.assert_awaited_once()
    instruments = prov.get_all()
    assert len(instruments) == 3  # home/draw/away


def test_load_all_async_empty_does_not_raise():
    """discovery-1.4.f: scraper 返空 → load_all_async 不抛、Provider 仍可用。"""
    scraper = SimpleNamespace()
    scraper.discover_events = AsyncMock(return_value=[])
    prov = OrbitExchInstrumentProvider(scraper)
    _run(prov.load_all_async())
    assert prov.get_all() == {}


# ── slice 7A:aliases 注入 ─────────────────────────────────────

def test_build_legs_applies_sport_alias():
    """discovery-1.4.g(slice 7A): sport_aliases 命中时 info["sport"] 替换为规范名。"""
    prov = OrbitExchInstrumentProvider(
        SimpleNamespace(),
        sport_aliases={"Soccer": "Soccer Normalized"},
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer Normalized"


def test_build_legs_applies_competition_alias():
    """discovery-1.4.h: competition_aliases 命中(`Men's Roland Garros 2026` → `ATP`)。"""
    prov = OrbitExchInstrumentProvider(
        SimpleNamespace(),
        competition_aliases={"EPL": "English Premier League"},
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["competition"] == "English Premier League"


def test_build_legs_alias_miss_uses_raw():
    """无 alias 命中 → info 用原 sport/competition 字符串。"""
    prov = OrbitExchInstrumentProvider(
        SimpleNamespace(),
        sport_aliases={"Tennis": "TENNIS"},  # 不匹配 _event 的 Soccer
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer"  # 原值透传


def test_build_legs_no_aliases_provided():
    """`sport_aliases=None` / `competition_aliases=None`(默认)→ 原值透传。"""
    prov = OrbitExchInstrumentProvider(SimpleNamespace())
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer"
    assert leg.info["competition"] == "EPL"
