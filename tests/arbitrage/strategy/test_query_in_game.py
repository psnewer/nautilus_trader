"""InGameQuery: 赛前/赛中 self_hits 判据(#326)。strategy-4.pre_rebate.1。"""

from types import SimpleNamespace

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.condition import EvalContext
from src.arbitrage.strategy.queries.in_game import InGameQuery


class _SportsStore:
    def __init__(self, state):
        self._state = state

    def get(self, game_id):
        return self._state


def _state(live, ended):
    return SimpleNamespace(live=live, ended=ended)


def _ctx(state, *, game_id=42, sports_store="default"):
    registry = PairRegistry()
    registry.register("p", ["Y.POLYMARKET"], game_id=game_id)
    store = _SportsStore(state) if sports_store == "default" else sports_store
    return EvalContext(pair_id="p", pair_registry=registry, sports_store=store)


def test_live_not_ended_is_in_game():
    assert InGameQuery().matches(_ctx(_state(True, False))) is True


def test_pre_game_none_state_not_in_game():
    assert InGameQuery().matches(_ctx(None)) is False


def test_not_live_not_in_game():
    assert InGameQuery().matches(_ctx(_state(False, False))) is False


def test_ended_not_in_game():
    # ended 落入 NOT in_game(与赛前同支),已知边界。
    assert InGameQuery().matches(_ctx(_state(True, True))) is False


def test_missing_game_id_fail_closed():
    registry = PairRegistry()
    registry.register("p", ["Y.POLYMARKET"])  # 无 game_id
    ctx = EvalContext(pair_id="p", pair_registry=registry, sports_store=_SportsStore(_state(True, False)))
    assert InGameQuery().matches(ctx) is False


def test_missing_sports_store_fail_closed():
    assert InGameQuery().matches(_ctx(_state(True, False), sports_store=None)) is False
