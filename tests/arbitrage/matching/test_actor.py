"""MarketMatchingActor —— #58(slice A):timer 触发 + cache-非空 latch + 匹配 + register + publish。

用 risk 的 _factories 模板构造带 info 6-key 的 PM BinaryOption + OE BettingInstrument。
（旧 on_data(InstrumentsRefreshed) + 2×window gate 已退役:发现迁 DataClient,matching 自 timer 读 cache。）
"""

from decimal import Decimal

import pandas as pd

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import USD
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
        currency="USD", price_precision=2, size_precision=2,
        min_notional=Money(Decimal("1"), USD),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "competition": comp,
              "home_team": home, "away_team": away,
              "start_ts": 0, "selection_role": role},
    )


def _se(comp, home, away, role, sel_id):
    return BettingInstrument(
        venue_name="SHARPEXCH", betting_type="ODDS",
        competition_id=1, competition_name=comp,
        event_country_code="", event_id=1, event_name=f"{home} v {away}",
        event_open_date=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        event_type_id=1, event_type_name="Soccer",
        market_id="se-123", market_name=role,
        market_start_time=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        market_type="MATCH_ODDS",
        selection_handicap=null_handicap(),
        selection_id=sel_id, selection_name=role,
        currency="USD", price_precision=2, size_precision=2,
        min_notional=Money(Decimal("1"), USD),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "competition": comp,
              "home_team": home, "away_team": away,
              "start_ts": 0, "selection_role": role},
    )


def _pmsports(comp, home, away, game_id=777):
    return BettingInstrument(
        venue_name="PMSPORTS", betting_type="EVENT",
        competition_id=0, competition_name=comp,
        event_country_code="", event_id=game_id, event_name=f"{home} v {away}",
        event_open_date=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        event_type_id=0, event_type_name="Soccer",
        market_id=str(game_id), market_name="event_anchor",
        market_start_time=pd.Timestamp("2030-02-07 23:30:00+00:00"),
        market_type="EVENT_ANCHOR",
        selection_handicap=null_handicap(),
        selection_id=game_id, selection_name="event",
        currency="USD", price_precision=2, size_precision=2,
        min_notional=Money(Decimal("0"), USD),
        ts_event=0, ts_init=0,
        info={"sport": "Soccer", "competition": comp,
              "home_team": home, "away_team": away,
              "start_ts": 0, "selection_role": "event", "game_id": game_id,
              "tradable": False, "anchor": True},
    )


def _harness(interval=30.0, *, anchor_venue="POLYMARKET", tradable_venues=("ORBITEXCH",)):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)
    registry = PairRegistry()
    cfg = MarketMatchingConfig(
        refresh_interval_secs=interval,
        anchor_venue=anchor_venue,
        tradable_venues=tradable_venues,
    )
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


def test_market_matching_config_defaults_do_not_implicit_pm_oe_match():
    """未显式配置 anchor/tradable venue 时不做 PM/OE legacy 兜底。"""
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)
    registry = PairRegistry()
    actor = MarketMatchingActor(MarketMatchingConfig(), _RuntimeDeps(pair_registry=registry))
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    _populate_match(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert published == []
    assert len(registry) == 0


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
    pm_ids = mp.venue_instrument_ids["POLYMARKET"]
    oe_ids = mp.venue_instrument_ids["ORBITEXCH"]
    assert len(pm_ids) == 2 and len(oe_ids) == 2
    assert mp.tradable_instrument_ids == pm_ids + oe_ids

    # registry: 4 条腿全映射到同一 pair_id
    pair_id = mp.pair_id
    for iid in mp.tradable_instrument_ids:
        assert registry.get(iid) == pair_id


def test_anchor_and_tradable_venue_fields_preserve_current_pm_anchor_behavior():
    """matching-pmsports-anchor prep:新字段可表达当前 POLYMARKET anchor + ORBITEXCH tradable。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="POLYMARKET",
        tradable_venues=("ORBITEXCH",),
    )
    _populate_match(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert len(published) == 1
    mp = published[0]["data"]
    assert registry.anchor_ids_for_pair(mp.pair_id) == set()
    assert mp.anchor_instrument_ids == []
    assert len(mp.venue_instrument_ids["POLYMARKET"]) == 2
    assert len(mp.venue_instrument_ids["ORBITEXCH"]) == 2


def test_pmsports_anchor_aggregates_enabled_tradable_venues_into_one_pair():
    """matching-pmsports-anchor.1:non-tradable anchor 只进 registry anchor 槽,PM/OE 可交易腿同 pair。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
    )
    anchor = _pmsports("ATP", "Rafael Jodar", "Felix Gill")
    legs = [
        anchor,
        _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h"),
        _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a"),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12),
    ]
    for instrument in legs:
        cache.add_instrument(instrument)
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert len(published) == 1
    mp = published[0]["data"]
    assert mp.pair_id == "ATP|Rafael Jodar|Felix Gill"
    pm_ids = mp.venue_instrument_ids["POLYMARKET"]
    oe_ids = mp.venue_instrument_ids["ORBITEXCH"]
    assert len(pm_ids) == 2
    assert len(oe_ids) == 2
    assert mp.anchor_instrument_ids == [str(anchor.id)]
    assert mp.tradable_instrument_ids == pm_ids + oe_ids
    assert registry.anchor_ids_for_pair(mp.pair_id) == {str(anchor.id)}
    assert registry.instrument_ids_for_pair(mp.pair_id) == set(mp.tradable_instrument_ids)
    assert str(anchor.id) not in registry.instrument_ids_for_pair(mp.pair_id)


