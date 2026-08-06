"""DashGateAction:按 pair start_price 过滤 selected candidate 的 BUY 腿。"""

import asyncio

from src.arbitrage.common.pair_prices import PairPriceStore
from src.arbitrage.strategy.actions.dash_gate import DashGateAction
from src.arbitrage.strategy.condition import EvalContext


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Cache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def add(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def _context(start_prices=None):
    cache = _Cache()
    store = PairPriceStore(cache)
    store.initialize("p", ["yes", "no"])
    if start_prices is not None:
        store.capture_start("p", start_prices)
    return EvalContext(pair_id="p", cache=cache)


def test_drops_only_buy_leg_strictly_below_half_corresponding_start_price():
    ctx = _context({"yes": 0.8, "no": 0.4})
    candidate = {
        "candidate_id": "chosen",
        "rate": 0.1,
        "legs": [
            {"instrument_id": "Y1", "side": "BUY", "claim": "yes", "prob": 0.39},
            {"instrument_id": "Y2", "side": "BUY", "claim": "yes", "prob": 0.40},
            {"instrument_id": "N1", "side": "BUY", "role": "no", "prob": 0.21},
            {"instrument_id": "N2", "side": "SELL", "claim": "no", "prob": 0.01},
        ],
    }
    ctx.scratch["selected_candidate"] = candidate
    ctx.scratch["legs"] = candidate["legs"]

    _run(DashGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["Y2", "N1", "N2"]
    assert ctx.scratch["selected_candidate"]["rate"] == 0.1
    assert ctx.scratch["selected_candidate"]["legs"] == ctx.scratch["legs"]


def test_uses_default_start_price_and_keeps_uncomparable_legs():
    ctx = _context()  # yes/no 默认 0.6,过滤阈值为 0.3
    candidate = {
        "candidate_id": "chosen",
        "legs": [
            {"instrument_id": "LOW", "side": "BUY", "claim": "yes", "prob": 0.29},
            {"instrument_id": "NO_PROB", "side": "BUY", "claim": "yes"},
            {"instrument_id": "NO_OUTCOME", "side": "BUY", "prob": 0.01},
        ],
    }
    ctx.scratch["selected_candidate"] = candidate
    ctx.scratch["legs"] = candidate["legs"]

    _run(DashGateAction().execute(ctx))

    assert [leg["instrument_id"] for leg in ctx.scratch["legs"]] == ["NO_PROB", "NO_OUTCOME"]


def test_noop_without_selected_candidate_or_pair_price_state():
    ctx = EvalContext(pair_id="p", cache=_Cache())
    legs = [{"instrument_id": "LOW", "side": "BUY", "claim": "yes", "prob": 0.01}]
    ctx.scratch["selected_candidate"] = {"candidate_id": "chosen", "legs": legs}
    ctx.scratch["legs"] = legs

    _run(DashGateAction().execute(ctx))

    assert ctx.scratch["legs"] == legs
