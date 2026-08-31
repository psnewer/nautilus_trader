"""多数据源比赛阶段状态。"""

import pytest

from src.arbitrage.common.sports_phase import PHASE_IN_PLAY
from src.arbitrage.common.sports_phase import PHASE_POST
from src.arbitrage.common.sports_phase import PHASE_PRE
from src.arbitrage.common.sports_phase import SportsPhaseStore


class _Cache:
    def __init__(self):
        self.data = {}

    def add(self, key, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


def test_phase_store_advances_monotonically_across_sources():
    store = SportsPhaseStore(_Cache())

    assert store.observe_in_play(7, False, source="ORBITEXCH:1", ts_event=1) is True
    assert store.get(7).phase == PHASE_PRE
    assert store.observe_in_play(7, False, source="SHARPEXCH:2", ts_event=2) is False

    assert store.observe_in_play(7, True, source="SHARPEXCH:2", ts_event=3) is True
    assert store.get(7).phase == PHASE_IN_PLAY
    assert store.observe_in_play(7, False, source="ORBITEXCH:1", ts_event=4) is False
    assert store.get(7).phase == PHASE_IN_PLAY

    assert store.advance(7, PHASE_POST, source="PMSPORTS", ts_event=5) is True
    assert store.observe_in_play(7, True, source="ORBITEXCH:1", ts_event=6) is False
    assert store.get(7).phase == PHASE_POST


def test_phase_store_rejects_unknown_phase_and_deletes_state():
    store = SportsPhaseStore(_Cache())
    with pytest.raises(ValueError, match="Unsupported sports phase"):
        store.advance(7, "PAUSED", source="test", ts_event=1)

    store.observe_in_play(7, False, source="test", ts_event=2)
    store.delete(7)
    assert store.get(7) is None
