"""MarketMatchingActor —— #58(slice A):timer 触发 + cache-非空 latch + 匹配 + register + publish。

用 risk 的 _factories 模板构造带 matching info 的 PM BinaryOption + OE BettingInstrument。
（旧 on_data(InstrumentsRefreshed) + 2×window gate 已退役:发现迁 DataClient,matching 自 timer 读 cache。）
"""

from decimal import Decimal

import pandas as pd

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
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


# ── matching info 完整的 instrument 构造器 ───────────────────
def _pm(comp, home, away, role, token, claim=None):
    raw = Symbol(f"0xcond-{token}")
    info = {"sport": "Soccer", "competition": comp,
            "home_team": home, "away_team": away,
            "selection_role": role, "game_id": 777}
    if claim:
        info["claim"] = claim
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
        info=info,
    )


def _oe(comp, home, away, role, sel_id, claim=None):
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
        info=(
            {"sport": "Soccer", "competition": comp,
             "home_team": home, "away_team": away,
             "selection_role": role, "claim": claim}
            if claim else
            {"sport": "Soccer", "competition": comp,
             "home_team": home, "away_team": away,
             "selection_role": role}
        ),
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
              "selection_role": role},
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
              "selection_role": "event", "game_id": game_id,
              "tradable": False, "anchor": True},
    )


def _harness(
    interval=30.0,
    *,
    anchor_venue="POLYMARKET",
    tradable_venues=("ORBITEXCH",),
    probability_validation_enabled=False,
):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)
    registry = PairRegistry()
    cfg = MarketMatchingConfig(
        refresh_interval_secs=interval,
        anchor_venue=anchor_venue,
        tradable_venues=tradable_venues,
        probability_validation_enabled=probability_validation_enabled,
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


def _wire_validation_books(actor, cache, books: dict[str, float]):
    subscribed = []
    unsubscribed = []
    actor.subscribe_order_book_deltas = lambda iid, *a, **k: subscribed.append(str(iid))
    actor.unsubscribe_order_book_deltas = lambda iid, *a, **k: unsubscribed.append(str(iid))
    for iid, ask in books.items():
        _add_order_book(cache, InstrumentId.from_str(iid), ask)
    return subscribed, unsubscribed


def _add_order_book(cache, instrument_id, ask):
    book = OrderBook(instrument_id, BookType.L2_MBP)
    book.apply_delta(OrderBookDelta(
        instrument_id=instrument_id,
        action=BookAction.ADD,
        order=BookOrder(
            side=OrderSide.SELL,
            price=Price.from_str(str(ask)),
            size=Quantity.from_int(100),
            order_id=1,
        ),
        flags=0,
        sequence=1,
        ts_event=0,
        ts_init=0,
    ))
    cache.add_order_book(book)


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


def test_probability_validation_passes_then_registers_and_publishes():
    """matching-prob.1:先 register/publish，再取消 Matching 临时订阅。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("ATP", "Rafael Jodar", "Felix Gill")
    pm_home = _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h")
    pm_away = _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a")
    oe_home = _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11)
    oe_away = _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12)
    for instrument in [anchor, pm_home, pm_away, oe_home, oe_away]:
        cache.add_instrument(instrument)
    subscribed, unsubscribed = _wire_validation_books(actor, cache, {
        str(pm_home.id): 0.50,
        str(pm_away.id): 0.50,
        str(oe_home.id): 2.00,
        str(oe_away.id): 2.00,
    })
    published = []

    def publish_after_register_before_unsubscribe(**kwargs):
        pair = kwargs["data"]
        assert pair.order_books_managed is True
        assert registry.instrument_ids_for_pair(pair.pair_id) == set(pair.tradable_instrument_ids)
        assert unsubscribed == []
        published.append(kwargs)

    actor.publish_data = publish_after_register_before_unsubscribe

    actor._maybe_match()

    assert len(published) == 1
    pair_id = published[0]["data"].pair_id
    assert actor._pair_validations[pair_id].status == "PASSED"
    assert registry.instrument_ids_for_pair(pair_id) == set(published[0]["data"].tradable_instrument_ids)
    assert set(subscribed) == {str(pm_home.id), str(pm_away.id), str(oe_home.id), str(oe_away.id)}
    assert set(unsubscribed) == set(subscribed)


def test_probability_validation_waits_when_venue_sum_not_clean():
    """matching-prob.2:任一 venue 自身 ask 概率和 > 1.05 时保持 PENDING,不 publish。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("ATP", "Rafael Jodar", "Felix Gill")
    pm_home = _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h")
    pm_away = _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a")
    oe_home = _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11)
    oe_away = _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12)
    for instrument in [anchor, pm_home, pm_away, oe_home, oe_away]:
        cache.add_instrument(instrument)
    subscribed, unsubscribed = _wire_validation_books(actor, cache, {
        str(pm_home.id): 0.60,
        str(pm_away.id): 0.60,
        str(oe_home.id): 2.00,
        str(oe_away.id): 2.00,
    })
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()
    actor._maybe_match()  # 同 pair_id 已在 validation 中,新 candidate 直接跳过

    assert published == []
    assert len(registry) == 0
    assert list(actor._pair_validations.values())[0].status == "PENDING"
    assert len(subscribed) == 4
    assert unsubscribed == []


