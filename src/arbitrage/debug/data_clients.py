"""
Debug DataClient 子类 —— Q11.A 行情数据掉包(子类化 + 工厂选择,P10)。

设计见 `architectures/_cross-cutting/debug-injection.md`。

行为:覆盖 `_handle_data(data)` —— 在数据进 NT DataEngine 之前看一眼,
根据 `DebugConfig.get_mock(MockCategory.ODDS, context)` 决定是否替换。

**框架职责:拦截 + seam**;同时内置最小 ODDS mock:当 `mock_data` 按
`instrument_id` / `venue` / `market_type` / `selection_role` 匹配时,把配置里的
`bid|back`、`ask|lay` 转成 snapshot `OrderBookDeltas`。更复杂场景仍可子类化覆盖
`_maybe_substitute(data)`。

PM / OE 两个子类除继承基类不同,机制完全对称;共用 `_maybe_substitute` 钩子。
"""

from __future__ import annotations

from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

from nautilus_trader.adapters.orbitexch.data import OrbitExchDataClient

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory


class _DebugDataClientMixin:
    """共享行为:拦 `_handle_data` + `_maybe_substitute` 钩子。

    子类必须把 `_debug: DebugConfig` 存到 `self._debug`(由具体 PM/OE 子类 __init__ 注入)。
    """

    def _handle_data(self, data) -> None:
        substituted = self._maybe_substitute(data)
        # noinspection PyUnresolvedReferences
        super()._handle_data(substituted if substituted is not None else data)

    def _maybe_substitute(self, data):
        """Hook:返回替换后的 data;返 None 走 passthrough。

        默认支持 `MockCategory.ODDS` 的最小盘口替换;更复杂 mock 可由用户子类化覆盖:
        ```python
        class MyDebugPMDataClient(DebugPolymarketDataClient):
            def _maybe_substitute(self, data):
                if not isinstance(data, OrderBookDeltas):
                    return None
                mock = self._debug.get_mock(MockCategory.ODDS,
                                            context={"instrument_id": str(data.instrument_id)})
                if mock is None:
                    return None
                # 按 mock.data 字段构造替换的 OrderBookDeltas...
                return _build_mock_deltas(data.instrument_id, mock.data, ts=...)
        ```
        """
        if not isinstance(data, OrderBookDeltas):
            return None
        mock = self._debug.get_mock(MockCategory.ODDS, _mock_context(self, data.instrument_id))
        if mock is None:
            return None
        return _mock_order_book_deltas(self, data, mock.data)

    @property
    def debug_config(self) -> DebugConfig:
        # noinspection PyUnresolvedReferences
        return self._debug


class DebugPolymarketDataClient(_DebugDataClientMixin, PolymarketDataClient):
    """PM 数据客户端 Debug 子类。覆盖 `_handle_data` 拦数据;`_maybe_substitute` 默认 passthrough。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug


class DebugOrbitExchDataClient(_DebugDataClientMixin, OrbitExchDataClient):
    """OE 数据客户端 Debug 子类。机制同 PM(共享 mixin)。"""

    def __init__(self, *args, debug: DebugConfig, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._debug = debug


def _mock_context(client, instrument_id) -> dict:
    """构造 mock_data conditions 的匹配上下文。"""
    venue = str(instrument_id).rsplit(".", 1)[-1]
    inst = _cache_instrument(client, instrument_id)
    info = dict(getattr(inst, "info", None) or {}) if inst is not None else {}
    role = info.get("selection_role") or info.get("market_type")
    market_type = info.get("market_type") or info.get("selection_role")
    return {
        "instrument_id": str(instrument_id),
        "venue": venue.lower(),
        "venue_upper": venue.upper(),
        "selection_role": role,
        "market_type": market_type,
        "competition": info.get("competition"),
        "sport": info.get("sport"),
    }


def _mock_order_book_deltas(client, source: OrderBookDeltas, raw: object) -> OrderBookDeltas | None:
    """把 mock odds dict 转成 CLEAR + BUY/SELL ADD 的快照 deltas。"""
    if not isinstance(raw, dict):
        return None
    inst = _cache_instrument(client, source.instrument_id)
    bid = raw.get("bid", raw.get("back"))
    ask = raw.get("ask", raw.get("lay"))
    bid_size = raw.get("bid_size", raw.get("back_size", raw.get("size", 100.0)))
    ask_size = raw.get("ask_size", raw.get("lay_size", raw.get("size", 100.0)))

    ts_event, ts_init = _source_timestamps(source)
    deltas = [
        OrderBookDelta(
            instrument_id=source.instrument_id,
            action=BookAction.CLEAR,
            order=None,
            flags=0,
            sequence=0,
            ts_event=ts_event,
            ts_init=ts_init,
        ),
    ]
    seq = 1
    if bid is not None and float(bid_size) > 0:
        deltas.append(_mock_add(source.instrument_id, inst, OrderSide.BUY, bid, bid_size, seq, ts_event, ts_init))
        seq += 1
    if ask is not None and float(ask_size) > 0:
        deltas.append(_mock_add(source.instrument_id, inst, OrderSide.SELL, ask, ask_size, seq, ts_event, ts_init))

    return OrderBookDeltas(source.instrument_id, deltas) if len(deltas) > 1 else None


def _mock_add(instrument_id, inst, side, price, size, sequence, ts_event, ts_init) -> OrderBookDelta:
    make_price = getattr(inst, "make_price", None)
    make_qty = getattr(inst, "make_qty", None)
    order = BookOrder(
        side=side,
        price=make_price(float(price)) if callable(make_price) else Price(float(price), precision=3),
        size=make_qty(float(size)) if callable(make_qty) else Quantity(float(size), precision=2),
        order_id=sequence,
    )
    return OrderBookDelta(
        instrument_id=instrument_id,
        action=BookAction.ADD,
        order=order,
        flags=0,
        sequence=sequence,
        ts_event=ts_event,
        ts_init=ts_init,
    )


def _source_timestamps(source: OrderBookDeltas) -> tuple[int, int]:
    if source.deltas:
        first = source.deltas[0]
        return first.ts_event, first.ts_init
    return 0, 0


def _cache_instrument(client, instrument_id):
    cache = getattr(client, "_cache", None)
    if cache is None:
        return None
    instrument = getattr(cache, "instrument", None)
    return instrument(instrument_id) if callable(instrument) else None
