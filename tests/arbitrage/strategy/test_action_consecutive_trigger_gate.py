"""连续触发门保存完整历史，并按最近连续方向和比分变化放行。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.consecutive_trigger_gate import ConsecutiveTriggerGateAction
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from tests.arbitrage.strategy._live_state import live_context


class _SportsStore:
    def __init__(self, score):
        self.score = score

    def get(self, _game_id):
        return SimpleNamespace(score=self.score)


def _ctx(*, score="0-1", outcome="yes", store=None, event_name="MarketOrderBookDeltas"):
    ctx = live_context(
        pair_id="p",
        infos={"A.POLYMARKET": {"claim": outcome}},
        instrument_ids=["A.POLYMARKET"],
        sports_store=_SportsStore(score),
        consecutive_trigger_store=store or ConsecutiveTriggerStore(),
        event_name=event_name,
        ts_now_ns=123,
    )
    ctx.pair_registry.register("p", ["A.POLYMARKET"], game_id=7)
    ctx.scratch["candidates"] = [{
        "candidate_id": f"one_side:{outcome}",
        "rate": 0.12,
        "legs": [{
            "instrument_id": "A.POLYMARKET",
            "venue": "POLYMARKET",
            "side": "BUY",
            "claim": outcome,
            "price": 0.42,
            "prob": 0.42,
        }],
    }]
    return ctx


def _run(ctx, *, required_hits=2):
    asyncio.run(
        ConsecutiveTriggerGateAction(
            history_key="pre_rebate",
            required_hits=required_hits,
        ).execute(ctx),
    )


def test_second_same_direction_at_changed_score_passes_and_keeps_both_records():
    store = ConsecutiveTriggerStore()
    first = _ctx(score="0-1", store=store)
    _run(first)
    assert first.scratch["candidates"] == []

    second = _ctx(score="0-2", store=store)
    original = second.scratch["candidates"]
    _run(second)

    assert second.scratch["candidates"] == original
    original[0]["legs"][0]["price"] = 0.99
    history = store.history("p", "pre_rebate")
    assert [value.score_raw for value in history] == ["0-1", "0-2"]
    assert history[0].legs[0].price == 0.42
    assert history[1].legs[0].price == 0.42
    assert history[0].candidate_ids == ("one_side:yes",)


def test_same_score_duplicates_are_saved_but_do_not_count():
    store = ConsecutiveTriggerStore()
    for _ in range(2):
        ctx = _ctx(score="0-1", store=store)
        _run(ctx)
        assert ctx.scratch["candidates"] == []

    changed = _ctx(score="0-2", store=store)
    _run(changed)
    assert changed.scratch["candidates"]
    assert len(store.history("p", "pre_rebate")) == 3


def test_opposite_direction_breaks_recent_sequence():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="0-1", outcome="yes", store=store))
    _run(_ctx(score="0-2", outcome="no", store=store))
    current = _ctx(score="0-3", outcome="yes", store=store)
    _run(current)

    assert current.scratch["candidates"] == []
    assert [value.outcome for value in store.history("p", "pre_rebate")] == ["yes", "no", "yes"]


def test_required_hits_walks_recent_history_and_ignores_same_score_duplicates():
    store = ConsecutiveTriggerStore()
    for score in ("0-1", "0-1", "0-2"):
        _run(_ctx(score=score, store=store), required_hits=3)
    current = _ctx(score="0-3", store=store)
    _run(current, required_hits=3)

    assert current.scratch["candidates"]


@pytest.mark.parametrize("score", ["", "bad score"])
def test_missing_or_invalid_score_fails_closed_without_record(score):
    store = ConsecutiveTriggerStore()
    ctx = _ctx(score=score, store=store)
    _run(ctx)

    assert ctx.scratch["candidates"] == []
    assert store.history("p", "pre_rebate") == ()


def test_non_obd_event_fails_closed_without_record():
    store = ConsecutiveTriggerStore()
    ctx = _ctx(store=store, event_name="MatchedPair")
    _run(ctx)

    assert ctx.scratch["candidates"] == []
    assert store.history("p", "pre_rebate") == ()


@pytest.mark.parametrize(
    ("history_key", "required_hits"),
    [("", 2), ("x", 1), ("x", True)],
)
def test_invalid_params_fail_fast(history_key, required_hits):
    with pytest.raises(ValueError):
        ConsecutiveTriggerGateAction(history_key=history_key, required_hits=required_hits)
