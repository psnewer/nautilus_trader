"""ArbitragePortfolio outcome exposure / outcome share 聚合(risk-6.9.x)。

腿提取(`_legs_for_pair` / `_active_pair_ids`)依赖 cache 中的真实 NT Position;为隔离
聚合/公式逻辑,这些方法在相关用例里被 monkeypatch 成受控返回。腿提取本身经
`_leg_from_position`(duck position + 真实 instrument 入 cache)单独验证。
"""

import pytest

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from src.arbitrage.common.realized_pnl import RealizedPnlLedger
from src.arbitrage.common.venues import PositionOutcomeInvariantError
from src.arbitrage.risk.portfolio import ArbitragePortfolio
from src.arbitrage.risk.portfolio import _Leg
from tests.arbitrage.risk._factories import DuckPosition
from tests.arbitrage.risk._factories import oe_instrument
from tests.arbitrage.risk._factories import pm_instrument
from tests.arbitrage.risk._factories import se_instrument


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
    legs = [_Leg("polymarket", "yes", 100, 0.4), _Leg("orbitexch", "no", 40, 2.5)]
    exposures = pf._compute_outcome_exposures(legs)
    assert exposures["yes"].net_profit == pytest.approx(20.0)
    assert exposures["yes"].liability == pytest.approx(40.0)
    assert exposures["no"].net_profit == pytest.approx(20.0)
    assert exposures["no"].liability == pytest.approx(40.0)


def test_compute_outcome_exposures_rejects_legacy_outcomes():
    pf = _portfolio()
    legs = [_Leg("polymarket", "home", 100, 0.4), _Leg("polymarket", "draw", 50, 0.3)]
    with pytest.raises(PositionOutcomeInvariantError, match="yes/no"):
        pf._compute_outcome_exposures(legs)


def test_empty_legs_returns_empty_outcome_exposures():
    assert _portfolio()._compute_outcome_exposures([]) == {}


def test_outcome_exposures_uses_registered_outcomes_even_without_position():
    from src.arbitrage.common.pair_registry import PairRegistry

    cache = TestComponentStubs.cache()
    yes = pm_instrument("match_1", "home", token="yes")
    yes.info["claim"] = "yes"
    no = pm_instrument("match_1", "home", token="no")
    no.info["claim"] = "no"
    cache.add_instrument(yes)
    cache.add_instrument(no)

    pf = _portfolio(cache=cache)
    registry = PairRegistry()
    registry.register("match_1", [yes.id, no.id])
    pf.configure_arb(pair_registry=registry)
    _stub_legs(
        pf,
        [
            _Leg("polymarket", "yes", 100, 0.4),
        ],
    )

    exposures = pf.outcome_exposures("match_1")
    assert set(exposures.keys()) == {"yes", "no"}
    assert exposures["no"].net_profit == pytest.approx(-40.0)
    assert exposures["no"].liability == pytest.approx(40.0)


def test_outcome_exposures_adds_reconciled_realized_pnl_to_all_outcomes():
    from src.arbitrage.common.pair_registry import PairRegistry

    cache = TestComponentStubs.cache()
    yes = pm_instrument("match_1", "home", token="yes")
    yes.info.update({"claim": "yes"})
    no = pm_instrument("match_1", "home", token="no")
    no.info.update({"claim": "no"})
    cache.add_instrument(yes)
    cache.add_instrument(no)

    registry = PairRegistry()
    registry.register("match_1", [yes.id, no.id])
    ledger = RealizedPnlLedger()
    ledger.replace_instrument_snapshot(
        "POLYMARKET-001",
        external_realized={str(yes.id): 2.0},
        native_realized={str(yes.id): 0.0},
    )
    pf = _portfolio(cache=cache)
    pf.configure_arb(pair_registry=registry, realized_pnl_ledger=ledger)
    _stub_legs(pf, [_Leg("polymarket", "yes", 10.0, 0.40)])

    exposures = pf.outcome_exposures("match_1")

    assert exposures["yes"].net_profit == pytest.approx(8.0)
    assert exposures["no"].net_profit == pytest.approx(-2.0)
    assert exposures["no"].liability == pytest.approx(4.0)


