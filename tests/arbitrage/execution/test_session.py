"""ArbExecutionSessionMixin —— session 入口 / 终态 / 超时 / 同步。

mixin 只触及 cache.orders_open + cache.instrument → 用 FakeCache 轻量化;
order event 须真实(mixin 用 isinstance)→ 经 TestEventStubs 构造。
"""

import asyncio
import logging
from types import SimpleNamespace

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from src.arbitrage.common.pair_inflight import PairInFlightGate
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.execution.session import accepted_order_reserved_notional
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from tests.arbitrage.risk._factories import oe_account_state
from tests.arbitrage.risk._factories import oe_instrument
from tests.arbitrage.risk._factories import pm_account_state
from tests.arbitrage.risk._factories import pm_instrument
from tests.arbitrage.risk._factories import se_account_state
from tests.arbitrage.risk._factories import se_instrument


class _FakeCache:
    def __init__(self):
        self._open = []
        self._inst = {}
        self._orders = {}
        self._accounts = {}

    def add_instrument(self, inst):
        self._inst[inst.id] = inst

    def add_order(self, order):
        self._orders[order.client_order_id] = order

    def add_account(self, venue, account):
        self._accounts[venue] = account

    def add_residual(self, order):
        self._open.append(order)

    def orders_open(self, instrument_id=None):
        return [o for o in self._open if o.instrument_id == instrument_id]

    def instrument(self, instrument_id):
        return self._inst.get(instrument_id)

    def order(self, client_order_id):
        return self._orders.get(client_order_id)

    def account_for_venue(self, venue=None, account_id=None):
        return self._accounts.get(venue)


class _Base:
    def __init__(self, clock, msgbus, cache):
        self._clock = clock
        self._msgbus = msgbus
        self._cache = cache
        self._log = logging.getLogger("session-test")
        self.sent = []
        self.rejected = []
        self.cancels = []
        self.account_states = []
        self._loop = asyncio.new_event_loop()

    def _send_order_event(self, event):
        self.sent.append(event)  # 充当 NT 基类 super()._send_order_event

    def generate_order_rejected(self, *, strategy_id, instrument_id, client_order_id, reason, ts_event):
        self.rejected.append((client_order_id, reason))

    def generate_account_state(self, *, balances, margins, reported, ts_event, info=None):
        self.account_states.append(
            {
                "balances": balances,
                "margins": margins,
                "reported": reported,
                "ts_event": ts_event,
                "info": info or {},
            },
        )


class FakeSessionClient(ArbExecutionSessionMixin, _Base):
    def __init__(self, clock, msgbus, cache, pair_registry, timeout_secs, pair_inflight=None):
        _Base.__init__(self, clock, msgbus, cache)
        self._init_arb_session(
            session_timeout_secs=timeout_secs,
            pair_registry=pair_registry,
            pair_inflight=pair_inflight,
        )

    def _cancel_residual_orders(self, instrument_id, residual):
        self.cancels.append((instrument_id, list(residual)))


class FakeTrackedCancelClient(FakeSessionClient):
    async def _cancel_residual_one(self, order):
        self.cancels.append(("residual_one", order))


def _harness(timeout_secs=30.0, pair_inflight=None):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    pair_registry = PairRegistry()
    client = FakeSessionClient(clock, msgbus, cache, pair_registry, timeout_secs, pair_inflight)
    published = []  # #108 退役探针:execution.* 应永不再 publish → published 始终 []
    msgbus.subscribe("execution.started", lambda m: published.append(("started", m)))
    msgbus.subscribe("execution.finished", lambda m: published.append(("finished", m)))
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=clock)
    return client, clock, cache, pair_registry, published, factory


def _order(factory, instrument, qty=10, *, side=OrderSide.BUY, price=0.4):
    return factory.limit(instrument.id, side, Quantity.from_int(qty), instrument.make_price(price))


def _cmd(order):
    return SimpleNamespace(order=order)


# ── submit+track 入口(started + ref-count）──────────────────
def test_submit_track_marks_execution_active():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is True
    assert client._execution_active
    assert published == []                               # #108:不再 publish execution.started