def test_probability_validation_failed_is_sticky_and_not_published():
    """matching-prob.3:校验失败转 FAILED,不 register/publish;同 pair 再出现直接跳过。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("ATP", "Rafael Jodar", "Felix Gill")
    pm_home = _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h")
    pm_away = _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a")
    oe_home = _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11)
    oe_away = _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12)
    for instrument in [anchor, pm_home, pm_away, oe_home, oe_away]:
        cache.add_instrument(instrument)
    subscribed, unsubscribed = _wire_validation_books(actor, cache, {
        str(pm_home.id): 0.48,
        str(pm_away.id): 0.48,
        str(oe_home.id): 2.20,
        str(oe_away.id): 2.20,
    })
    published = []
    actor.publish_data = lambda **k: published.append(k)

    actor._maybe_match()
    actor._maybe_match()

    state = list(actor._pair_validations.values())[0]
    assert state.status == "FAILED"
    assert state.best_sum < 0.95
    assert published == []
    assert len(registry) == 0
    assert len(subscribed) == 4
    assert set(unsubscribed) == set(subscribed)


def test_probability_validation_ended_game_clears_state_and_subscription():
    """matching-prob.4:ended eviction 同时清 PairRegistry 与 _pair_validations。"""
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate

    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("ATP", "Rafael Jodar", "Felix Gill", game_id=888)
    pm_home = _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h")
    pm_away = _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a")
    oe_home = _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11)
    oe_away = _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12)
    for instrument in [anchor, pm_home, pm_away, oe_home, oe_away]:
        cache.add_instrument(instrument)
    subscribed, unsubscribed = _wire_validation_books(actor, cache, {
        str(pm_home.id): 0.60,
        str(pm_away.id): 0.60,
        str(oe_home.id): 2.00,
        str(oe_away.id): 2.00,
    })
    actor.publish_data = lambda **k: None

    actor._maybe_match()
    assert actor._pair_validations

    actor.on_data(SportsGameUpdate(
        ts_event=0, ts_init=0, game_id=888, league="x", home_team="", away_team="",
        status="", score="", period="", elapsed="", live=False, ended=True,
        finished_ts="2030-01-01T00:00:00Z",
    ))

    assert actor._pair_validations == {}
    assert actor._validation_pairs_by_instrument == {}
    assert set(unsubscribed) == set(subscribed)


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


# ── #228:3-way 拆多 market 多 pair_id + FAIL 连坐 ─────────────────────────


def _populate_three_way(cache, comp="EPL", home="Arsenal", away="Chelsea"):
    """PM 3-way(3 binary market × yes/no token)+ OE 3-way(每 selection yes + 合成 no)。"""
    legs = {}
    for role, tok in (("home", "h"), ("draw", "d"), ("away", "a")):
        legs[f"pm_{role}_yes"] = _pm(comp, home, away, role, f"{tok}y", claim="yes")
        legs[f"pm_{role}_no"] = _pm(comp, home, away, role, f"{tok}n", claim="no")
    for role, sel in (("home", 11), ("draw", 12), ("away", 13)):
        legs[f"oe_{role}_yes"] = _oe(comp, home, away, role, sel, claim="yes")
        legs[f"oe_{role}_no"] = _oe(comp, home, away, role, sel + 100, claim="no")
    for leg in legs.values():
        cache.add_instrument(leg)
    return legs


def test_three_way_pm_anchor_splits_into_role_pairs():
    """matching-228.1:PM-anchor 路径 3-way → 3 个 role pair,role 后缀在 venue 后缀之前。"""
    actor, clock, cache, registry, _ = _harness()
    _populate_three_way(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k["data"])

    actor._maybe_match()

    pair_ids = {p.pair_id for p in published}
    assert pair_ids == {
        "EPL|Arsenal|Chelsea|home|ORBITEXCH",
        "EPL|Arsenal|Chelsea|draw|ORBITEXCH",
        "EPL|Arsenal|Chelsea|away|ORBITEXCH",
    }
    for p in published:
        assert p.event_key == "EPL|Arsenal|Chelsea"
        assert p.outcomes == ["yes", "no"]
        # 每 venue 每 outcome 恰好一条腿:PM yes/no + OE yes/no
        assert len(p.venue_instrument_ids["POLYMARKET"]) == 2
        assert len(p.venue_instrument_ids["ORBITEXCH"]) == 2
        assert registry.instrument_ids_for_pair(p.pair_id) == set(p.tradable_instrument_ids)


def test_three_way_pmsports_anchor_splits_and_duplicates_anchor():
    """matching-228.2:PMSPORTS 聚合路径 3-way → 3 个无 venue 后缀 pair,唯一锚一对多登记。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
    )
    anchor = _pmsports("EPL", "Arsenal", "Chelsea")
    cache.add_instrument(anchor)
    _populate_three_way(cache)
    published = []
    actor.publish_data = lambda **k: published.append(k["data"])

    actor._maybe_match()

    pair_ids = {p.pair_id for p in published}
    assert pair_ids == {
        "EPL|Arsenal|Chelsea|home",
        "EPL|Arsenal|Chelsea|draw",
        "EPL|Arsenal|Chelsea|away",
    }
    for p in published:
        assert p.anchor_instrument_ids == [str(anchor.id)]
        assert p.outcomes == ["yes", "no"]
        assert registry.anchor_ids_for_pair(p.pair_id) == {str(anchor.id)}
    # game ended → 3 个 pair 同 game_id,一次 evict 全清
    assert actor._game_to_pair[777] == pair_ids


