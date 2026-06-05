"""ArbExecutionSessionMixin —— session 入口 / leg_settled / 终态 / 超时 / 同步。

mixin 只触及 cache.orders_open + cache.instrument → 用 FakeCache 轻量化;
order event 须真实(mixin 用 isinstance)→ 经 TestEventStubs 构造。
"""

import logging
from types import SimpleNamespace

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from tests.arbitrage.risk._factories import pm_instrument


class _FakeCache:
    def __init__(self):
        self._open = []
        self._inst = {}

    def add_instrument(self, inst):
        self._inst[inst.id] = inst

    def add_residual(self, order):
        self._open.append(order)

    def orders_open(self, instrument_id=None):
        return [o for o in self._open if o.instrument_id == instrument_id]

    def instrument(self, instrument_id):
        return self._inst.get(instrument_id)


class _Base:
    def __init__(self, clock, msgbus, cache):
        self._clock = clock
        self._msgbus = msgbus
        self._cache = cache
        self._log = logging.getLogger("session-test")
        self.sent = []
        self.rejected = []
        self.cancels = []

    def _send_order_event(self, event):
        self.sent.append(event)  # 充当 NT 基类 super()._send_order_event

    def generate_order_rejected(self, *, strategy_id, instrument_id, client_order_id, reason, ts_event):
        self.rejected.append((client_order_id, reason))


class FakeSessionClient(ArbExecutionSessionMixin, _Base):
    def __init__(self, clock, msgbus, cache, leg_settled, pair_registry, timeout_secs):
        _Base.__init__(self, clock, msgbus, cache)
        self._init_arb_session(
            leg_settled=leg_settled,
            session_timeout_secs=timeout_secs,
            pair_registry=pair_registry,
        )

    def _cancel_residual_orders(self, instrument_id, residual):
        self.cancels.append((instrument_id, list(residual)))


def _harness(timeout_secs=30.0):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    leg_settled = LegSettledRegistry()
    pair_registry = PairRegistry()
    client = FakeSessionClient(clock, msgbus, cache, leg_settled, pair_registry, timeout_secs)
    published = []
    msgbus.subscribe("execution.started", lambda m: published.append(("started", m)))
    msgbus.subscribe("execution.finished", lambda m: published.append(("finished", m)))
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=clock)
    return client, clock, cache, leg_settled, pair_registry, published, factory


def _order(factory, instrument, qty=10):
    return factory.limit(instrument.id, OrderSide.BUY, Quantity.from_int(qty), instrument.make_price(0.4))


def _cmd(order):
    return SimpleNamespace(order=order)


# ── submit+track 入口(arm + started + ref-count）──────────────────
def test_submit_track_arms_leg_publishes_started():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is True
    assert leg_settled.any_unsettled("match_1")          # 该腿 armed=false
    assert client._execution_active
    assert published == [("started", {"instrument_id": pm.id, "pair_id": "match_1"})]


# ── cancel-only(残留挂单 → 撤 + reject + 丢弃）────────────────────
def test_cancel_only_when_residual_rejects_and_discards():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    residual = _order(factory, pm)
    cache.add_residual(residual)
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is False   # 丢弃当次 submit
    assert client.cancels and client.cancels[0][0] == pm.id
    assert client.rejected and client.rejected[0][0] == order.client_order_id
    assert not client._execution_active                  # 未建 session
    assert not leg_settled.has_entry("match_1")             # 未 arm


# ── leg_settled 标记(任一 venue 确认事件）─────────────────────────
def test_venue_confirm_marks_leg_settled():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))
    assert leg_settled.any_unsettled("match_1")

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert leg_settled.all_settled("match_1")               # accepted 即置 true
    assert client.sent                                   # super() 仍正常上送
    assert client._execution_active                       # accepted 非终态,session 仍在


# ── 终态(全成 / 撤单 → 结束 + finished + 取消 watchdog）───────────
def test_full_fill_is_terminal_ends_session():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    fill = TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(10))
    client._send_order_event(fill)

    assert not client._execution_active
    assert ("finished", {"instrument_id": pm.id, "pair_id": "match_1"}) in published


def test_partial_fill_not_terminal():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(4)))

    assert client._execution_active                       # 部分成交不终态(绝对超时)
    assert not any(p[0] == "finished" for p in published)


def test_canceled_is_terminal():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_canceled(order))

    assert not client._execution_active
    assert any(p[0] == "finished" for p in published)


# ── 超时(NT clock 绝对超时 → 结束,不补救）───────────────────────
def test_timeout_ends_session():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness(timeout_secs=30.0)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))
    assert client._execution_active

    for handler in clock.advance_time(secs_to_nanos(31.0)):
        handler.handle()                                  # 触发超时 callback

    assert not client._execution_active
    assert any(p[0] == "finished" for p in published)


# ── ref-count(两腿并发 → count>0 直到都结束）─────────────────────
def test_refcount_two_concurrent_legs():
    client, clock, cache, leg_settled, pair_registry, published, factory = _harness()
    pm_h = pm_instrument("match_1", "home", token="h"); cache.add_instrument(pm_h)
    pm_a = pm_instrument("match_1", "away", token="a"); cache.add_instrument(pm_a)
    pair_registry.register("match_1", [pm_h.id, pm_a.id])
    o1 = _order(factory, pm_h); o2 = _order(factory, pm_a)
    client._begin_session(_cmd(o1)); client._begin_session(_cmd(o2))
    assert client._execution_active

    client._send_order_event(TestEventStubs.order_canceled(o1))
    assert client._execution_active                       # 还剩一条在飞
    client._send_order_event(TestEventStubs.order_canceled(o2))
    assert not client._execution_active
