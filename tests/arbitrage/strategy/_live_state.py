"""Strategy 单测使用的最小 live Cache / PairRegistry 装配。"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.identifiers import InstrumentId
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.strategy.condition import EvalContext


class StrategyTestCache:
    def __init__(self, *, books=None, infos=None, positions=None, orders=None, constraints=None):
        self._books = {str(key): value for key, value in (books or {}).items()}
        self._values = {}
        self._positions = list(positions or ())
        self._orders = list(orders or ())
        constraints = constraints or {}
        self._instruments = {}
        for raw_id, raw_info in (infos or {}).items():
            instrument_id = str(raw_id)
            info = dict(raw_info or {})
            if not info.get("claim"):
                role = str(info.get("selection_role") or "").lower()
                if role == "home":
                    info["claim"] = "yes"
                elif role == "away":
                    info["claim"] = "no"
            values = constraints.get(raw_id, constraints.get(instrument_id, {})) or {}
            if values.get("min_buy_notional") is not None:
                info["min_buy_notional"] = values["min_buy_notional"]
            self._instruments[instrument_id] = SimpleNamespace(
                id=InstrumentId.from_str(instrument_id),
                info=info,
                min_quantity=values.get("min_quantity"),
                min_notional=values.get("min_notional"),
                size_increment=values.get("size_increment"),
            )

    def order_book(self, instrument_id):
        return self._books.get(str(instrument_id))

    def instrument(self, instrument_id):
        return self._instruments.get(str(instrument_id))

    def get(self, key):
        return self._values.get(key)

    def add(self, key, value):
        self._values[key] = value

    def delete(self, key):
        self._values.pop(key, None)

    def positions_open(self, *, instrument_id=None):
        if instrument_id is None:
            return list(self._positions)
        return [
            position
            for position in self._positions
            if str(getattr(position, "instrument_id", "")) == str(instrument_id)
        ]

    def orders_open(self, *, instrument_id=None):
        if instrument_id is None:
            return list(self._orders)
        return [
            order
            for order in self._orders
            if str(getattr(order, "instrument_id", "")) == str(instrument_id)
        ]


def live_context(
    *,
    pair_id="p",
    books=None,
    infos=None,
    positions=None,
    orders=None,
    instrument_ids=None,
    constraints=None,
    **kwargs,
):
    ids = list(instrument_ids or (infos or {}).keys() or (books or {}).keys())
    cache = StrategyTestCache(
        books=books,
        infos=infos,
        positions=positions,
        orders=orders,
        constraints=constraints,
    )
    registry = PairRegistry()
    registry.register(pair_id, ids)
    return EvalContext(
        pair_id=pair_id,
        cache=cache,
        pair_registry=registry,
        **kwargs,
    )
