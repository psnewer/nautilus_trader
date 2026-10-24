"""LatestScoreAction:按最近一次可唯一归因的比分增量筛选腿。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.latest_score import LatestScoreAction
from src.arbitrage.strategy.actions.latest_score import _score_transition_role
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


class _SportsStore:
    def __init__(self, *scores):
        self._history = tuple(SimpleNamespace(score=score) for score in scores)

    def score_history(self, game_id):
        assert game_id == 42
        return self._history


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(*scores):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        sports_store=_SportsStore(*scores),
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    legs = [
        {"instrument_id": "H.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "BUY", "claim": "no"},
        {"instrument_id": "H.POLYMARKET", "side": "SELL", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "SELL", "claim": "no"},
    ]
    ctx.scratch["selected_candidate"] = {"candidate_id": "chosen", "legs": legs}
    ctx.scratch["legs"] = legs
    return ctx


@pytest.mark.parametrize(
    ("scores", "expected_instrument"),
    [
        (("0-0", "1-0"), "H.POLYMARKET"),
        (("6-4, 2-2", "6-4, 2-3"), "A.POLYMARKET"),
        (("6-6(3-4)", "6-6(4-4)"), "H.POLYMARKET"),
        (("6-6(6-5)", "7-6"), "H.POLYMARKET"),
    ],
)
def test_keeps_last_scoring_instrument_for_both_order_sides(scores, expected_instrument):
    ctx = _ctx(*scores)

    _run(LatestScoreAction().execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        (expected_instrument, "BUY"),
        (expected_instrument, "SELL"),
    ]


def test_new_set_zero_all_does_not_replace_previous_real_scorer():
    ctx = _ctx("5-4", "6-4", "6-4, 0-0")

    _run(LatestScoreAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == [
        "H.POLYMARKET",
        "H.POLYMARKET",
    ]


@pytest.mark.parametrize(
    "scores",
    [
        ("1-0",),                 # 首帧没有前态
        ("0-0", "1-1"),         # 跳帧后双方均增加,最后得分方不可知
        ("2-1", "1-1"),         # 比分回退/修正
        ("unknown", "1-0"),     # 坏格式
    ],
)
def test_unknown_latest_scorer_fails_closed(scores):
    ctx = _ctx(*scores)

    _run(LatestScoreAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_transition_classifier_distinguishes_structural_change_from_score():
    assert _score_transition_role("6-4", "6-4, 0-0") == "no_score"
    assert _score_transition_role("6-4, 0-0", "6-4, 0-1") == "away"
