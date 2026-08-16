"""Pair 级初始/开赛价格状态,物理存储使用 NT Cache 通用对象区。"""

from __future__ import annotations

import json
from dataclasses import dataclass


DEFAULT_START_PRICE = 0.6
_STORE_KEY_PREFIX = "arb:pair_price:"


@dataclass(frozen=True, slots=True)
class PairPriceState:
    first_price: dict[str, float]
    start_price: dict[str, float]
    up_price: dict[str, float]
    down_price: dict[str, float]


class PairPriceStore:
    """按 market-level pair_id 保存完整 outcome 价格向量。"""

    def __init__(self, cache) -> None:
        self._cache = cache

    @staticmethod
    def _key(pair_id: str) -> str:
        return f"{_STORE_KEY_PREFIX}{pair_id}"

    def get(self, pair_id: str) -> PairPriceState | None:
        raw = self._cache.get(self._key(pair_id))
        if not raw:
            return None
        values = json.loads(raw.decode("utf-8"))
        return PairPriceState(
            first_price={str(k): float(v) for k, v in values["first_price"].items()},
            start_price={str(k): float(v) for k, v in values["start_price"].items()},
            up_price={str(k): float(v) for k, v in values.get("up_price", {}).items()},
            down_price={str(k): float(v) for k, v in values.get("down_price", {}).items()},
        )

    def initialize(self, pair_id: str, outcomes) -> PairPriceState:
        existing = self.get(pair_id)
        if existing is not None:
            return existing
        normalized = tuple(dict.fromkeys(
            str(value).strip().lower()
            for value in outcomes
            if str(value).strip()
        ))
        state = PairPriceState(
            first_price={},
            start_price=dict.fromkeys(normalized, DEFAULT_START_PRICE),
            up_price={},
            down_price={},
        )
        self._put(pair_id, state)
        return state

    def capture_first(self, pair_id: str, prices: dict[str, float]) -> bool:
        state = self.get(pair_id)
        if state is None or state.first_price or set(prices) != set(state.start_price):
            return False
        self._put(pair_id, PairPriceState(
            first_price=dict(prices),
            start_price=state.start_price,
            up_price=state.up_price,
            down_price=state.down_price,
        ))
        return True

    def capture_start(self, pair_id: str, prices: dict[str, float]) -> bool:
        state = self.get(pair_id)
        if state is None or not state.start_price or set(prices) != set(state.start_price):
            return False
        if any(value != DEFAULT_START_PRICE for value in state.start_price.values()):
            return False
        self._put(pair_id, PairPriceState(
            first_price=state.first_price,
            start_price=dict(prices),
            up_price=state.up_price,
            down_price=state.down_price,
        ))
        return True

    def update_extremes(self, pair_id: str, prices: dict[str, float]) -> bool:
        """用一个完整同刻价格向量更新各 outcome 的历史最高/最低价。"""
        state = self.get(pair_id)
        if state is None or set(prices) != set(state.start_price):
            return False
        up_price = {
            outcome: max(float(prices[outcome]), state.up_price.get(outcome, float("-inf")))
            for outcome in state.start_price
        }
        down_price = {
            outcome: min(float(prices[outcome]), state.down_price.get(outcome, float("inf")))
            for outcome in state.start_price
        }
        self._put(pair_id, PairPriceState(
            first_price=state.first_price,
            start_price=state.start_price,
            up_price=up_price,
            down_price=down_price,
        ))
        return True

    def delete(self, pair_id: str) -> None:
        self._cache.delete(self._key(pair_id))

    def _put(self, pair_id: str, state: PairPriceState) -> None:
        raw = json.dumps({
            "first_price": state.first_price,
            "start_price": state.start_price,
            "up_price": state.up_price,
            "down_price": state.down_price,
        }).encode("utf-8")
        self._cache.add(self._key(pair_id), raw)
