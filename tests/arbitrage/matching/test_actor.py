"""MarketMatchingActor —— #58(slice A):timer 触发 + cache-非空 latch + 匹配 + register + publish。

用 risk 的 _factories 模板构造带 info 6-key 的 PM BinaryOption + OE BettingInstrument。
（旧 on_data(InstrumentsRefreshed) + 2×window gate 已退役:发现迁 DataClient,matching 自 timer 读 cache。）
"""

from decimal import Decimal

import pandas as pd

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.instruments.betting import null_handicap
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.matching.actor import MarketMatchingActor
from src.arbitrage.matching.actor import MarketMatchingConfig
from src.arbitrage.matching.actor import _RuntimeDeps
from src.arbitrage.matching.events import MatchedPair


# ── 6-key 完整的 instrument 构造器(matching 用)───────────────────
def _pm(comp, home, away, role, token):
    raw = Symbol(f"0xcond-{token}")
    return BinaryOption(
        instrument_id=InstrumentId(symbol=raw, venue=Venue("POLYMARKET")),
        raw_symbol=raw, outcome=role, description="t",
        asset_class=AssetClass.ALTERNATIVE, currency=USDC,
        price_precision=3, price_increment=Price.from_str("0.001"),
        size_precision=2, size_increment=Quantity.from_str("0.01"),
        activation_ns=0, expiration_ns=pd.Timestamp("2030-01-01", tz="UTC").value,
        max_quantity=None, min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0), taker_fee=Decimal(0),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "competition": comp,
              "home_team": home, "away_team": away,
              "start_ts": 0, "selection_role": role, "game_id": 777},
    )


def _oe(comp, home, away, role, sel_id):
    return BettingInstrument(
        venue_name="ORBITEXCH", betting_type="ODDS",
        competition_id=1, competition_name=comp,
        event_country_code="", event_id=1, event_name=f"{home} v {away}",
        event_open_date=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        event_type_id=1, event_type_name="Soccer",
        market_id="1-123", market_name=role,
        market_start_time=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        market_type="MATCH_ODDS",
        selection_handicap=null_handicap(),
        selection_id=sel_id, selection_name=role,
        currency="GBP", price_precision=2, size_precision=2,
        min_notional=Money(Decimal("1"), GBP),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "competition": comp,
              "home_team": home, "away_team": away,
              "start_ts": 0, "selection_role": role},
    )


def _harness(interval=30.0):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)
    registry = PairRegistry()
    cfg = MarketMatchingConfig(refresh_interval_secs=interval, min_similarity=1)
    actor = MarketMatchingActor(cfg, _RuntimeDeps(pair_registry=registry))
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    return actor, clock, cache, registry, msgbus


def _populate_match(cache, comp="EPL", home="Arsenal", away="Chelsea"):
    """同 (comp, home, away) 各 venue 两腿入 cache(home/away)。"""
    cache.add_instrument(_pm(comp, home, away, "home", f"pmh{comp}"))
    cache.add_instrument(_pm(comp, home, away, "away", f"pma{comp}"))
    cache.add_instrument(_oe(comp, home, away, "home", 1))
    cache.add_instrument(_oe(comp, home, away, "away", 2))


# ── 触发:_maybe_match(timer 回调驱动)+ cache-非空 latch ──────────────
def test_only_one_venue_in_cache_no_match():
    """matching-3.1.a(#58): 只一边 cache 有 instrument → latch 不过,不匹配。"""
    actor, clock, cache, registry, _ = _harness()
    cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", "home", "pmh"))
    cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", "away", "pma"))
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()
    assert published == [] and len(registry) == 0


def test_both_venues_in_cache_matches_and_publishes():
    """matching-3.1.b(#58): 两 venue cache 都有 → 匹配 + register + publish MatchedPair。"""
    actor, clock, cache, registry, _ = _harness()
    _populate_match(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert len(published) == 1
    mp = published[0]["data"]
    assert isinstance(mp, MatchedPair)
    assert mp.competition == "EPL" and mp.confidence > 0
    assert len(mp.pm_instrument_ids) == 2 and len(mp.oe_instrument_ids) == 2

    # registry: 4 条腿全映射到同一 pair_id
    pair_id = mp.pair_id
    for iid in mp.pm_instrument_ids + mp.oe_instrument_ids:
        assert registry.get(iid) == pair_id


def test_on_alert_triggers_match_and_reschedules():
    """matching-3.1.c(#58): clock alert 回调 → _maybe_match 跑 + 重排下次 alert。"""
    actor, clock, cache, registry, _ = _harness(interval=30.0)
    _populate_match(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor.on_start()                       # 排首次 alert
    actor._on_alert(None)                  # 模拟 alert 触发

    assert len(published) == 1             # 匹配并发布
    assert clock.next_time_ns(_match_alert_name()) > 0   # 重排了下次


def test_match_unrelated_competition_no_pair():
    """matching-3.3: 不同 competition → 不进入互比,无 MatchedPair(两 venue 都有 instrument,latch 过)。"""
    actor, clock, cache, registry, _ = _harness()
    cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", "home", "p1"))
    cache.add_instrument(_oe("LaLiga", "RealMadrid", "Barcelona", "home", 1))
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert published == [] and len(registry) == 0


def test_sports_ended_evicts_pair():
    """matching-3.5(#60): sports `ended:true` → 经 gameId 查 pair → unregister + 不再 re-match。"""
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
    actor, clock, cache, registry, _ = _harness()
    _populate_match(cache)              # PM 腿 info["game_id"]=777
    actor.publish_data = lambda **k: None

    actor._maybe_match()                # 正常匹配 → 注册 + 记 gameId→pair
    assert len(registry) > 0
    assert 777 in actor._game_to_pair

    # sports WS 推该 game ended
    actor.on_data(SportsGameUpdate(
        ts_event=0, ts_init=0, game_id=777, league="x", home_team="", away_team="",
        status="", score="", period="", elapsed="", live=False, ended=True,
        finished_ts="2030-01-01T00:00:00Z",
    ))
    assert len(registry) == 0           # game ended → evicted
    assert actor._emitted_pairs == set()
    assert 777 in actor._ended_games

    # 再 match 不 re-register(已结束 game 的 PM 腿被排除)
    actor._maybe_match()
    assert len(registry) == 0


def test_sports_update_non_ended_ignored():
    """matching-3.6(#60): live(未 ended)的 SportsGameUpdate 不触发 eviction。"""
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate
    actor, clock, cache, registry, _ = _harness()
    _populate_match(cache)
    actor.publish_data = lambda **k: None
    actor._maybe_match()
    assert len(registry) > 0
    actor.on_data(SportsGameUpdate(
        ts_event=0, ts_init=0, game_id=777, league="x", home_team="", away_team="",
        status="InProgress", score="1-0", period="2H", elapsed="80:00", live=True, ended=False,
        finished_ts="",
    ))
    assert len(registry) > 0            # 未 ended → 不清


def _match_alert_name() -> str:
    from src.arbitrage.matching.actor import _MATCH_ALERT
    return _MATCH_ALERT
