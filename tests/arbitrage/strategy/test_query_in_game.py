"""赛前/赛中 self_hits 判据。strategy-4.pre_rebate.1。"""

from types import SimpleNamespace

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.queries.in_game import InGameQuery
from src.arbitrage.strategy.queries.in_game import PreGameQuery


class _SportsStore:
    def __init__(self, state):
        self._state = state

    def get(self, game_id):
        return self._state


def _state(phase):
    return SimpleNamespace(phase=phase)


def _ctx(state, *, game_id=42, phase_store="default"):
    registry = PairRegistry()
    registry.register("p", ["Y.POLYMARKET"], game_id=game_id)
    store = _SportsStore(state) if phase_store == "default" else phase_store
    return EvalContext(pair_id="p", pair_registry=registry, phase_store=store)


def test_live_not_ended_is_in_game():
    assert InGameQuery().matches(_ctx(_state("IN_PLAY"))) is True


def test_none_state_is_neither_in_game_nor_pre_game():
    assert InGameQuery().matches(_ctx(None)) is False
    assert PreGameQuery().matches(_ctx(None)) is False


def test_explicit_pre_is_pre_game_only():
    assert InGameQuery().matches(_ctx(_state("PRE"))) is False
    assert PreGameQuery().matches(_ctx(_state("PRE"))) is True


def test_ended_is_neither_in_game_nor_pre_game():
    assert InGameQuery().matches(_ctx(_state("POST"))) is False
    assert PreGameQuery().matches(_ctx(_state("POST"))) is False


def test_missing_game_id_fail_closed():
    registry = PairRegistry()
    registry.register("p", ["Y.POLYMARKET"])  # 无 game_id
    ctx = EvalContext(pair_id="p", pair_registry=registry, phase_store=_SportsStore(_state("IN_PLAY")))
    assert InGameQuery().matches(ctx) is False


def test_missing_phase_store_fail_closed():
    assert InGameQuery().matches(_ctx(_state("IN_PLAY"), phase_store=None)) is False
    assert PreGameQuery().matches(_ctx(_state("PRE"), phase_store=None)) is False