def test_three_way_validation_publishes_only_after_all_roles_pass():
    """matching-228.3:全部 role PASS 后整组注册、发布，再统一退订。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("EPL", "Arsenal", "Chelsea")
    cache.add_instrument(anchor)
    legs = _populate_three_way(cache)
    books = {}
    for role in ("home", "draw", "away"):
        books[str(legs[f"pm_{role}_yes"].id)] = 0.50
        books[str(legs[f"pm_{role}_no"].id)] = 0.52
        books[str(legs[f"oe_{role}_yes"].id)] = 2.00
        books[str(legs[f"oe_{role}_no"].id)] = 2.10
    subscribed, unsubscribed = _wire_validation_books(actor, cache, books)
    published = []
    expected_pair_ids = {
        "EPL|Arsenal|Chelsea|home",
        "EPL|Arsenal|Chelsea|draw",
        "EPL|Arsenal|Chelsea|away",
    }

    def publish_after_group_registered_before_unsubscribe(**kwargs):
        candidate = kwargs["data"]
        assert candidate.order_books_managed is True
        assert all(
            actor._pair_validations[pair_id].status == "PASSED"
            for pair_id in expected_pair_ids
        )
        assert set(registry.all_pair_ids()) == expected_pair_ids
        assert unsubscribed == []
        published.append(candidate)

    actor.publish_data = publish_after_group_registered_before_unsubscribe

    actor._maybe_match()

    assert {pair.pair_id for pair in published} == expected_pair_ids
    assert all(registry.instrument_ids_for_pair(pair.pair_id) for pair in published)
    assert set(unsubscribed) == set(subscribed)


def test_three_way_tradable_anchor_skips_event_when_role_has_only_one_venue():
    """PM-anchor 路径与 PMSPORTS 聚合路径一致，不发布单 venue role。"""
    actor, clock, cache, registry, _ = _harness()
    for role, token in (("home", "h"), ("away", "a")):
        cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", role, f"{token}y", claim="yes"))
        cache.add_instrument(_pm("EPL", "Arsenal", "Chelsea", role, f"{token}n", claim="no"))
    for role, selection in (("home", 11), ("draw", 12), ("away", 13)):
        cache.add_instrument(_oe("EPL", "Arsenal", "Chelsea", role, selection, claim="yes"))
        cache.add_instrument(_oe("EPL", "Arsenal", "Chelsea", role, selection + 100, claim="no"))
    published = []
    actor.publish_data = lambda **kwargs: published.append(kwargs["data"])

    actor._maybe_match()

    assert published == []
    assert registry.instrument_ids_for_pair("EPL|Arsenal|Chelsea|draw|ORBITEXCH") == set()


def test_three_way_validation_fail_never_publishes_event_siblings():
    """matching-228.3:三个 role 未全部 PASS 前不发布;任一 FAIL 后整组 sticky FAILED。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    anchor = _pmsports("EPL", "Arsenal", "Chelsea")
    cache.add_instrument(anchor)
    legs = _populate_three_way(cache)
    # home pair 通过(≈1.0);draw pair 失败(best sum ≈ 0.85 < 0.95);away 后到被 event FAIL 拦截。
    books = {
        str(legs["pm_home_yes"].id): 0.50, str(legs["pm_home_no"].id): 0.52,
        str(legs["oe_home_yes"].id): 2.00, str(legs["oe_home_no"].id): 2.10,   # no 腿 ask=lay 原值 → prob=1−1/2.1
        str(legs["pm_draw_yes"].id): 0.40, str(legs["pm_draw_no"].id): 0.45,
        str(legs["oe_draw_yes"].id): 2.50, str(legs["oe_draw_no"].id): 1.90,
        str(legs["pm_away_yes"].id): 0.50, str(legs["pm_away_no"].id): 0.52,
        str(legs["oe_away_yes"].id): 2.00, str(legs["oe_away_no"].id): 2.10,
    }
    _wire_validation_books(actor, cache, books)
    published = []
    actor.publish_data = lambda **k: published.append(k["data"])

    actor._maybe_match()

    home_pair = "EPL|Arsenal|Chelsea|home"
    draw_pair = "EPL|Arsenal|Chelsea|draw"
    away_pair = "EPL|Arsenal|Chelsea|away"
    # home 虽先 PASS,但须等整组;draw FAIL 后整组均不得进入真钱策略流。
    assert published == []
    assert actor._pair_validations[home_pair].status == "FAILED"
    assert actor._pair_validations[draw_pair].status == "FAILED"
    assert actor._pair_validations[away_pair].status == "FAILED"
    assert registry.instrument_ids_for_pair(home_pair) == set()
    assert registry.instrument_ids_for_pair(draw_pair) == set()
    assert registry.instrument_ids_for_pair(away_pair) == set()