def test_outcome_exposures_excludes_realized_pnl_when_flag_false():
    """#327:include_realized_pnl=False → 返回不含 realized 的开仓投影(供 recovery pnl=False)。"""
    from src.arbitrage.common.pair_registry import PairRegistry

    cache = TestComponentStubs.cache()
    yes = pm_instrument("match_1", "home", token="yes")
    yes.info.update({"claim": "yes"})
    no = pm_instrument("match_1", "home", token="no")
    no.info.update({"claim": "no"})
    cache.add_instrument(yes)
    cache.add_instrument(no)

    registry = PairRegistry()
    registry.register("match_1", [yes.id, no.id])
    ledger = RealizedPnlLedger()
    ledger.replace_instrument_snapshot(
        "POLYMARKET-001",
        external_realized={str(yes.id): 2.0},
        native_realized={str(yes.id): 0.0},
    )
    pf = _portfolio(cache=cache)
    pf.configure_arb(pair_registry=registry, realized_pnl_ledger=ledger)
    _stub_legs(pf, [_Leg("polymarket", "yes", 10.0, 0.40)])

    with_realized = pf.outcome_exposures("match_1")
    without = pf.outcome_exposures("match_1", include_realized_pnl=False)

    assert with_realized["yes"].net_profit == pytest.approx(8.0)   # 含 realized 2.0
    assert without["yes"].net_profit == pytest.approx(6.0)         # 仅开仓投影
    assert without["no"].net_profit == pytest.approx(-4.0)
    assert without["no"].liability == pytest.approx(4.0)           # liability 不受 flag 影响


def test_realized_pnl_for_pair_exposes_existing_aggregate(monkeypatch):
    pf = _portfolio()
    monkeypatch.setattr(pf, "_realized_pnl_for_pair", lambda pair_id, account_id=None: 1.25)

    assert pf.realized_pnl_for_pair("match_1") == pytest.approx(1.25)


def test_outcome_shares_aggregates_by_outcome():
    pf = _portfolio()
    _stub_legs(
        pf,
        [
            _Leg("polymarket", "yes", 5, 0.4),
            _Leg("orbitexch", "yes", 3, 2.0),
            _Leg("orbitexch", "no", 4, 2.5),
        ],
    )

    shares = pf.outcome_shares("match_1")

    assert shares["yes"] == pytest.approx(11.0)
    assert shares["no"] == pytest.approx(10.0)


# ── 腿提取 seam(risk-6.9.6)──────────────────────────────────────────
def test_leg_from_position_pm_and_oe():
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    pm.info["claim"] = "yes"
    oe = oe_instrument("match_1", "away", 2)
    oe.info["claim"] = "no"
    cache.add_instrument(pm)
    cache.add_instrument(oe)
    pf = _portfolio(cache=cache)

    leg_pm = pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))
    assert leg_pm.venue == "polymarket" and leg_pm.market_type == "yes"
    assert leg_pm.size == pytest.approx(100.0) and leg_pm.price == pytest.approx(0.4)

    leg_oe = pf._leg_from_position(DuckPosition(oe.id, 50.0, 2.5))
    assert leg_oe.venue == "orbitexch" and leg_oe.market_type == "no"


def test_leg_from_position_sharpexch_keeps_venue_identity():
    cache = TestComponentStubs.cache()
    se = se_instrument("match_1", "away", 3)
    se.info["claim"] = "no"
    cache.add_instrument(se)
    pf = _portfolio(cache=cache)

    leg = pf._leg_from_position(DuckPosition(se.id, 50.0, 2.5))

    assert leg.venue == "sharpexch"
    assert leg.market_type == "no"
    assert leg.share_if_wins() == pytest.approx(125.0)


def test_outcome_shares_for_venue_keeps_oe_and_se_separate():
    pf = _portfolio()
    _stub_legs(
        pf,
        [
            _Leg("orbitexch", "yes", 3, 2.0),
            _Leg("sharpexch", "yes", 4, 2.5),
        ],
    )

    assert pf.outcome_shares_for_venue("match_1", "orbitexch")["yes"] == pytest.approx(6.0)
    assert pf.outcome_shares_for_venue("match_1", "sharpexch")["yes"] == pytest.approx(10.0)


