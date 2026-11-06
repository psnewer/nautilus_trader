"""TriggerCountAction: 只保留当前盘去重触发次数唯一最多的 outcome。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.arbitrage.strategy.actions.trigger_count import TriggerCountAction
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.consecutive_triggers import TriggerObservation
from src.arbitrage.strategy.consecutive_triggers import score_key
from tests.arbitrage.strategy._live_state import live_context


_PAIR = "p"
_HISTORY = "history"


def _observation(outcome, score, *, passed=True):
    return TriggerObservation(
        ts_ns=1,
        event_name="MarketOrderBookDeltas",
        score_raw=score,
        score_key=score_key(score),
        outcome=outcome,
        legs=(),
        candidate_ids=(),
        rates=(),
        passed=passed,
    )


class _SportsStore:
    def __init__(self, score):
        self._score = score

    def get(self, game_id):
        assert game_id == 42
        return SimpleNamespace(score=self._score)


def _ctx(history, *, score="6-4, 3-3", legs=None):
    store = ConsecutiveTriggerStore()
    for observation in history:
        store.append(_PAIR, _HISTORY, observation)
    ctx = live_context(pair_id=_PAIR, consecutive_trigger_store=store)
    ctx.pair_registry.register(
        _PAIR,
        ctx.pair_registry.instrument_ids_for_pair(_PAIR),
        game_id=42,
    )
    ctx.sports_store = _SportsStore(score)
    ctx.scratch["legs"] = legs or [
        {"instrument_id": "Y.POLYMARKET", "role": "yes"},
        {"instrument_id": "N.POLYMARKET", "role": "no"},
    ]
    return ctx


def _run(ctx, *, when=None):
    asyncio.run(TriggerCountAction(_HISTORY, when=when).execute(ctx))


def _book(ask):
    book = MagicMock()
    book.best_ask_price.return_value = ask
    return book


def test_keeps_unique_most_triggered_outcome_in_current_set():
    ctx = _ctx([
        _observation("yes", "6-4, 0-0"),
        _observation("yes", "6-4, 1-1"),
        _observation("no", "6-4, 2-1"),
    ])

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_generates_current_leg_when_no_upstream_leg_exists():
    store = ConsecutiveTriggerStore()
    for observation in (
        _observation("yes", "6-4, 0-0"),
        _observation("yes", "6-4, 1-1"),
        _observation("no", "6-4, 2-1"),
    ):
        store.append(_PAIR, _HISTORY, observation)
    ctx = live_context(
        pair_id=_PAIR,
        books={
            "Y.POLYMARKET": _book(0.4),
            "H.ORBITEXCH": _book(0.5),
            "N.POLYMARKET": _book(0.6),
        },
        infos={
            "Y.POLYMARKET": {"claim": "yes"},
            "H.ORBITEXCH": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )
    ctx.pair_registry.register(
        _PAIR,
        ctx.pair_registry.instrument_ids_for_pair(_PAIR),
        game_id=42,
    )
    ctx.sports_store = _SportsStore("6-4, 3-3")

    _run(ctx)

    assert ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.4,
        "prob": 0.4,
        "role": "yes",
        "claim": "yes",
        "share_if_wins": 5.0,
    }]


def test_same_score_duplicates_count_once():
    ctx = _ctx([
        _observation("yes", "6-4, 1-1"),
        _observation("yes", "6-4, 1-1"),
        _observation("no", "6-4, 0-1"),
    ])

    _run(ctx)

    assert ctx.scratch["legs"] == []


def test_previous_set_and_failed_observations_are_not_counted():
    ctx = _ctx([
        _observation("no", "1-0"),
        _observation("no", "2-0"),
        _observation("no", "6-4, 1-0", passed=False),
        _observation("yes", "6-4, 2-1"),
    ])

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_draw_excludes_latest_draw_and_later_scores():
    ctx = _ctx(
        [
            _observation("yes", "6-4, 0-1"),
            _observation("yes", "6-4, 1-1"),
            _observation("no", "6-4, 2-1"),
            _observation("no", "6-4, 2-2"),
            _observation("no", "6-4, 2-2"),
            _observation("no", "6-4, 3-2"),
            _observation("no", "6-4, 4-2"),
            _observation("no", "6-4, 5-2"),
        ],
        score="6-4, 5-2",
    )

    _run(ctx, when="current_draw")

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["yes"]


def test_current_draw_without_draw_in_current_set_fails_closed():
    ctx = _ctx(
        [
            _observation("yes", "6-4, 1-0"),
            _observation("yes", "6-4, 2-0"),
        ],
        score="6-4, 2-0",
    )

    _run(ctx, when="current_draw")

    assert ctx.scratch["legs"] == []


def test_omitted_when_keeps_counting_after_latest_draw():
    ctx = _ctx(
        [
            _observation("yes", "6-4, 1-0"),
            _observation("no", "6-4, 1-1"),
            _observation("no", "6-4, 2-1"),
        ],
        score="6-4, 2-1",
    )

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["legs"]] == ["no"]


def test_selected_candidate_is_filtered_and_synchronized_to_legs():
    ctx = _ctx([
        _observation("no", "6-4, 0-0"),
        _observation("no", "6-4, 0-1"),
        _observation("yes", "6-4, 1-1"),
    ])
    ctx.scratch["selected_candidate"] = {"candidate_id": "c", "legs": ctx.scratch["legs"]}

    _run(ctx)

    assert [leg["role"] for leg in ctx.scratch["selected_candidate"]["legs"]] == ["no"]
    assert ctx.scratch["legs"] == ctx.scratch["selected_candidate"]["legs"]


@pytest.mark.parametrize("score", [None, "", "bad"])
def test_missing_or_invalid_current_score_fails_closed(score):
    ctx = _ctx([_observation("yes", "6-4, 1-1")])
    if score is None:
        ctx.sports_store = None
    else:
        ctx.sports_store = _SportsStore(score)

    _run(ctx)

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize("history_key", [None, "", "  "])
def test_invalid_history_key_fails_fast(history_key):
    with pytest.raises(ValueError, match="history_key"):
        TriggerCountAction(history_key)


@pytest.mark.parametrize("when", ["", "draw", True, 1])
def test_invalid_when_fails_fast(when):
    with pytest.raises(ValueError, match="when"):
        TriggerCountAction(_HISTORY, when=when)
