"""in_game 持仓稀疏运行时状态。"""

from types import SimpleNamespace

import pytest

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.in_game_positions import InGamePositionRuntimeStore
from src.arbitrage.strategy.in_game_positions import current_set_number


class _SportsStore:
    def __init__(self, period):
        self._state = SimpleNamespace(period=period)

    def get(self, game_id):
        return self._state


def test_store_is_sparse_and_deletes_pair_override():
    store = InGamePositionRuntimeStore()

    assert store.min_tp_set("p") is None
    assert store.min_sl_set("p") is None
    store.set_min_tp_set("p", 4)
    store.set_min_sl_set("p", 2)
    assert store.min_tp_set("p") == 4
    assert store.min_sl_set("p") == 2
    store.delete_pair("p")
    assert store.min_tp_set("p") is None
    assert store.min_sl_set("p") is None


@pytest.mark.parametrize("setter", ["set_min_tp_set", "set_min_sl_set"])
@pytest.mark.parametrize("value", [0, -1, True, 2.5])
def test_store_rejects_invalid_min_set(setter, value):
    with pytest.raises(ValueError, match="positive integer"):
        getattr(InGamePositionRuntimeStore(), setter)("p", value)


@pytest.mark.parametrize(
    ("period", "expected"),
    [("S3", 3), ("s12", 12), ("TB3", 3), ("tb12", 12), ("SET3", 0), ("", 0)],
)
def test_current_set_number_fails_closed(period, expected):
    registry = PairRegistry()
    registry.register("p", ["Y.POLYMARKET"], game_id=42)

    assert current_set_number(
        pair_id="p",
        pair_registry=registry,
        sports_store=_SportsStore(period),
    ) == expected
