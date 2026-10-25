"""LatestConsecutiveTriggerAction 只读查询连续触发历史尾部。"""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.arbitrage.strategy.actions.latest_consecutive_trigger import LatestConsecutiveTriggerAction
from src.arbitrage.strategy.actions.venue_replace import VenueReplaceAction
from src.arbitrage.strategy.consecutive_triggers import ConsecutiveTriggerStore
from src.arbitrage.strategy.consecutive_triggers import TriggerLeg
from src.arbitrage.strategy.consecutive_triggers import TriggerObservation
from tests.arbitrage.strategy._live_state import live_context


def _observation(*, passed: bool, price: float = 2.0) -> TriggerObservation:
    return TriggerObservation(
        ts_ns=1,
        event_name="MarketOrderBookDeltas",
        score_raw="1-1",
        score_key=((1, 1, None, None),),
        outcome="yes",
        legs=(TriggerLeg("H.ORBITEXCH", "ORBITEXCH", "BUY", "yes", price, 0.5),),
        candidate_ids=("one_side:yes",),
        rates=(0.1,),
        passed=passed,
    )


def _run(ctx):
    asyncio.run(LatestConsecutiveTriggerAction("history").execute(ctx))


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