def test_claim_aware_positions_include_pm_no_long_and_decimal_lay_short():
    cache = TestComponentStubs.cache()
    pm_yes = pm_instrument("match_1", "home", token="yes")
    pm_yes.info.update({"selection_role": "home", "claim": "yes"})
    pm_no = pm_instrument("match_1", "home", token="no")
    pm_no.info.update({"selection_role": "home", "claim": "no"})
    oe_yes = oe_instrument("match_1", "home", 2)
    oe_yes.info.update({"selection_role": "home", "claim": "yes"})
    for instrument in (pm_yes, pm_no, oe_yes):
        cache.add_instrument(instrument)

    pf = _portfolio(cache=cache)
    from src.arbitrage.common.pair_registry import PairRegistry

    registry = PairRegistry()
    registry.register("match_1", [pm_yes.id, pm_no.id, oe_yes.id])
    pf.configure_arb(pair_registry=registry)
    positions = [
        DuckPosition(pm_yes.id, 10.0, 0.4),
        DuckPosition(pm_no.id, 10.0, 0.3),
        DuckPosition(oe_yes.id, 4.0, 2.5, side=PositionSide.SHORT),
    ]
    legs = [
        pf._leg_from_position(position, outcomes={"yes", "no"})
        for position in positions
    ]
    assert all(leg is not None for leg in legs)
    _stub_legs(pf, legs)

    exposures = pf.outcome_exposures("match_1")
    shares = pf.outcome_shares("match_1")

    assert exposures["yes"].net_profit == pytest.approx(-3.0)
    assert exposures["yes"].liability == pytest.approx(9.0)
    assert exposures["no"].net_profit == pytest.approx(7.0)
    assert exposures["no"].liability == pytest.approx(4.0)
    assert shares == pytest.approx({"yes": 10.0, "no": 20.0})
    assert pf.outcome_shares_for_venue("match_1", "orbitexch")["no"] == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("instrument_factory", "expected_venue"),
    [(oe_instrument, "orbitexch"), (se_instrument, "sharpexch")],
)
def test_decimal_short_in_two_way_pair_maps_to_other_claim(
    instrument_factory,
    expected_venue,
):
    cache = TestComponentStubs.cache()
    instrument = instrument_factory("match_1", "home", 2)
    instrument.info.update({"selection_role": "home", "claim": "yes"})
    cache.add_instrument(instrument)
    pf = _portfolio(cache=cache)

    leg = pf._leg_from_position(
        DuckPosition(instrument.id, 4.0, 2.5, side=PositionSide.SHORT),
        outcomes={"yes", "no"},
    )

    assert leg is not None
    assert leg.venue == expected_venue
    assert leg.market_type == "no"
    assert leg.share_if_wins() == pytest.approx(10.0)
    assert leg.profit_if_wins() == pytest.approx(4.0)
    assert leg.loss_if_loses() == pytest.approx(6.0)


def test_leg_from_position_missing_claim_fails_closed():
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "")  # market_type 空
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    with pytest.raises(PositionOutcomeInvariantError, match="missing claim"):
        pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))


def test_leg_from_position_probability_short_fails_closed():
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    pm.info["claim"] = "yes"
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)

    with pytest.raises(PositionOutcomeInvariantError, match="cannot be SHORT"):
        pf._leg_from_position(
            DuckPosition(pm.id, 10.0, 0.4, side=PositionSide.SHORT),
            outcomes={"yes", "no"},
        )


def test_leg_from_position_dust_short_treated_as_flat():
    # #307:venue 撮合误差 dust —— reduce 多卖成 -0.02 空头(≤ dust)不该触发概率盘
    # "cannot be SHORT",视为 flat 跳过(返回 None)。
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    pm.info["claim"] = "yes"
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)

    leg = pf._leg_from_position(
        DuckPosition(pm.id, 0.02, 0.4, side=PositionSide.SHORT),
        outcomes={"yes", "no"},
    )
    assert leg is None


def test_leg_from_position_dust_long_treated_as_flat():
    # #307 对称:reduce 少卖留 +0.02 多头(≤ dust)也当 flat 跳过,不作真实腿计入。
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    pm.info["claim"] = "yes"
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)

    leg = pf._leg_from_position(
        DuckPosition(pm.id, 0.02, 0.4, side=PositionSide.LONG),
        outcomes={"yes", "no"},
    )
    assert leg is None


def test_leg_from_position_uses_claim_not_selection_role():
    """统一 outcome 后 selection_role 只用于匹配和展示。"""
    cache = TestComponentStubs.cache()
    pm = pm_instrument("match_1", "home")
    # 模拟 Provider(matching/discovery)用 Q9 标准 selection_role 而非旧 market_type
    pm.info.update({"selection_role": "draw", "claim": "yes"})
    pm.info.pop("market_type", None)
    cache.add_instrument(pm)
    pf = _portfolio(cache=cache)
    leg = pf._leg_from_position(DuckPosition(pm.id, 100.0, 0.4))
    assert leg is not None and leg.market_type == "yes"


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