# ── cancel-only(残留挂单 → 撤 + reject + 丢弃）────────────────────
def test_cancel_only_when_residual_rejects_and_discards(caplog):
    caplog.set_level(logging.INFO, logger="session-test")
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    residual = _order(factory, pm)
    cache.add_residual(residual)
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is False   # 丢弃当次 submit
    assert client.cancels and client.cancels[0][0] == pm.id
    assert client.rejected and client.rejected[0][0] == order.client_order_id
    assert not client._execution_active                  # 未建 session
    assert "Execution session cancel-only" in caplog.text
    assert str(residual.client_order_id) in caplog.text


def test_accepted_keeps_session_active(caplog):
    caplog.set_level(logging.INFO, logger="session-test")
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.sent                                   # super() 仍正常上送
    assert client._execution_active                       # accepted 非终态,session 仍在
    assert "Execution session accepted" in caplog.text
    assert str(order.client_order_id) in caplog.text


def test_accepted_reserves_probability_venue_available_balance():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    cache.add_account(pm.id.venue, AccountFactory.create(pm_account_state(100)))
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=50)  # price=0.4, reserved=20
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states
    balance = client.account_states[-1]["balances"][0]
    assert balance.free.as_double() == 80.0
    assert balance.total.as_double() == 80.0
    assert balance.locked.as_double() == 0.0


def test_accepted_probability_sell_reduction_does_not_reserve_cash():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    cache.add_account(pm.id.venue, AccountFactory.create(pm_account_state(100)))
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=50, side=OrderSide.SELL)
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 100.0


def test_accepted_reserves_decimal_venue_available_balance_without_fx():
    client, clock, cache, pair_registry, published, factory = _harness()
    oe = oe_instrument("match_1", "away"); cache.add_instrument(oe)
    cache.add_account(oe.id.venue, AccountFactory.create(oe_account_state(total=100, free=100)))
    pair_registry.register("match_1", [oe.id])
    order = _order(factory, oe, qty=12)  # decimal venue reserved=USD stake quantity, not qty/price/fx
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 88.0


def test_accepted_reserves_decimal_lay_liability():
    client, clock, cache, pair_registry, published, factory = _harness()
    oe = oe_instrument("match_1", "away"); cache.add_instrument(oe)
    cache.add_account(oe.id.venue, AccountFactory.create(oe_account_state(total=100, free=100)))
    pair_registry.register("match_1", [oe.id])
    order = _order(factory, oe, qty=10, side=OrderSide.SELL, price=5.0)
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 60.0


def test_accepted_reserves_sharpexch_available_balance_without_fx():
    client, clock, cache, pair_registry, published, factory = _harness()
    se = se_instrument("match_1", "away"); cache.add_instrument(se)
    cache.add_account(se.id.venue, AccountFactory.create(se_account_state(total=100, free=100)))
    pair_registry.register("match_1", [se.id])
    order = _order(factory, se, qty=12)
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 88.0


def test_accepted_order_reserved_notional_uses_venue_capability():
    pm = pm_instrument("match_1", "home")
    oe = oe_instrument("match_1", "away")

    assert accepted_order_reserved_notional(pm.id, quantity=50.0, price=0.2) == 10.0
    assert accepted_order_reserved_notional(
        pm.id,
        quantity=50.0,
        price=0.8,
        side=OrderSide.SELL,
    ) == 0.0
    assert accepted_order_reserved_notional(oe.id, quantity=12.0, price=1.8) == 12.0
    assert accepted_order_reserved_notional(oe.id, quantity=10.0, price=5.0, side=OrderSide.SELL) == 40.0


# ── 终态(全成 / 撤单 → 结束 + finished + 取消 watchdog）───────────
def test_full_fill_is_terminal_ends_session():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    fill = TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(10))
    client._send_order_event(fill)

    assert not client._execution_active
    assert published == []                               # #108:不再 publish execution.finished


def test_partial_fill_not_terminal():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(4)))

    assert client._execution_active                       # 部分成交不终态(绝对超时)
    assert not any(p[0] == "finished" for p in published)


def test_canceled_is_terminal():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_canceled(order))

    assert not client._execution_active
    assert published == []                               # #108:execution.* 退役


def test_cancel_session_ignores_fill_until_cancel_terminal():
    client, clock, cache, pair_registry, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm, qty=10)

    assert client._begin_cancel_session(order) is True
    client._send_order_event(TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(10)))

    assert client._execution_active                       # cancel session 不由成交事件收口

    client._send_order_event(TestEventStubs.order_canceled(order))

    assert not client._execution_active


