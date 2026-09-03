"""ScoreSelectionAction：按最新比分和订单方向过滤 selected candidate。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.score_selection import ScoreSelectionAction
from src.arbitrage.strategy.actions.score_selection import _compare_score
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


class _SportsStore:
    def __init__(self, score):
        self._state = SimpleNamespace(score=score) if score is not None else None

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(score="6-4, 2-3"):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        sports_store=_SportsStore(score),
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    legs = [
        {"instrument_id": "H.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "BUY", "claim": "no"},
        {"instrument_id": "H.POLYMARKET", "side": "SELL", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "SELL", "claim": "no"},
    ]
    ctx.scratch["selected_candidate"] = {"candidate_id": "chosen", "rate": 0.1, "legs": legs}
    ctx.scratch["legs"] = legs
    return ctx


def test_true_keeps_non_trailing_buy_and_trailing_sell():
    ctx = _ctx()  # 主方已赢一盘，即使当前盘 2-3，比赛级仍非落后

    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("H.POLYMARKET", "BUY"),
        ("A.POLYMARKET", "SELL"),
    ]
    assert ctx.scratch["selected_candidate"]["rate"] == 0.1


def test_false_keeps_trailing_buy_and_non_trailing_sell():
    ctx = _ctx()

    _run(ScoreSelectionAction(win_or_draw=False).execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("A.POLYMARKET", "BUY"),
        ("H.POLYMARKET", "SELL"),
    ]


def test_draw_treats_both_sides_as_non_trailing():
    ctx = _ctx("3-3")

    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("H.POLYMARKET", "BUY"),
        ("A.POLYMARKET", "BUY"),
    ]


@pytest.mark.parametrize("tie_break", [False, None])
def test_tie_break_disabled_cannot_classify_six_all(tie_break):
    ctx = _ctx("6-6(3-4)")
    params = {"win_or_draw": True}
    if tie_break is not None:
        params["tie_break"] = tie_break

    _run(ScoreSelectionAction(**params).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_tie_break_enabled_uses_tie_break_points():
    ctx = _ctx("6-6(3-4)")

    _run(ScoreSelectionAction(win_or_draw=True, tie_break=True).execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("A.POLYMARKET", "BUY"),
        ("H.POLYMARKET", "SELL"),
    ]


def test_tie_break_enabled_treats_bare_six_all_as_zero_all():
    ctx = _ctx("6-6")

    _run(ScoreSelectionAction(win_or_draw=True, tie_break=True).execute(ctx))

    assert [(leg["instrument_id"], leg["side"]) for leg in ctx.scratch["legs"]] == [
        ("H.POLYMARKET", "BUY"),
        ("A.POLYMARKET", "BUY"),
    ]


def test_missing_param_passes_every_leg_without_reading_score():
    ctx = _ctx(None)
    before = ctx.scratch["selected_candidate"]

    _run(ScoreSelectionAction().execute(ctx))

    assert ctx.scratch["selected_candidate"] is before
    assert len(ctx.scratch["legs"]) == 4


@pytest.mark.parametrize("score", [None, "", "unknown", "6:4"])
def test_configured_action_drops_all_when_score_is_unknown(score):
    ctx = _ctx(score)

    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))

    assert ctx.scratch["legs"] == []


def test_split_market_no_claim_is_not_misclassified_as_the_named_side():
    infos = {
        "HY.POLYMARKET": {"selection_role": "home", "claim": "yes"},
        "HN.POLYMARKET": {"selection_role": "home", "claim": "no"},
    }
    ctx = live_context(infos=infos, instrument_ids=list(infos), sports_store=_SportsStore("2-0"))
    ctx.pair_registry.register(ctx.pair_id, list(infos), game_id=42)
    legs = [
        {"instrument_id": "HY.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "HN.POLYMARKET", "side": "BUY", "claim": "no"},
    ]
    ctx.scratch["selected_candidate"] = {"legs": legs}
    ctx.scratch["legs"] = legs

    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["HY.POLYMARKET"]


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("6-4, 2-3", 1),
        ("4-6, 6-3, 2-3", -1),
        ("4-6, 6-3, 3-3", 0),
        ("6-4, 6-7(7-9)", 0),
        ("6-7(7-9), 7-5, 4-2", 1),
    ],
)
def test_compare_score_uses_completed_units_then_current_unit(score, expected):
    assert _compare_score(score) == expected


@pytest.mark.parametrize("score", ["6-6", "6-6(1-2)", "6-4, 6-6(4-3)"])
def test_compare_score_rejects_tie_break_when_disabled(score):
    assert _compare_score(score) is None


@pytest.mark.parametrize(
    ("score", "expected"),
    [("6-6", 0), ("6-6(1-2)", -1), ("6-4, 6-6(4-3)", 1)],
)
def test_compare_score_uses_tie_break_when_enabled(score, expected):
    assert _compare_score(score, tie_break=True) == expected


def test_noop_without_selected_candidate_and_for_cancel_candidate():
    ctx = _ctx()
    ctx.scratch.clear()
    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))
    assert ctx.scratch == {}

    candidate = {"cancel_pair_orders": True, "legs": []}
    ctx.scratch["selected_candidate"] = candidate
    _run(ScoreSelectionAction(win_or_draw=True).execute(ctx))
    assert ctx.scratch["selected_candidate"] is candidate


def test_invalid_param_raises():
    with pytest.raises(ValueError, match="win_or_draw must be a boolean"):
        ScoreSelectionAction(win_or_draw="true")
    with pytest.raises(ValueError, match="tie_break must be a boolean"):
        ScoreSelectionAction(tie_break="false")
