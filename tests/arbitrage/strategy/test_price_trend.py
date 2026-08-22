"""Pair trend_price 基准采样与 OBD 顶价变化检测。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.actor import StrategyEvaluator
from tests.arbitrage.strategy._live_state import StrategyTestCache


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
    "Y.ORBITEXCH": {"claim": "yes"},
    "N.ORBITEXCH": {"claim": "no"},
}


def _evaluator(books, instrument_ids=None):
    ids = list(instrument_ids or _INFOS)
    cache = StrategyTestCache(books=books, infos={iid: _INFOS[iid] for iid in ids})
    registry = PairRegistry()
    registry.register("p", ids)
    store = PairPriceStore(cache)
    store.initialize("p", ["yes", "no"])
    return SimpleNamespace(
        cache=cache,
        _pair_registry=registry,
        _pair_price_store=store,
        _last_best_ask={},
        _get_pair_price_store=lambda: store,
    )


def _deltas(iid="Y.POLYMARKET"):
    return SimpleNamespace(instrument_id=iid)


def test_top_ask_detector_only_reports_real_price_changes():
    actor = _evaluator({"Y.POLYMARKET": {"ask": 0.40}}, ["Y.POLYMARKET"])

    assert StrategyEvaluator._top_ask_changed(actor, _deltas()) is False
    assert StrategyEvaluator._top_ask_changed(actor, _deltas()) is False
    actor.cache._books["Y.POLYMARKET"] = {"ask": 0.41}
    assert StrategyEvaluator._top_ask_changed(actor, _deltas()) is True
    assert actor._last_best_ask["Y.POLYMARKET"] == pytest.approx(0.41)


def test_trend_price_uses_cross_venue_best_probability_for_each_outcome():
    actor = _evaluator({
        "Y.POLYMARKET": {"ask": 0.46},
        "N.POLYMARKET": {"ask": 0.58},
        "Y.ORBITEXCH": {"ask": 0.44},
        "N.ORBITEXCH": {"ask": 0.57},
    })

    StrategyEvaluator._update_trend_price(actor, _deltas())

    assert actor._pair_price_store.get("p").trend_price == {"yes": 0.44, "no": 0.57}


@pytest.mark.parametrize(
    ("yes", "no"),
    [
        (0.50, 0.50),
        (0.50, 0.55),
        (0.50, 0.56),
    ],
)
def test_trend_price_does_not_update_outside_strict_commission_window(yes, no):
    actor = _evaluator({
        "Y.POLYMARKET": {"ask": yes},
        "N.POLYMARKET": {"ask": no},
        "Y.ORBITEXCH": {"ask": yes},
        "N.ORBITEXCH": {"ask": no},
    })
    actor._pair_price_store.update_trend("p", {"yes": 0.45, "no": 0.57})

    StrategyEvaluator._update_trend_price(actor, _deltas())

    assert actor._pair_price_store.get("p").trend_price == {"yes": 0.45, "no": 0.57}


def test_trend_price_requires_complete_outcome_quotes():
    actor = _evaluator({"Y.POLYMARKET": {"ask": 0.45}}, ["Y.POLYMARKET"])

    StrategyEvaluator._update_trend_price(actor, _deltas())

    assert actor._pair_price_store.get("p").trend_price == {}