# ── #250:PMSPORTS lifecycle channel(NT subscribe_data 接线)──────────────
def _sports_update(game_id=888, *, ts=0, ended=False, live=False):
    from nautilus_trader.adapters.polymarket.sports import SportsGameUpdate

    return SportsGameUpdate(
        ts_event=ts, ts_init=ts, game_id=game_id, league="x", home_team="", away_team="",
        status="", score="", period="", elapsed="", live=live, ended=ended,
        finished_ts="2030-01-01T00:00:00Z" if ended else "",
    )


def test_per_game_subscription_drives_eviction_and_unsubscribe():
    """matching-3.sports.2:per-game topic 发布经 NT 路由到达 → eviction 关联 pair,
    并退订本场 sports(归零后 client 回收 Store 条目;client 侧行为见 PM adapter 用例)。"""
    from nautilus_trader.adapters.polymarket.sports import sports_data_type

    actor, clock, cache, registry, msgbus = _harness()
    actor.start()   # FSM → RUNNING(handle_data 门槛)

    actor._ensure_sports_subscription(888)          # 发现扫描的订阅动作
    registry.register("p1", ["L1.PM", "L2.OE"], game_id=888)
    actor._game_to_pair[888] = {"p1"}
    actor._emitted_pairs.add("p1")

    msgbus.publish(
        topic=f"data.{sports_data_type(888).topic}",
        msg=_sports_update(888, ts=2, ended=True),
    )

    assert registry.get("L1.PM") is None
    assert 888 in actor._ended_games
    assert 888 not in actor._sports_subscribed      # eviction 已退订本场


