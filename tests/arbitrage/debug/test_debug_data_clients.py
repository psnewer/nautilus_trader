"""Q11.A DebugDataClient mixin 行为(`_handle_data` 拦截 + `_maybe_substitute` 钩子)。

对应用例:debug-A.{1,3,4}(substitute hook seam)。

不构造真 NT DataClient(它需要 ClobClient/msgbus/cache/clock 等):
用一个 `_RecorderBase` 假基类替代,聚焦验证 mixin 注入的行为。
"""

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.debug.data_clients import _DebugDataClientMixin
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import OrderSide
from tests.arbitrage.risk._factories import oe_instrument
from tests.arbitrage.risk._factories import pm_instrument


class _RecorderBase:
    """假基类:记 `_handle_data` 收到啥(替代 NT DataClient 父类的 msgbus 路由)。"""

    def __init__(self):
        self.received = []

    def _handle_data(self, data) -> None:
        self.received.append(data)


class _DebugClient(_DebugDataClientMixin, _RecorderBase):
    def __init__(self, debug, cache=None):
        super().__init__()
        self._debug = debug
        self._cache = cache


class _Cache:
    def __init__(self, instrument):
        self._instrument = instrument

    def instrument(self, instrument_id):
        return self._instrument if instrument_id == self._instrument.id else None


def _source_deltas(instrument):
    order = BookOrder(
        side=OrderSide.SELL,
        price=instrument.make_price(0.50),
        size=instrument.make_qty(10),
        order_id=1,
    )
    return OrderBookDeltas(
        instrument.id,
        [
            OrderBookDelta(
                instrument_id=instrument.id,
                action=BookAction.ADD,
                order=order,
                flags=0,
                sequence=1,
                ts_event=11,
                ts_init=22,
            ),
        ],
    )


# ── debug-A.1: 默认 passthrough(`_maybe_substitute` 返 None,原 data 透传)─
def test_default_hook_passes_through():
    client = _DebugClient(debug=DebugConfig(enabled=False))
    client._handle_data("raw_delta")
    assert client.received == ["raw_delta"]


# ── debug-A.2: 子类覆盖 hook → 替换值进 super()._handle_data ──────
def test_overridden_hook_substitutes():
    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            return f"mocked:{data}"

    client = _Sub(debug=DebugConfig(enabled=True))
    client._handle_data("raw_delta")
    assert client.received == ["mocked:raw_delta"]


# ── debug-A.3: 子类 hook 返 None → 退化到 passthrough(不会传 None 给 super)──
def test_hook_returning_none_falls_back_to_passthrough():
    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            return None  # 显式 None

    client = _Sub(debug=DebugConfig(enabled=True))
    client._handle_data(42)
    assert client.received == [42]


# ── debug-A.4: 子类可经 self._debug 读 mock_data 决定替换 ────────
def test_hook_reads_debug_config_mock_data():
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["odds_force"] = MockDataItem(
        id="odds_force",
        category=MockCategory.ODDS,
        enabled=True,
        data={"price": 0.5},
    )

    class _Sub(_DebugClient):
        def _maybe_substitute(self, data):
            mock = self._debug.get_mock(MockCategory.ODDS, {})
            return f"price={mock.data['price']}" if mock else None

    client = _Sub(debug=cfg)
    client._handle_data("ignored")
    assert client.received == ["price=0.5"]


def test_default_odds_mock_replaces_polymarket_order_book_deltas():
    inst = pm_instrument("ATP", "home")
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["pm_home"] = MockDataItem(
        id="pm_home",
        category=MockCategory.ODDS,
        enabled=True,
        data={"bid": 0.01, "ask": 0.02, "size": 5.0},
        conditions={"venue": "polymarket", "market_type": "home"},
    )
    source = _source_deltas(inst)

    client = _DebugClient(debug=cfg, cache=_Cache(inst))
    client._handle_data(source)

    out = client.received[0]
    assert isinstance(out, OrderBookDeltas)
    assert out is not source
    assert [d.action for d in out.deltas] == [BookAction.CLEAR, BookAction.ADD, BookAction.ADD]
    assert out.deltas[1].order.side == OrderSide.BUY
    assert float(out.deltas[1].order.price) == 0.01
    assert out.deltas[2].order.side == OrderSide.SELL
    assert float(out.deltas[2].order.price) == 0.02
    assert out.deltas[0].ts_event == 11
    assert out.deltas[0].ts_init == 22


def test_default_odds_mock_accepts_orbitexch_back_lay_names():
    inst = oe_instrument("ATP", "away")
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["oe_away"] = MockDataItem(
        id="oe_away",
        category=MockCategory.ODDS,
        enabled=True,
        data={"back": 2.0, "lay": 2.2, "size": 7.0},
        conditions={"venue": "orbitexch", "market_type": "away"},
    )

    client = _DebugClient(debug=cfg, cache=_Cache(inst))
    client._handle_data(_source_deltas(inst))

    out = client.received[0]
    assert [d.action for d in out.deltas] == [BookAction.CLEAR, BookAction.ADD, BookAction.ADD]
    assert out.deltas[1].order.side == OrderSide.BUY
    assert float(out.deltas[1].order.price) == 2.0
    assert out.deltas[2].order.side == OrderSide.SELL
    assert float(out.deltas[2].order.price) == 2.2


def test_default_odds_mock_no_match_passes_through():
    inst = pm_instrument("ATP", "home")
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["pm_away"] = MockDataItem(
        id="pm_away",
        category=MockCategory.ODDS,
        enabled=True,
        data={"ask": 0.02},
        conditions={"venue": "polymarket", "market_type": "away"},
    )
    source = _source_deltas(inst)

    client = _DebugClient(debug=cfg, cache=_Cache(inst))
    client._handle_data(source)

    assert client.received == [source]


# ── debug_config 访问器 ──────────────────────────────────────
def test_debug_config_accessor():
    cfg = DebugConfig(enabled=True)
    client = _DebugClient(debug=cfg)
    assert client.debug_config is cfg
