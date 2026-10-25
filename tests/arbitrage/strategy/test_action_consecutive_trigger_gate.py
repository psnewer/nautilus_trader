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
    ctx.scratch["candidates"] = [
        {
            "candidate_id": f"one_side:{outcome}",
            "rate": 0.12,
            "legs": [
                {
                    "instrument_id": "A.POLYMARKET",
                    "venue": "POLYMARKET",
                    "side": "BUY",
                    "claim": outcome,
                    "price": 0.42,
                    "prob": 0.42,
                }
            ],
        }
    ]
    return ctx


def _run(ctx, *, required_hits=2, limit=None, cross_set=None):
    asyncio.run(
        ConsecutiveTriggerGateAction(
            history_key="pre_rebate",
            required_hits=required_hits,
            limit=limit,
            cross_set=cross_set,
        ).execute(ctx),
    )


@pytest.mark.parametrize("limit", [None, False])
def test_second_same_direction_at_changed_score_passes_and_keeps_both_records(limit):
    store = ConsecutiveTriggerStore()
    first = _ctx(score="0-1", store=store)
    _run(first, limit=limit)
    assert first.scratch["candidates"] == []

    second = _ctx(score="0-2", store=store)
    original = second.scratch["candidates"]
    _run(second, limit=limit)

    assert second.scratch["candidates"] == original
    original[0]["legs"][0]["price"] = 0.99
    history = store.history("p", "pre_rebate")
    assert [value.score_raw for value in history] == ["0-1", "0-2"]
    assert [value.passed for value in history] == [False, True]
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


def test_duplicate_candidates_for_same_outcome_do_not_change_continuity():
    store = ConsecutiveTriggerStore()
    first = _ctx(score="0-1", store=store)
    first.scratch["candidates"] *= 2
    _run(first)

    second = _ctx(score="0-2", store=store)
    second.scratch["candidates"] *= 2
    original = second.scratch["candidates"]
    _run(second)

    assert second.scratch["candidates"] == original
    history = store.history("p", "pre_rebate")
    assert [value.outcome for value in history] == ["yes", "yes"]
    assert [value.passed for value in history] == [False, True]


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


def test_limit_requires_elapsed_games_to_reach_required_hits():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="0-1", store=store), limit=True)

    one_game_later = _ctx(score="0-2", store=store)
    _run(one_game_later, limit=True)
    assert one_game_later.scratch["candidates"] == []

    two_games_later = _ctx(score="0-3", store=store)
    _run(two_games_later, limit=True)
    assert two_games_later.scratch["candidates"]


def test_limit_stops_at_set_boundary_then_counts_within_current_set():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="5-4", store=store), limit=True)

    new_set = _ctx(score="6-4, 0-1", store=store)
    _run(new_set, limit=True)
    assert new_set.scratch["candidates"] == []

    two_games_later = _ctx(score="6-4, 0-3", store=store)
    _run(two_games_later, limit=True)
    assert two_games_later.scratch["candidates"]


def test_limit_cross_set_counts_regular_games_across_set_boundary():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="5-4", store=store), limit=True, cross_set=True)

    current = _ctx(score="6-4, 1-0", store=store)
    _run(current, limit=True, cross_set=True)

    assert current.scratch["candidates"]


def test_limit_cross_set_does_not_count_tie_break_game():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="6-5", store=store), limit=True, cross_set=True)

    set_finished = _ctx(score="7-6(7-4), 0-0", store=store)
    _run(set_finished, limit=True, cross_set=True)
    assert set_finished.scratch["candidates"] == []

    one_regular_game_later = _ctx(score="7-6(7-4), 1-0", store=store)
    _run(one_regular_game_later, limit=True, cross_set=True)
    assert one_regular_game_later.scratch["candidates"]


@pytest.mark.parametrize("score", ["6-6", "6-6(0-0)", "6-6(5-4)"])
def test_tie_break_score_fails_closed_without_record(score):
    store = ConsecutiveTriggerStore()
    current = _ctx(score=score, store=store)
    _run(current, limit=True)

    assert current.scratch["candidates"] == []
    assert store.history("p", "pre_rebate") == ()


def test_limit_does_not_count_tie_break_game_itself():
    store = ConsecutiveTriggerStore()
    _run(_ctx(score="6-5", store=store), limit=True)

    current = _ctx(score="7-6(7-4)", store=store)
    _run(current, limit=True)

    assert current.scratch["candidates"] == []


def test_limit_uses_required_hits_as_minimum_elapsed_games():
    store = ConsecutiveTriggerStore()
    for score in ("0-1", "0-2", "0-3"):
        ctx = _ctx(score=score, store=store)
        _run(ctx, required_hits=3, limit=True)
        assert ctx.scratch["candidates"] == []

    current = _ctx(score="0-4", store=store)
    _run(current, required_hits=3, limit=True)
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
    ("history_key", "required_hits", "limit", "cross_set"),
    [
        ("", 2, None, None),
        ("x", 1, None, None),
        ("x", True, None, None),
        ("x", 2, 1, None),
        ("x", 2, "true", None),
        ("x", 2, None, 1),
        ("x", 2, None, "true"),
    ],
)
def test_invalid_params_fail_fast(history_key, required_hits, limit, cross_set):
    with pytest.raises(ValueError):
        ConsecutiveTriggerGateAction(
            history_key=history_key,
            required_hits=required_hits,
            limit=limit,
            cross_set=cross_set,
        )
