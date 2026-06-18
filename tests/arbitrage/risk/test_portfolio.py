"""ArbitragePortfolio way_rebate + 全局聚合(risk-6.9.x)。

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
    pf.configure_arb(share=100.0, fx=1.0)
    return pf


# ── 公式(risk-6.9.2 / 6.9.4 / 6.9.7)─────────────────────────────────
def test_compute_way_rebate_equal_share_matches_mean_rebate_shape():
    # mean_rebate 下单形态:PM share=100;OE stake=100/2.5=40 → 两腿赢时 share 都是 100
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4, 1.0), _Leg("orbitexch", "away", 40, 2.5, 1.0)]
    r = pf._compute_way_rebate(legs)
    assert r["home"] == pytest.approx(0.20)
    assert r["away"] == pytest.approx(0.20)
    assert min(r.values()) == pytest.approx(0.20)  # min_way_rebate 等价


def test_compute_way_rebate_uses_largest_actual_leg_share():
    # PM 实际 share=100;OE 实际 share=50*2.5=125 → 用最大腿 share=125 归一化
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4, 1.0), _Leg("orbitexch", "away", 50, 2.5, 1.0)]
    r = pf._compute_way_rebate(legs)
    assert r["home"] == pytest.approx(0.08)
    assert r["away"] == pytest.approx(0.28)
    assert min(r.values()) == pytest.approx(0.08)


def test_compute_way_rebate_three_way_adds_draw():
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4, 1.0), _Leg("polymarket", "draw", 50, 0.3, 1.0)]
    r = pf._compute_way_rebate(legs)
    assert set(r.keys()) == {"home", "draw", "away"}


def test_empty_legs_returns_empty():
    assert _portfolio()._compute_way_rebate([]) == {}


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
    assert leg_oe.venue == "orbitexch" and leg_oe.market_type == "away" and leg_oe.fx == 1.0


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


def test_way_rebate_always_computes_from_positions_without_liveness_gate():
    pf = _portfolio()
    _stub_legs(pf, [_Leg("polymarket", "home", 100, 0.4, 1.0)])
    assert pf.way_rebate("match_X") != {}
    assert pf.min_way_rebate("match_X") is not None
    assert pf.way_rebates_by_venue("match_X") != {}


# ── 全局聚合(risk-6.9.5 / 6.9.5b / 6.9.12)──────────────────────────
def test_global_min_rebate_sum_only_active_pairs():
    pf = _portfolio()
    pf._active_pair_ids = lambda account_id=None: {"match_X", "match_Y"}
    mins = {"match_X": 0.10, "match_Y": 0.05}
    pf.min_way_rebate = lambda pair_id, account_id=None: mins[pair_id]
    assert pf.global_min_rebate_sum() == pytest.approx(0.15)


def test_global_min_rebate_sum_untraded_match_not_in_scan():
    # active scan 只含有持仓的 pair;未交易比赛压根不在 _active_pair_ids → 不致 None
    pf = _portfolio()
    pf._active_pair_ids = lambda account_id=None: {"match_X"}
    pf.min_way_rebate = lambda pair_id, account_id=None: 0.10
    assert pf.global_min_rebate_sum() == pytest.approx(0.10)


def test_global_min_rebate_sum_does_not_carry_liveness_gate():
    pf = _portfolio()
    pf._active_pair_ids = lambda account_id=None: {"match_X", "match_Y"}
    pf.min_way_rebate = lambda pair_id, account_id=None: 0.10
    assert pf.global_min_rebate_sum() == pytest.approx(0.20)