def test_unsubscribed_game_topic_does_not_reach_matching():
    """matching-3.sports.1:per-game topic 隔离 —— 未订阅比赛的发布不触发 eviction。"""
    from nautilus_trader.adapters.polymarket.sports import sports_data_type

    actor, clock, cache, registry, msgbus = _harness()
    actor.start()
    actor._ensure_sports_subscription(888)
    registry.register("p1", ["L1.PM"], game_id=999)
    actor._game_to_pair[999] = {"p1"}

    msgbus.publish(
        topic=f"data.{sports_data_type(999).topic}",   # 999 未订阅 → 无 handler
        msg=_sports_update(999, ts=2, ended=True),
    )

    assert registry.get("L1.PM") == "p1"
    assert 999 not in actor._ended_games


def test_candidate_scan_subscribes_games_and_skips_ended():
    """matching-3.sports.4(#252):candidate 产生时按场订阅;ended/evict 后
    不重订(anchor 实体仍留 cache)。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
    )
    actor.start()
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
    assert 888 in actor._sports_subscribed          # 扫描即订

    actor._evict_game(888)
    assert 888 not in actor._sports_subscribed
    actor._maybe_match()                            # ended 后重扫
    assert 888 not in actor._sports_subscribed      # 不重订


def test_anchor_without_tradable_counterpart_not_subscribed():
    """matching-3.sports.5(#252):纯 anchor(无 tradable 对手)不产生 candidate → 不订阅。
    兜住 gamma closed=false 延迟场景:死比赛进宇宙但配不出 candidate,不再造出死订阅。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
    )
    actor.start()
    cache.add_instrument(_pmsports("ATP", "Rafael Jodar", "Felix Gill", game_id=777))
    # 不放任何 tradable instrument
    actor.publish_data = lambda **k: None

    actor._maybe_match()
    assert actor._sports_subscribed == set()


def test_reconcile_evicts_pending_keeps_passed_and_failed_marker():
    """matching-3.sports.6(#252):差集清理 —— PENDING 清态+释放订阅;PASSED 不动;
    FAILED 保留 sticky 标记仅释放订阅。"""
    actor, clock, cache, registry, _ = _harness(
        anchor_venue="PMSPORTS",
        tradable_venues=("POLYMARKET", "ORBITEXCH"),
        probability_validation_enabled=True,
    )
    actor.start()
    for instrument in [
        _pmsports("ATP", "Rafael Jodar", "Felix Gill", game_id=888),
        _pm("ATP", "Rafael Jodar", "Felix Gill", "home", "h"),
        _pm("ATP", "Rafael Jodar", "Felix Gill", "away", "a"),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "home", 11),
        _oe("ATP", "Rafael Jodar", "Felix Gill", "away", 12),
    ]:
        cache.add_instrument(instrument)
    actor.publish_data = lambda **k: None

    actor._maybe_match()                       # candidate → PENDING + 订阅
    assert 888 in actor._sports_subscribed
    pending = [pid for pid, st in actor._pair_validations.items() if st.status == "PENDING"]
    assert pending

    actor._reconcile_sports_subscriptions(set())   # 模拟下 tick candidate 消失
    assert 888 not in actor._sports_subscribed
    assert all(pid not in actor._pair_validations for pid in pending)   # PENDING 清态
    assert actor._validation_pairs_by_instrument == {}                  # 校验 books 退订

    # PASSED 保留:注册后再 reconcile 空集,订阅不动
    actor._ensure_sports_subscription(999)
    actor._game_to_pair[999] = {"p_pass"}
    registry.register("p_pass", ["L1.PM"], game_id=999)
    actor._reconcile_sports_subscriptions(set())
    assert 999 in actor._sports_subscribed

    # FAILED 保留标记,仅释放订阅
    from src.arbitrage.matching.actor import _PairCandidate, _PairValidationState
    cand = _PairCandidate(
        pair_id="p_fail", sport="Tennis", competition="ATP", confidence=1.0,
        anchor_instrument_ids=[], tradable_instrument_ids=[], venue_instrument_ids={},
        registry_instrument_ids=[], registry_anchor_ids=[], game_id=666,
    )
    actor._pair_validations["p_fail"] = _PairValidationState(candidate=cand, status="FAILED")
    actor._ensure_sports_subscription(666)
    actor._game_to_pair[666] = {"p_fail"}
    actor._reconcile_sports_subscriptions(set())
    assert 666 not in actor._sports_subscribed
    assert actor._pair_validations["p_fail"].status == "FAILED"   # sticky 连坐标记保留
