"""TrendGateAction:按 pair 级跨 venue/outcome 一致的价格趋势过滤 leg(#329)。

一致判据:某 outcome 所有 venue 腿 Δ≥0(up/flat)、互斥 outcome 所有 venue 腿 Δ≤0(down/flat)、
至少一处严格移动 → 该 outcome 为"干净上升"。任一 venue 反向 → 无干净趋势 → 不过滤。
"""

import asyncio

import pytest

from src.arbitrage.strategy.actions.trend_gate import TrendGateAction
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


# pair 的全部 tradable 腿(判一致性用):PM yes/no + OE yes/no
_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
    "Y.ORBITEXCH": {"claim": "yes"},
    "N.ORBITEXCH": {"claim": "no"},
}


def _ctx(trend):
    return live_context(infos=_INFOS, instrument_ids=list(_INFOS), price_trend=trend)


def _install(ctx, legs):
    ctx.scratch["selected_candidate"] = {"candidate_id": "chosen", "rate": 0.1, "legs": legs}
    ctx.scratch["legs"] = legs


def _pm_legs():
    return [
        {"instrument_id": "Y.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "N.POLYMARKET", "side": "BUY", "claim": "no"},
    ]


def test_up_keeps_rising_outcome_when_all_venues_agree():
    # yes 各 venue 都涨/平,no 各 venue 都跌/平 → yes 干净上升
    trend = {"Y.POLYMARKET": 0.02, "Y.ORBITEXCH": 0.01, "N.POLYMARKET": -0.03, "N.ORBITEXCH": 0.0}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction().execute(ctx))  # 默认 up

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]  # 只留 yes
    assert ctx.scratch["selected_candidate"]["rate"] == 0.1


def test_down_keeps_falling_outcome():
    trend = {"Y.POLYMARKET": 0.02, "Y.ORBITEXCH": 0.01, "N.POLYMARKET": -0.03, "N.ORBITEXCH": 0.0}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction(trend="down").execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["N.POLYMARKET"]  # 留下降的 no


def test_inconsistent_across_venues_drops_all():
    # OE 的 yes 跌,PM 的 yes 涨 → yes 不一致 → 无干净趋势 → 没有腿符合 → 全删
    trend = {"Y.POLYMARKET": 0.02, "Y.ORBITEXCH": -0.02, "N.POLYMARKET": -0.03, "N.ORBITEXCH": 0.0}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []
    assert ctx.scratch["selected_candidate"]["legs"] == []


def test_missing_venue_data_treated_as_flat():
    # OE 两腿无趋势数据(当 flat),PM:yes 涨 no 跌 → 仍是 yes 干净上升
    trend = {"Y.POLYMARKET": 0.02, "N.POLYMARKET": -0.03}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]


def test_steps_absent_keeps_existing_coherence_behavior():
    trend = {"Y.POLYMARKET": 0.001, "N.POLYMARKET": -0.001}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]


def test_steps_keeps_coherent_trend_when_all_leg_absolute_momentum_reaches_threshold():
    trend = {
        "Y.POLYMARKET": 0.02,
        "Y.ORBITEXCH": 0.01,
        "N.POLYMARKET": -0.03,
        "N.ORBITEXCH": 0.0,
    }
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction(steps=0.06).execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y.POLYMARKET"]


def test_steps_drops_all_when_all_leg_absolute_momentum_is_below_threshold():
    trend = {
        "Y.POLYMARKET": 0.02,
        "Y.ORBITEXCH": 0.01,
        "N.POLYMARKET": -0.03,
        "N.ORBITEXCH": 0.0,
    }
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction(steps=0.061).execute(ctx))

    assert ctx.scratch["legs"] == []
    assert ctx.scratch["selected_candidate"]["legs"] == []


def test_all_flat_drops_all():
    # 全平(含缺数据)→ 无严格移动 → 无趋势 → 全删
    trend = {"Y.POLYMARKET": 0.0, "N.POLYMARKET": 0.0}
    ctx = _ctx(trend)
    _install(ctx, _pm_legs())

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_no_price_trend_drops_all():
    # 未接入 / 未预热(无任何趋势数据)→ 无干净趋势 → 全删
    for tr in (None, {}):
        ctx = _ctx(tr)
        _install(ctx, _pm_legs())
        _run(TrendGateAction().execute(ctx))
        assert ctx.scratch["legs"] == []


def test_noop_without_selected_candidate():
    ctx = _ctx({"Y.POLYMARKET": 0.02, "N.POLYMARKET": -0.02})
    _run(TrendGateAction().execute(ctx))
    assert "legs" not in ctx.scratch


def test_skips_cancel_pair_candidate():
    ctx = _ctx({"Y.POLYMARKET": 0.02, "N.POLYMARKET": -0.02})
    cand = {"candidate_id": "c", "cancel_pair_orders": True, "legs": _pm_legs()}
    ctx.scratch["selected_candidate"] = cand
    ctx.scratch["legs"] = cand["legs"]

    _run(TrendGateAction().execute(ctx))

    assert ctx.scratch["selected_candidate"] is cand  # 未改


def test_invalid_trend_param_raises():
    with pytest.raises(ValueError):
        TrendGateAction(trend="sideways")


@pytest.mark.parametrize("steps", [-0.01, float("nan"), float("inf")])
def test_invalid_steps_param_raises(steps):
    with pytest.raises(ValueError, match="steps must be"):
        TrendGateAction(steps=steps)
