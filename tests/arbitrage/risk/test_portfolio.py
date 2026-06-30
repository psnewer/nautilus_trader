"""ArbitragePortfolio outcome exposure / outcome share 聚合(risk-6.9.x)。

腿提取(`_legs_for_pair` / `_active_pair_ids`)依赖 cache 中的真实 NT Position;为隔离
聚合/公式逻辑,这些方法在相关用例里被 monkeypatch 成受控返回。腿提取本身经
`_leg_from_position`(duck position + 真实 instrument 入 cache)单独验证。
"""

import pytest

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.risk.portfolio import ArbitragePortfolio
from src.arbitrage.risk.portfolio import _Leg
from tests.arbitrage.risk._factories import DuckPosition
from tests.arbitrage.risk._factories import oe_instrument
from tests.arbitrage.risk._factories import pm_instrument


def _portfolio(cache=None) -> ArbitragePortfolio:
    clock = TestClock()
    cache = cache or TestComponentStubs.cache()
    pf = ArbitragePortfolio(
        msgbus=MessageBus(trader_id=TraderId("T-000"), clock=clock),
        cache=cache,
        clock=clock,
    )
    pf.configure_arb(share=100.0)
    return pf


# ── 公式(risk-6.9.2b / 6.9.2c / 6.9.2d)──────────────────────────────
def test_compute_outcome_exposures_net_profit_and_liability():
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4), _Leg("orbitexch", "away", 40, 2.5)]
    exposures = pf._compute_outcome_exposures(legs)
    assert exposures["home"].net_profit == pytest.approx(20.0)
    assert exposures["home"].liability == pytest.approx(40.0)
    assert exposures["away"].net_profit == pytest.approx(20.0)
    assert exposures["away"].liability == pytest.approx(40.0)


def test_compute_outcome_exposures_three_way_adds_draw():
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4), _Leg("polymarket", "draw", 50, 0.3)]
    exposures = pf._compute_outcome_exposures(legs)
    assert set(exposures.keys()) == {"home", "draw", "away"}
    assert exposures["away"].net_profit == pytest.approx(-55.0)
    assert exposures["away"].liability == pytest.approx(55.0)


def test_empty_legs_returns_empty_outcome_exposures():
    assert _portfolio()._compute_outcome_exposures([]) == {}


def test_outcome_exposures_uses_registered_outcomes_even_without_position():
    from src.arbitrage.common.pair_registry import PairRegistry

    cache = TestComponentStubs.cache()
    home = pm_instrument("match_1", "home", token="home")
    draw = pm_instrument("match_1", "draw", token="draw")
    away = pm_instrument("match_1", "away", token="away")
    cache.add_instrument(home)
    cache.add_instrument(draw)
    cache.add_instrument(away)

    pf = _portfolio(cache=cache)
    registry = PairRegistry()
    registry.register("match_1", [home.id, draw.id, away.id])
    pf.configure_arb(pair_registry=registry)
    _stub_legs(
        pf,
        [
            _Leg("polymarket", "home", 100, 0.4),
            _Leg("polymarket", "away", 100, 0.4),
        ],
    )

    exposures = pf.outcome_exposures("match_1")
    assert set(exposures.keys()) == {"home", "draw", "away"}
    assert exposures["draw"].net_profit == pytest.approx(-80.0)
    assert exposures["draw"].liability == pytest.approx(80.0)


def test_outcome_shares_aggregates_by_outcome():
    pf = _portfolio()
    _stub_legs(
        pf,
        [
            _Leg("polymarket", "home", 5, 0.4),
            _Leg("orbitexch", "home", 3, 2.0),
            _Leg("orbitexch", "away", 4, 2.5),
        ],
    )

    shares = pf.outcome_shares("match_1")

    assert shares["home"] == pytest.approx(11.0)
    assert shares["away"] == pytest.approx(10.0)


# ── 腿提取 seam(risk-6.9.6)──────────────────────────────────────────
def test_leg_from_position_pm_and_oe():
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    oe = oe_instrument("match_1", "away", 2)
    cache.add_instrument(pm)
    cache.add_instrument(oe)
    pf = _portfolio(cache=cache)

    leg_pm = pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))
    assert leg_pm.venue == "polymarket" and leg_pm.market_type == "home"
    assert leg_pm.size == pytest.approx(100.0) and leg_pm.price == pytest.approx(0.4)

    leg_oe = pf._leg_from_position(DuckPosition(oe.id, 50.0, 2.5))
    assert leg_oe.venue == "orbitexch" and leg_oe.market_type == "away"


def test_leg_from_position_missing_info_returns_none():
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "")  # market_type 空
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    assert pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4)) is None


def test_leg_from_position_prefers_selection_role_q9_key():
    """#34/35:Q9 标准 key 是 selection_role;Provider 写它,本类优先读它。"""
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    # 模拟 Provider(matching/discovery)用 Q9 标准 selection_role 而非旧 market_type
    pm.info.update({"selection_role": "draw"})
    pm.info.pop("market_type", None)
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    leg = pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))
    assert leg is not None and leg.market_type == "draw"


def test_leg_from_position_falls_back_to_market_type():
    """selection_role 缺、market_type 在 → 用 market_type(向后兼容旧 _factories 风格)。"""
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "away")   # _factories 设的 info["market_type"]="away"
    pm.info.pop("selection_role", None)     # 显式去掉新 key
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    leg = pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))
    assert leg is not None and leg.market_type == "away"


def test_resolve_pair_id_reads_from_pair_registry():
    """#34: pair_id 经 PairRegistry(matching 写),不再读 info["competition"]。"""
    from src.arbitrage.common.pair_registry import PairRegistry
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_42", "home")
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    # 没注册时返回 None(下游组合指标自然不参与该腿)
    assert pf._resolve_pair_id(DuckPosition(pm.id, 100.0, 0.4)) is None
    # 注册后正常返
    registry = PairRegistry(); registry.register("match_42", [pm.id])
    pf.configure_arb(pair_registry=registry)
    assert pf._resolve_pair_id(DuckPosition(pm.id, 100.0, 0.4)) == "match_42"


def _stub_legs(pf, legs):
    pf._legs_for_pair = lambda pair_id, account_id=None: legs
