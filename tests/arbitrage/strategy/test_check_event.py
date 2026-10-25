"""event Check 按评估触发源过滤分支。"""

import pytest

from src.arbitrage.strategy.checks.event import EventCheck
from tests.arbitrage.strategy._live_state import live_context


@pytest.mark.parametrize(
    "event_name",
    [None, "MatchedPair", "MarketOrderBookDeltas", "SportsGameUpdate"],
)
def test_missing_event_does_not_filter_trigger_source(event_name):
    assert EventCheck().passes(live_context(event_name=event_name)) is True


@pytest.mark.parametrize("event_name", ["OrderBookDeltas", "MarketOrderBookDeltas"])
def test_obd_accepts_both_order_book_event_names(event_name):
    assert EventCheck(event="obd").passes(live_context(event_name=event_name)) is True


def test_score_only_accepts_sports_game_update():
    assert EventCheck(event="score").passes(live_context(event_name="SportsGameUpdate")) is True
    assert (
        EventCheck(event="score").passes(live_context(event_name="MarketOrderBookDeltas"))
        is False
    )


@pytest.mark.parametrize("event", ["", "sports", "other", 1])
def test_invalid_event_fails_fast(event):
    with pytest.raises(ValueError, match="obd/score"):
        EventCheck(event=event)
