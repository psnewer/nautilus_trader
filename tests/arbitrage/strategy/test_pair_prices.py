"""PairPriceStore 的初始化、参考价/极值/趋势基准写入和释放语义。"""

import json

from src.arbitrage.common.pair_prices import PairPriceStore


class _Cache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def add(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def test_initialize_is_idempotent_and_capture_fields_only_once():
    store = PairPriceStore(_Cache())
    state = store.initialize("p", ["yes", "no"])
    assert state.first_price == {}
    assert state.start_price == {"yes": 0.6, "no": 0.6}
    assert state.up_price == {}
    assert state.down_price == {}
    assert state.trend_price == {}

    assert store.capture_first("p", {"yes": 0.4, "no": 0.6}) is True
    assert store.capture_first("p", {"yes": 0.3, "no": 0.7}) is False
    assert store.capture_start("p", {"yes": 0.45, "no": 0.55}) is True
    assert store.capture_start("p", {"yes": 0.5, "no": 0.5}) is False

    state = store.initialize("p", ["home", "away"])
    assert state.first_price == {"yes": 0.4, "no": 0.6}
    assert state.start_price == {"yes": 0.45, "no": 0.55}


def test_update_extremes_tracks_each_outcome_high_and_low():
    store = PairPriceStore(_Cache())
    store.initialize("p", ["yes", "no"])

    assert store.update_extremes("p", {"yes": 0.4, "no": 0.6}) is True
    assert store.update_extremes("p", {"yes": 0.55, "no": 0.45}) is True
    assert store.update_extremes("p", {"yes": 0.35, "no": 0.65}) is True

    state = store.get("p")
    assert state.up_price == {"yes": 0.55, "no": 0.65}
    assert state.down_price == {"yes": 0.35, "no": 0.45}


def test_update_trend_replaces_complete_outcome_vector():
    store = PairPriceStore(_Cache())
    store.initialize("p", ["yes", "no"])

    assert store.update_trend("p", {"yes": 0.45, "no": 0.57}) is True
    assert store.get("p").trend_price == {"yes": 0.45, "no": 0.57}
    assert store.update_trend("p", {"yes": 0.46}) is False
    assert store.get("p").trend_price == {"yes": 0.45, "no": 0.57}


def test_old_schema_reads_with_empty_extremes_and_trend():
    cache = _Cache()
    cache.add(
        "arb:pair_price:p",
        json.dumps({"first_price": {"yes": 0.4}, "start_price": {"yes": 0.6}}).encode(),
    )

    state = PairPriceStore(cache).get("p")
    assert state.up_price == {}
    assert state.down_price == {}
    assert state.trend_price == {}


def test_delete_removes_pair_state():
    store = PairPriceStore(_Cache())
    store.initialize("p", ["yes", "no"])
    store.delete("p")
    assert store.get("p") is None
