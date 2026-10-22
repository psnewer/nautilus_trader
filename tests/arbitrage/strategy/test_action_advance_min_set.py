"""止盈/止损平仓门槛分别推进到 current_set+offset。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.arbitrage.strategy.actions.advance_min_set import AdvanceMinSlSetAction
from src.arbitrage.strategy.actions.advance_min_set import AdvanceMinTpSetAction
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from tests.arbitrage.strategy._live_state import live_context


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ctx(*, period="S2", store=True, with_registry=True):
    kwargs = {
        "sports_store": SimpleNamespace(get=lambda game_id: SimpleNamespace(period=period)),
    }
    if store:
        kwargs["in_game_position_store"] = InGamePositionRuntimeStore()
    ctx = live_context(infos={"Y.POLYMARKET": {"claim": "yes"}}, instrument_ids=["Y.POLYMARKET"], **kwargs)
    if with_registry:
        ctx.pair_registry.register(ctx.pair_id, ["Y.POLYMARKET"], game_id=42)
    return ctx


@pytest.mark.parametrize("period", ["S2", "TB2"])
def test_advance_tp_and_sl_independently(period):
    ctx = _ctx(period=period)
    _run(AdvanceMinTpSetAction().execute(ctx))
    assert ctx.in_game_position_store.min_tp_set(ctx.pair_id) == 3
    assert ctx.in_game_position_store.min_sl_set(ctx.pair_id) is None

    _run(AdvanceMinSlSetAction(offset=2).execute(ctx))
    assert ctx.in_game_position_store.min_tp_set(ctx.pair_id) == 3
    assert ctx.in_game_position_store.min_sl_set(ctx.pair_id) == 4


def test_offset_is_configurable():
    ctx = _ctx(period="S2")
    _run(AdvanceMinTpSetAction(offset=2).execute(ctx))
    assert ctx.in_game_position_store.min_tp_set(ctx.pair_id) == 4


@pytest.mark.parametrize("period", ["", "foo", None])
def test_no_write_when_current_set_unknown(period):
    ctx = _ctx(period=period)
    _run(AdvanceMinTpSetAction().execute(ctx))
    _run(AdvanceMinSlSetAction().execute(ctx))
    assert ctx.in_game_position_store.min_tp_set(ctx.pair_id) is None
    assert ctx.in_game_position_store.min_sl_set(ctx.pair_id) is None


def test_no_write_without_store():
    ctx = _ctx(store=False)
    # 无 store 不应抛错
    _run(AdvanceMinTpSetAction().execute(ctx))


@pytest.mark.parametrize("action", [AdvanceMinTpSetAction, AdvanceMinSlSetAction])
@pytest.mark.parametrize("offset", [0, -1, 1.5, True, float("nan")])
def test_rejects_invalid_offset(action, offset):
    with pytest.raises(ValueError, match="offset must be a positive integer"):
        action(offset=offset)
