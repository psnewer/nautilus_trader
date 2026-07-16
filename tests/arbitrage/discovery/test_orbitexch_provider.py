"""
OE InstrumentProvider 测试(用例见 README.md)。

OE 自写 `OrbitExchInstrumentProvider` 包 `OrbitExchDiscoveryClient.discover_events`。
本测试用 mock discovery client 验证:每场 MarketEvent → 各方向 BettingInstrument(只对有 selection_id 的
方向产腿)、matching info 完整、命名 + venue 正确。Browser/page 路径属 live(/live-test 验)。

2026-07-03: 迁移到 `sport/details` API,与 SE 对齐。

对应章节: refactor.md §5.1.2, §6.2, §6.4;架构 architectures/discovery/architecture.md §3.2/§4.1
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchDiscoveryClient
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchMarketEvent
from nautilus_trader.adapters.orbitexch.discovery_client import OrbitExchRunner

from nautilus_trader.adapters.orbitexch.providers import OrbitExchInstrumentProvider


@pytest.fixture
def provider():
    # discovery 不被 _build_legs 触及,用 mock
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[])
    return OrbitExchInstrumentProvider(discovery)


def _event(home_sel="1", draw_sel="2", away_sel="3", start_ts=1704067200000000000):
    """创建测试用 MarketEvent,支持可选的 draw。"""
    runners = []
    if home_sel:
        runners.append(OrbitExchRunner(selection_id=home_sel, runner_name="Arsenal", role="home"))
    if draw_sel:
        runners.append(OrbitExchRunner(selection_id=draw_sel, runner_name="The Draw", role="draw"))
    if away_sel:
        runners.append(OrbitExchRunner(selection_id=away_sel, runner_name="Chelsea", role="away"))
    return OrbitExchMarketEvent(
        sport="Soccer", competition="EPL", home_team="Arsenal", away_team="Chelsea",
        sport_id="1", competition_id="100", market_id="1-123456",
        start_ts=start_ts, runners=tuple(runners),
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_legs_three_way(provider):
    """discovery-1.4.a(#228): 3-way 每 selection 产 yes + 合成 no 两条腿,共 6 条。"""
    legs = list(provider._build_legs(_event()))
    assert [(leg.info["selection_role"], leg.info["claim"]) for leg in legs] == [
        ("home", "yes"), ("home", "no"),
        ("draw", "yes"), ("draw", "no"),
        ("away", "yes"), ("away", "no"),
    ]
    yes_home, no_home = legs[0], legs[1]
    # no 腿:缓存 identity 使用负 selection,真实 selection 单独保留,执行重定向回 yes
    assert no_home.market_id == yes_home.market_id
    assert no_home.selection_id == -(yes_home.selection_id + 1)
    assert no_home.info["venue_selection_id"] == yes_home.selection_id
    assert no_home.id != yes_home.id
    assert no_home.id.symbol.is_composite() is False
    assert no_home.info["quote_claim"] == "no"
    assert no_home.info["exec_instrument_id"] == str(yes_home.id)


def test_build_legs_two_way_drops_missing_draw(provider):
    """无 draw 时只产两个真实 runner，并统一映射为 yes/no。"""
    legs = list(provider._build_legs(_event(draw_sel="")))
    assert [leg.info["selection_role"] for leg in legs] == ["home", "away"]
    assert [leg.info["claim"] for leg in legs] == ["yes", "no"]
    assert all("exec_instrument_id" not in leg.info for leg in legs)


def test_build_legs_fills_matching_info_keys(provider):
    """discovery-1.4.c: info 必含 matching 统一 key。"""
    leg = next(iter(provider._build_legs(_event())))
    required = {"sport", "competition", "home_team", "away_team", "selection_role"}
    assert required <= set(leg.info.keys())
    assert leg.info["competition"] == "EPL"
    assert leg.info["sport"] == "Soccer"
    assert leg.info["home_team"] == "Arsenal" and leg.info["away_team"] == "Chelsea"


def test_build_legs_instrument_id_carries_market_and_selection(provider):
    """discovery-1.4.d: InstrumentId 形如 `{market_id}-{selection_id}*.ORBITEXCH`(Q1)。"""
    legs = list(provider._build_legs(_event()))
    for leg, sel in zip(legs, ["1", "2", "3"]):
        s = str(leg.id)
        assert s.endswith(".ORBITEXCH"), s
        assert "1-123456" in s and sel in s


def test_build_legs_sets_orbitexch_min_stake(provider):
    """OE 最小 stake 入 NT 时按 adapter 外 USD 口径写成 7*fx。"""
    leg = next(iter(provider._build_legs(_event())))
    assert leg.min_notional.as_double() == 7.0
    assert str(leg.min_notional.currency) == "USD"


def test_build_legs_sets_orbitexch_min_stake_with_fx():
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[])
    prov = OrbitExchInstrumentProvider(discovery, fx=1.3)
    leg = next(iter(prov._build_legs(_event())))
    assert leg.min_notional.as_double() == pytest.approx(9.1)
    assert str(leg.min_notional.currency) == "USD"


def test_load_all_async_invokes_discovery_and_adds_instruments():
    """discovery-1.4.e: load_all_async → discovery.discover_events → 基类 add。"""
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[_event()])
    prov = OrbitExchInstrumentProvider(discovery)

    _run(prov.load_all_async())

    discovery.discover_events.assert_awaited_once()
    instruments = prov.get_all()
    assert len(instruments) == 6  # #228:3-way = (home/draw/away) × (yes/no)


def test_load_all_async_empty_does_not_raise():
    """discovery-1.4.f: discovery 返空 → load_all_async 不抛、Provider 仍可用。"""
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[])
    prov = OrbitExchInstrumentProvider(discovery)
    _run(prov.load_all_async())
    assert prov.get_all() == {}


# ── slice 7A:aliases 注入 ─────────────────────────────────────

def _mock_discovery():
    discovery = SimpleNamespace()
    discovery.discover_events = AsyncMock(return_value=[])
    return discovery


def test_build_legs_applies_sport_alias():
    """discovery-1.4.g(slice 7A): sport_aliases 命中时 info["sport"] 替换为规范名。"""
    prov = OrbitExchInstrumentProvider(
        _mock_discovery(),
        sport_aliases={"Soccer": "Soccer Normalized"},
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer Normalized"


def test_build_legs_applies_competition_alias():
    """discovery-1.4.h: competition_aliases 命中(`Men's Roland Garros 2026` → `ATP`)。"""
    prov = OrbitExchInstrumentProvider(
        _mock_discovery(),
        competition_aliases={"EPL": "English Premier League"},
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["competition"] == "English Premier League"


def test_build_legs_alias_miss_uses_raw():
    """无 alias 命中 → info 用原 sport/competition 字符串。"""
    prov = OrbitExchInstrumentProvider(
        _mock_discovery(),
        sport_aliases={"Tennis": "TENNIS"},  # 不匹配 _event 的 Soccer
    )
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer"  # 原值透传


def test_build_legs_no_aliases_provided():
    """`sport_aliases=None` / `competition_aliases=None`(默认)→ 原值透传。"""
    prov = OrbitExchInstrumentProvider(_mock_discovery())
    leg = next(iter(prov._build_legs(_event())))
    assert leg.info["sport"] == "Soccer"
    assert leg.info["competition"] == "EPL"
