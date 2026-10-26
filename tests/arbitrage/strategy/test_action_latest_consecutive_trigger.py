"""LatestConsecutiveTriggerAction 只读查询连续触发历史尾部。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.arbitrage.strategy.actions.latest_consecutive_trigger import LatestConsecutiveTriggerAction
from src.arbitrage.strategy.actions.venue_replace import VenueReplaceAction
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.consecutive_triggers import TriggerLeg
from src.arbitrage.strategy.consecutive_triggers import TriggerObservation
from tests.arbitrage.strategy._live_state import live_context


def _observation(
    *,
    passed: bool,
    price: float = 2.0,
    score="1-1",
) -> TriggerObservation:
    parts = tuple(
        (int(left), int(right), None, None)
        for left, right in (part.strip().split("-") for part in score.split(","))
    )
    return TriggerObservation(
        ts_ns=1,
        event_name="MarketOrderBookDeltas",
        score_raw=score,
        score_key=parts,
        outcome="yes",
        legs=(TriggerLeg("H.ORBITEXCH", "ORBITEXCH", "BUY", "yes", price, 0.5),),
        candidate_ids=("one_side:yes",),
        rates=(0.1,),
        passed=passed,
    )


def _run(ctx, *, limit=True, current_set=False):
    asyncio.run(
        LatestConsecutiveTriggerAction(
            "history",
            limit=limit,
            current_set=current_set,
        ).execute(ctx),
    )


class _SportsStore:
    def __init__(self, score):
        self._state = SimpleNamespace(score=score)

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _with_current_score(ctx, score):
    ctx.sports_store = _SportsStore(score)
    ctx.pair_registry.register(
        ctx.pair_id,
        ctx.pair_registry.instrument_ids_for_pair(ctx.pair_id),
        game_id=42,
    )
    return ctx


def _book(ask):
    book = MagicMock()
    book.best_ask_price.return_value = ask
    return book


def _current_quotes(*, yes_pm=0.4, yes_oe=0.5):
    return {
        "books": {
            "H.ORBITEXCH": _book(yes_oe),
            "Y.POLYMARKET": _book(yes_pm),
            "N.POLYMARKET": _book(0.6),
        },
        "infos": {
            "H.ORBITEXCH": {"claim": "yes"},
            "Y.POLYMARKET": {"claim": "yes"},
            "N.POLYMARKET": {"claim": "no"},
        },
    }


def test_latest_passed_observation_generates_current_leg_without_changing_history():
    store = ConsecutiveTriggerStore()
    observation = _observation(passed=True)
    store.append("p", "history", observation)
    ctx = live_context(
        pair_id="p",
        **_current_quotes(),
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

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
    assert store.latest("p", "history") is observation


def test_historical_duplicate_legs_are_ignored_when_generating_current_leg():
    store = ConsecutiveTriggerStore()
    observation = _observation(passed=True)
    store.append("p", "history", replace(observation, legs=observation.legs * 2))
    ctx = live_context(
        pair_id="p",
        **_current_quotes(),
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

    _run(ctx)

    assert len(ctx.scratch["legs"]) == 1
    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_newer_failed_observation_interrupts_older_passed_trigger():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True))
    store.append("p", "history", _observation(passed=False, price=2.1))
    ctx = live_context(
        pair_id="p",
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

    _run(ctx)

    assert ctx.scratch["legs"] == []


def test_limit_false_skips_newer_failed_observation():
    store = ConsecutiveTriggerStore()
    passed = _observation(passed=True)
    failed = _observation(passed=False, price=2.1)
    store.append("p", "history", passed)
    store.append("p", "history", failed)
    ctx = live_context(
        pair_id="p",
        **_current_quotes(),
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

    _run(ctx, limit=False)

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"
    assert store.history("p", "history") == (passed, failed)


def test_current_set_true_keeps_passed_observation_in_current_set():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True, score="6-4, 1-2"))
    ctx = _with_current_score(
        live_context(
            pair_id="p",
            **_current_quotes(),
            consecutive_trigger_store=store,
            strategy_defaults={"share": 5.0},
        ),
        "6-4, 2-2",
    )

    _run(ctx, current_set=True)

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


def test_current_set_true_rejects_passed_observation_from_previous_set():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True, score="5-4"))
    ctx = _with_current_score(
        live_context(
            pair_id="p",
            **_current_quotes(),
            consecutive_trigger_store=store,
            strategy_defaults={"share": 5.0},
        ),
        "6-4, 0-0",
    )

    _run(ctx, current_set=True)

    assert ctx.scratch["legs"] == []


def test_limit_false_current_set_skips_passed_observation_from_previous_set():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True, score="5-4"))
    store.append("p", "history", _observation(passed=False, score="6-4, 0-1"))
    ctx = _with_current_score(
        live_context(
            pair_id="p",
            **_current_quotes(),
            consecutive_trigger_store=store,
            strategy_defaults={"share": 5.0},
        ),
        "6-4, 0-2",
    )

    _run(ctx, limit=False, current_set=True)

    assert ctx.scratch["legs"] == []


def test_limit_false_current_set_skips_failure_and_keeps_same_set_pass():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True, score="6-4, 0-1"))
    store.append("p", "history", _observation(passed=False, score="6-4, 0-2"))
    ctx = _with_current_score(
        live_context(
            pair_id="p",
            **_current_quotes(),
            consecutive_trigger_store=store,
            strategy_defaults={"share": 5.0},
        ),
        "6-4, 0-3",
    )

    _run(ctx, limit=False, current_set=True)

    assert ctx.scratch["legs"][0]["instrument_id"] == "Y.POLYMARKET"


@pytest.mark.parametrize("score", [None, "", "bad"])
def test_current_set_true_fails_closed_without_valid_current_score(score):
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True))
    ctx = live_context(
        pair_id="p",
        **_current_quotes(),
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )
    if score is not None:
        _with_current_score(ctx, score)

    _run(ctx, current_set=True)

    assert ctx.scratch["legs"] == []


def test_missing_current_quote_fails_closed_even_when_history_contains_a_leg():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True))
    ctx = live_context(
        pair_id="p",
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

    _run(ctx)

    assert ctx.scratch["legs"] == []


def test_generated_leg_can_enter_existing_venue_replace_chain():
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True))
    ctx = live_context(
        pair_id="p",
        **_current_quotes(yes_pm=0.6, yes_oe=0.5),
        consecutive_trigger_store=store,
        strategy_defaults={"share": 5.0},
    )

    _run(ctx)
    asyncio.run(VenueReplaceAction().execute(ctx))

    assert ctx.scratch["legs"] == [{
        "instrument_id": "Y.POLYMARKET",
        "venue": "POLYMARKET",
        "side": "BUY",
        "price": 0.6,
        "prob": 0.6,
        "role": "yes",
        "claim": "yes",
        "qty": 5.0,
        "share_if_wins": 5.0,
        "cost": 3.0,
    }]


@pytest.mark.parametrize("share", [None, 0, -1])
def test_missing_or_non_positive_share_fails_closed(share):
    store = ConsecutiveTriggerStore()
    store.append("p", "history", _observation(passed=True))
    ctx = live_context(
        pair_id="p",
        **_current_quotes(),
        consecutive_trigger_store=store,
        strategy_defaults={"share": share},
    )

    _run(ctx)

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize("history_key", ["", "  ", None])
def test_invalid_history_key_fails_fast(history_key):
    with pytest.raises(ValueError, match="history_key"):
        LatestConsecutiveTriggerAction(history_key)


@pytest.mark.parametrize("limit", [None, 0, 1, "false"])
def test_invalid_limit_fails_fast(limit):
    with pytest.raises(ValueError, match="limit"):
        LatestConsecutiveTriggerAction("history", limit=limit)


@pytest.mark.parametrize("current_set", [None, 0, 1, "true"])
def test_invalid_current_set_fails_fast(current_set):
    with pytest.raises(ValueError, match="current_set"):
        LatestConsecutiveTriggerAction("history", current_set=current_set)
