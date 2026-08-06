"""PairPriceStore 的初始化、单次写入和释放语义。"""

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

    assert store.capture_first("p", {"yes": 0.4, "no": 0.6}) is True
    assert store.capture_first("p", {"yes": 0.3, "no": 0.7}) is False
    assert store.capture_start("p", {"yes": 0.45, "no": 0.55}) is True
    assert store.capture_start("p", {"yes": 0.5, "no": 0.5}) is False

    state = store.initialize("p", ["home", "away"])
    assert state.first_price == {"yes": 0.4, "no": 0.6}
    assert state.start_price == {"yes": 0.45, "no": 0.55}


def test_delete_removes_pair_state():
    store = PairPriceStore(_Cache())
    store.initialize("p", ["yes", "no"])
    store.delete("p")
    assert store.get("p") is None
