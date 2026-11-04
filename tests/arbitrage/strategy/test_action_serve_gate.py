"""ServeGateAction:由 first_serve 与当前局数奇偶筛腿。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.serve_gate import ServeGateAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "H.POLYMARKET": {"selection_role": "home", "claim": "yes"},
    "A.POLYMARKET": {"selection_role": "away", "claim": "no"},
}


class _SportsStore:
    def __init__(self, score, first_serve, ended=False):
        self._state = SimpleNamespace(score=score, first_serve=first_serve, ended=ended)

    def get(self, game_id):
        assert game_id == 42
        return self._state


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(score="0-0", first_serve="home", ended=False):
    ctx = live_context(
        infos=_INFOS,
        instrument_ids=list(_INFOS),
        sports_store=_SportsStore(score, first_serve, ended),
    )
    ctx.pair_registry.register(ctx.pair_id, list(_INFOS), game_id=42)
    ctx.scratch["legs"] = [
        {"instrument_id": "H.POLYMARKET", "side": "BUY", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "BUY", "claim": "no"},
        {"instrument_id": "H.POLYMARKET", "side": "SELL", "claim": "yes"},
        {"instrument_id": "A.POLYMARKET", "side": "SELL", "claim": "no"},
    ]
    return ctx


def _instruments(ctx):
    return [leg["instrument_id"] for leg in ctx.scratch["legs"]]


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0-0", "H.POLYMARKET"),
        ("1-0", "A.POLYMARKET"),
        ("6-4, 0-0", "H.POLYMARKET"),
        ("6-4", "H.POLYMARKET"),
        ("7-6", "A.POLYMARKET"),
        ("7-6, 0-0", "A.POLYMARKET"),
        ("6-4, 2-1", "A.POLYMARKET"),
    ],
)
def test_current_server_uses_first_serve_and_completed_game_parity(score, expected):
    ctx = _ctx(score=score, first_serve="home")

    _run(ServeGateAction().execute(ctx))

    assert _instruments(ctx) == [expected, expected]


def test_serve_false_keeps_receiver():
    ctx = _ctx(score="2-2", first_serve="away")

    _run(ServeGateAction(serve=False).execute(ctx))

    assert _instruments(ctx) == ["H.POLYMARKET", "H.POLYMARKET"]


@pytest.mark.parametrize(
    ("score", "first_serve"),
    [
        ("0-0", ""),
        ("", "home"),
        ("bad", "home"),
        ("6-6", "home"),
        ("6-6(3-2)", "home"),
        ("3-2, 0-0", "home"),
    ],
)
def test_unknown_or_unsafe_server_fails_closed(score, first_serve):
    ctx = _ctx(score=score, first_serve=first_serve)

    _run(ServeGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_ended_match_fails_closed_even_when_last_set_is_complete():
    ctx = _ctx(score="6-4, 6-4", first_serve="home", ended=True)

    _run(ServeGateAction().execute(ctx))

    assert ctx.scratch["legs"] == []


def test_selected_candidate_and_candidate_pool_are_filtered_but_cancel_is_preserved():
    selected = _ctx(score="1-0")
    legs = selected.scratch["legs"]
    selected.scratch["selected_candidate"] = {"candidate_id": "chosen", "legs": legs}

    _run(ServeGateAction().execute(selected))

    assert _instruments(selected) == ["A.POLYMARKET", "A.POLYMARKET"]
    assert selected.scratch["selected_candidate"]["legs"] == selected.scratch["legs"]

    pool = _ctx()
    legs = pool.scratch.pop("legs")
    cancel = {"cancel_pair_orders": True, "legs": []}
    pool.scratch["candidates"] = [
        {"candidate_id": "home", "legs": [legs[0]]},
        {"candidate_id": "away", "legs": [legs[1]]},
        cancel,
    ]

    _run(ServeGateAction().execute(pool))

    assert pool.scratch["candidates"] == [
        {"candidate_id": "home", "legs": [legs[0]]},
        cancel,
    ]


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_serve_must_be_boolean(value):
    with pytest.raises(ValueError, match="serve_gate: serve must be a boolean"):
        ServeGateAction(serve=value)