def test_pmsports_anchor_ended_evicts_aggregated_pair():
    """matching-pmsports-anchor.3:Sports ended 清理 PMSPORTS anchor 聚合 pair。"""
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate

    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
    )
    for instrument in [
        _pmsports("ATP", "Rafael Jodar", "Felix Gill", game_id=888),
        _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h"),
        _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a"),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12),
    ]:
        cache.add_instrument(instrument)
    actor.publish_data = lambda **k: None

    actor._maybe_match()
    assert len(registry) > 0
    assert 888 in actor._game_to_pair

    actor.on_data(SportsGameUpdate(
        ts_event=0, ts_init=0, game_id=888, league="x", home_team="", away_team="",
        status="", score="", period="", elapsed="", live=False, ended=True,
        finished_ts="2030-01-01T00:00:00Z",
    ))
    assert len(registry) == 0
    assert actor._emitted_pairs == set()
    assert 888 in actor._ended_games

    actor._maybe_match()
    assert len(registry) == 0


def test_sharpexch_tradable_venue_matches_without_orbitexch():
    """matching-3.se.1: tradable_venues 含 SE 时,OE 缺失不阻塞 PM↔SE 匹配。"""
    actor, clock, cache, registry, _ = _harness(tradable_venues=("ORBITEXCH", "SHARPEXCH"))
    cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", "home", "pmh"))
    cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", "away", "pma"))
    cache.add_instrument(_se("EPL", "Arsenal", "Chelsea", "home", 11))
    cache.add_instrument(_se("EPL", "Arsenal", "Chelsea", "away", 12))
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    assert len(published) == 1
    mp = published[0]["data"]
    assert mp.pair_id.endswith("|SHARPEXCH")
    se_ids = mp.venue_instrument_ids["SHARPEXCH"]
    assert all(iid.endswith(".SHARPEXCH") for iid in se_ids)
    for iid in mp.tradable_instrument_ids:
        assert registry.get(iid) == mp.pair_id


def test_multiple_tradable_venues_emit_distinct_pairs_for_same_pm_game():
    """matching-3.se.2: PM↔OE 与 PM↔SE 同场共存时 pair_id 不互相覆盖。"""
    actor, clock, cache, registry, _ = _harness(tradable_venues=("ORBITEXCH", "SHARPEXCH"))
    _populate_match(cache)
    cache.add_instrument(_se("EPL", "Arsenal", "Chelsea", "home", 11))
    cache.add_instrument(_se("EPL", "Arsenal", "Chelsea", "away", 12))
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()

    pair_ids = {item["data"].pair_id for item in published}
    assert pair_ids == {"EPL|Arsenal|Chelsea|ORBITEXCH", "EPL|Arsenal|Chelsea|SHARPEXCH"}
    assert actor._game_to_pair[777] == pair_ids


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
