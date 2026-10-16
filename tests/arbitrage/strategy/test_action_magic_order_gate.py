"""MagicOrderGateAction 的同类型活动订单去重契约。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.magic_order_gate import MagicOrderGateAction
from tests.arbitrage.strategy._live_state import live_context


_INFOS = {
    "Y.POLYMARKET": {"claim": "yes"},
    "N.POLYMARKET": {"claim": "no"},
}


def _run(action, ctx):
    asyncio.run(action.execute(ctx))


def _order(order_id, magic, *, status="SUBMITTED", instrument_id="Y.POLYMARKET"):
    return SimpleNamespace(
        client_order_id=order_id,
        instrument_id=instrument_id,
        status=status,
        tags=["arb:origin=pre_game", f"arb:magic={magic}"],
    )


def _ctx(*, history=()):
    ctx = live_context(infos=_INFOS, order_history=history)
    ctx.scratch.update({
        "legs": [{"instrument_id": "Y.POLYMARKET"}],
        "candidates": [{"id": "candidate"}],
        "selected_candidate": {"id": "selected"},
    })
    return ctx


def test_allows_when_same_magic_order_does_not_exist_and_marks_next_order():
    ctx = _ctx(history=[_order("OTHER", "other")])

    _run(MagicOrderGateAction("position_close"), ctx)

    assert ctx.scratch["order_magic"] == "position_close"
    assert ctx.scratch["legs"]


def test_additional_conflict_magic_blocks_but_does_not_replace_output_magic():
    ctx = _ctx(history=[_order("CONFLICT", "in_game_position_close")])

    _run(
        MagicOrderGateAction(
            "pre_game_position_close",
            conflicts=["in_game_position_close"],
        ),
        ctx,
    )

    assert ctx.scratch["legs"] == []
    assert "order_magic" not in ctx.scratch

    allowed = _ctx(history=[_order("OTHER", "other")])
    _run(
        MagicOrderGateAction(
            "pre_game_position_close",
            conflicts=["in_game_position_close"],
        ),
        allowed,
    )
    assert allowed.scratch["order_magic"] == "pre_game_position_close"


def test_primary_magic_always_conflicts_even_when_not_listed_explicitly():
    ctx = _ctx(history=[_order("SAME", "pre_game_position_close")])

    _run(
        MagicOrderGateAction(
            "pre_game_position_close",
            conflicts=["in_game_position_close"],
        ),
        ctx,
    )

    assert ctx.scratch["legs"] == []


@pytest.mark.parametrize("status", ["INITIALIZED", "SUBMITTED", "ACCEPTED"])
def test_blocks_same_magic_non_terminal_order_including_initialized(status):
    ctx = _ctx(history=[_order("SAME", "position_close", status=status)])

    _run(MagicOrderGateAction("position_close"), ctx)

    assert ctx.scratch["legs"] == []
    assert ctx.scratch["candidates"] == []
    assert "selected_candidate" not in ctx.scratch
    assert "order_magic" not in ctx.scratch


@pytest.mark.parametrize("status", ["FILLED", "CANCELED", "DENIED", "REJECTED", "EXPIRED"])
def test_terminal_same_magic_order_does_not_block(status):
    ctx = _ctx(history=[_order("DONE", "position_close", status=status)])

    _run(MagicOrderGateAction("position_close"), ctx)

    assert ctx.scratch["order_magic"] == "position_close"


def test_missing_runtime_dependencies_fails_closed():
    ctx = _ctx()
    ctx.cache = None

    _run(MagicOrderGateAction("position_close"), ctx)

    assert ctx.scratch["legs"] == []
    assert "order_magic" not in ctx.scratch


@pytest.mark.parametrize("magic", [" ", 1, True])
def test_rejects_invalid_magic(magic):
    with pytest.raises(ValueError, match="non-empty string"):
        MagicOrderGateAction(magic)


@pytest.mark.parametrize(
    "conflicts",
    ["other", [""], [1], [None]],
)
def test_rejects_invalid_conflicts(conflicts):
    with pytest.raises(ValueError, match="list of non-empty strings"):
        MagicOrderGateAction("position_close", conflicts=conflicts)