def test_base_cancel_only_tracks_residual_until_cancel_terminal():
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    pair_registry = PairRegistry()
    gate = PairInFlightGate()
    client = FakeTrackedCancelClient(clock, msgbus, cache, pair_registry, 30.0, gate)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=clock,
    )
    residual = _order(factory, pm)
    cache.add_residual(residual)
    gate.try_enter("match_1")

    ArbExecutionSessionMixin._cancel_residual_orders(client, pm.id, [residual])

    assert client._execution_active
    assert _exec_count(gate, "match_1") == 1

    client._send_order_event(TestEventStubs.order_canceled(residual))

    assert not client._execution_active
    assert _exec_count(gate, "match_1") == 0


# ── 超时(NT clock 绝对超时 → 结束,不补救）───────────────────────
def test_timeout_ends_session():
    client, clock, cache, pair_registry, published, factory = _harness(timeout_secs=30.0)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    client._begin_session(_cmd(order))
    assert client._execution_active

    for handler in clock.advance_time(secs_to_nanos(31.0)):
        handler.handle()                                  # 触发超时 callback

    assert not client._execution_active
    assert published == []                               # #108:execution.* 退役


# ── ref-count(两腿并发 → count>0 直到都结束）─────────────────────
def test_refcount_two_concurrent_legs():
    client, clock, cache, pair_registry, published, factory = _harness()
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


# ── #105 ②:watchdog 与 exec_started 原子(置位一定有出口）─────────
# 说明:strategy `try_enter` 先置 `_inflight`(is_in_flight 探针);session `exec_started` 置
# `_exec_count`。下列测试先 try_enter 模拟 strategy 已 acquire,再验 exec_started/exec_finished 的 count。
def _exec_count(gate, pair_id):
    return gate._exec_count.get(pair_id, 0)


def test_watchdog_armed_before_exec_started_no_leak_on_alert_failure():
    """set_time_alert 抛(本块唯一可能抛的操作)→ 尚未 exec_started → exec_count 不会 ++ 而无看门狗;
    eval 层 in-flight 仍可由 strategy `release_eval` 清(因 exec_count==0)。"""
    gate = PairInFlightGate()
    client, clock, cache, pair_registry, published, factory = _harness(pair_inflight=gate)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    gate.try_enter("match_1")   # strategy 已 acquire

    class _AlertBoomClock:  # Cython TestClock.set_time_alert_ns 只读,代理使其抛
        def __init__(self, real): self._real = real
        def timestamp_ns(self): return self._real.timestamp_ns()
        def set_time_alert_ns(self, **kwargs): raise RuntimeError("clock alert failed")
    client._clock = _AlertBoomClock(clock)

    try:
        client._begin_session(_cmd(order))
    except RuntimeError:
        pass
    assert _exec_count(gate, "match_1") == 0             # exec_started 未触达 → 无悬挂 count
    assert not client._execution_active                   # session 未建立
    gate.release_eval("match_1")                          # exec_count==0 → 正常释放
    assert not gate.is_in_flight("match_1")


def test_begin_session_arms_watchdog_and_exec_started():
    gate = PairInFlightGate()
    client, clock, cache, pair_registry, published, factory = _harness(pair_inflight=gate)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    gate.try_enter("match_1")

    assert client._begin_session(_cmd(order)) is True
    assert _exec_count(gate, "match_1") == 1             # exec_started 已 ++
    assert client._clock.timer_count >= 1                 # watchdog 已 arm


def test_end_session_clears_inflight_even_if_publish_throws():
    """#105 ②(出口对称):_publish_execution 抛 → exec_finished 已先行,in-flight 仍被清。"""
    gate = PairInFlightGate()
    client, clock, cache, pair_registry, published, factory = _harness(pair_inflight=gate)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    pair_registry.register("match_1", [pm.id])
    order = _order(factory, pm)
    gate.try_enter("match_1")
    client._begin_session(_cmd(order))
    assert gate.is_in_flight("match_1") and _exec_count(gate, "match_1") == 1

    def _boom(*args, **kwargs):
        raise RuntimeError("msgbus publish failed")
    client._publish_execution = _boom

    try:
        client._end_session(order.client_order_id)
    except RuntimeError:
        pass
    assert _exec_count(gate, "match_1") == 0            # exec_finished 已先行
    assert not gate.is_in_flight("match_1")             # 归 0 → in-flight 被清(publish 抛之前)
