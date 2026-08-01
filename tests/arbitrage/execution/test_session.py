"""ArbExecutionSessionMixin —— session 入口 / 终态 / 超时 / 同步。

mixin 只触及 cache.orders_open + cache.instrument → 用 FakeCache 轻量化;
order event 须真实(mixin 用 isinstance)→ 经 TestEventStubs 构造。
"""

import asyncio
import logging

import pytest
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

from src.arbitrage.execution.session import accepted_order_reserved_notional
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.common.opportunity import OpportunityMeta
from src.arbitrage.common.opportunity import tags_from_meta
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
        self._submit_order_dispatched = []
        self._cancel_order_dispatched = []

    def _send_order_event(self, event):
        self.sent.append(event)  # 充当 NT 基类 super()._send_order_event

    def submit_order(self, command):
        # 充当 NT 基类 `LiveExecutionClient.submit_order`(生产里是 create_task(_submit_order))
        self._submit_order_dispatched.append(command.order.client_order_id)

    def cancel_order(self, command):
        self._cancel_order_dispatched.append(command.client_order_id)

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
    def __init__(self, clock, msgbus, cache, timeout_secs):
        _Base.__init__(self, clock, msgbus, cache)
        self._init_arb_session(session_timeout_secs=timeout_secs)

    def _cancel_residual_orders(self, instrument_id, residual):
        self.cancels.append((instrument_id, list(residual)))


class FakeTrackedCancelClient(FakeSessionClient):
    async def _cancel_residual_one(self, order):
        self.cancels.append(("residual_one", order))


def _harness(timeout_secs=30.0):
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    client = FakeSessionClient(clock, msgbus, cache, timeout_secs)
    published = []  # #108 退役探针:execution.* 应永不再 publish → published 始终 []
    msgbus.subscribe("execution.started", lambda m: published.append(("started", m)))
    msgbus.subscribe("execution.finished", lambda m: published.append(("finished", m)))
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=clock)
    return client, clock, cache, published, factory


def _order(factory, instrument, qty=10, *, side=OrderSide.BUY, price=0.4, tags=None):
    return factory.limit(
        instrument.id,
        side,
        Quantity.from_int(qty),
        instrument.make_price(price),
        tags=tags,
    )


def _cmd(order):
    return SimpleNamespace(order=order)


# ── submit+track 入口(started + ref-count）──────────────────
def test_submit_track_marks_execution_active():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is True
    assert client._execution_active
    assert published == []                               # #108:不再 publish execution.started


def test_cancel_track_marks_execution_active_before_dispatch():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)
    cache.add_order(order)
    command = SimpleNamespace(
        client_order_id=order.client_order_id,
        params={},
    )

    client.cancel_order(command)

    assert client._execution_active
    assert client._cancel_order_dispatched == [order.client_order_id]
    assert command.params["arb_cancel_session_started"] is True


# ── cancel-only(残留挂单 → 撤 + reject + 丢弃）────────────────────
def test_cancel_only_when_residual_rejects_and_discards(caplog):
    caplog.set_level(logging.INFO, logger="session-test")
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
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
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.sent                                   # super() 仍正常上送
    assert client._execution_active                       # accepted 非终态,session 仍在
    assert "Execution session accepted" in caplog.text
    assert str(order.client_order_id) in caplog.text


def test_disabled_timeout_ends_session_on_accepted(caplog):
    caplog.set_level(logging.INFO, logger="session-test")
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home")
    cache.add_instrument(pm)
    order = _order(
        factory,
        pm,
        tags=tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-1",
                pair_id="pair-1",
                leg_key="pm:home:0",
                expected_legs=("pm:home:0",),
                enable_timeout=False,
            ),
        ),
    )
    client._begin_session(_cmd(order))

    accepted = TestEventStubs.order_accepted(order)
    client._send_order_event(accepted)

    assert client.sent == [accepted]
    assert not client._execution_active
    assert "tracking ends on ack" in caplog.text


def test_disabled_timeout_ack_only_ends_own_execution_client_session():
    pm_client, clock, pm_cache, _, factory = _harness()
    se_cache = _FakeCache()
    se_client = FakeSessionClient(clock, pm_client._msgbus, se_cache, 30.0)
    pm = pm_instrument("match_1", "home")
    se = se_instrument("match_1", "away")
    pm_cache.add_instrument(pm)
    se_cache.add_instrument(se)
    tags = tags_from_meta(
        OpportunityMeta(
            opportunity_id="opp-1",
            pair_id="pair-1",
            leg_key="unused",
            expected_legs=("unused",),
            enable_timeout=False,
        ),
    )
    pm_order = _order(factory, pm, tags=tags)
    se_order = _order(factory, se, price=2.0, tags=tags)
    pm_client._begin_session(_cmd(pm_order))
    se_client._begin_session(_cmd(se_order))

    pm_client._send_order_event(TestEventStubs.order_accepted(pm_order))

    assert not pm_client._execution_active
    assert se_client._execution_active
    assert se_order.client_order_id in se_client._active_sessions


def test_enabled_timeout_keeps_session_active_on_accepted():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home")
    cache.add_instrument(pm)
    order = _order(
        factory,
        pm,
        tags=tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-1",
                pair_id="pair-1",
                leg_key="pm:home:0",
                expected_legs=("pm:home:0",),
                enable_timeout=True,
            ),
        ),
    )
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client._execution_active


def test_accepted_reserves_probability_venue_available_balance():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    cache.add_account(pm.id.venue, AccountFactory.create(pm_account_state(100)))
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
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    cache.add_account(pm.id.venue, AccountFactory.create(pm_account_state(100)))
    order = _order(factory, pm, qty=50, side=OrderSide.SELL)
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 100.0


def test_accepted_reserves_decimal_venue_available_balance_without_fx():
    client, clock, cache, published, factory = _harness()
    oe = oe_instrument("match_1", "away"); cache.add_instrument(oe)
    cache.add_account(oe.id.venue, AccountFactory.create(oe_account_state(total=100, free=100)))
    order = _order(factory, oe, qty=12)  # decimal venue reserved=USD stake quantity, not qty/price/fx
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 88.0


def test_accepted_reserves_decimal_lay_liability():
    client, clock, cache, published, factory = _harness()
    oe = oe_instrument("match_1", "away"); cache.add_instrument(oe)
    cache.add_account(oe.id.venue, AccountFactory.create(oe_account_state(total=100, free=100)))
    order = _order(factory, oe, qty=10, side=OrderSide.SELL, price=5.0)
    cache.add_order(order)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_accepted(order))

    assert client.account_states[-1]["balances"][0].free.as_double() == 60.0


def test_accepted_reserves_sharpexch_available_balance_without_fx():
    client, clock, cache, published, factory = _harness()
    se = se_instrument("match_1", "away"); cache.add_instrument(se)
    cache.add_account(se.id.venue, AccountFactory.create(se_account_state(total=100, free=100)))
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
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    fill = TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(10))
    client._send_order_event(fill)

    assert not client._execution_active
    assert published == []                               # #108:不再 publish execution.finished


def test_partial_fill_not_terminal():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm, qty=10)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(4)))

    assert client._execution_active                       # 部分成交不终态(绝对超时)
    assert not any(p[0] == "finished" for p in published)


def test_canceled_is_terminal():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)
    client._begin_session(_cmd(order))

    client._send_order_event(TestEventStubs.order_canceled(order))

    assert not client._execution_active
    assert published == []                               # #108:execution.* 退役


def test_cancel_session_ignores_fill_until_cancel_terminal_when_timeout_enabled():
    # #306:缺失/enable_timeout=true 保持原语义 —— 成交(即使打满)不收口 cancel session,
    # 只有 OrderCanceled / OrderCancelRejected / timeout 能结束。
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm, qty=10)

    assert client._begin_cancel_session(order, enable_timeout=True) is True
    client._send_order_event(TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(10)))

    assert client._execution_active                       # true:成交不收口 cancel session

    client._send_order_event(TestEventStubs.order_canceled(order))

    assert not client._execution_active


@pytest.mark.parametrize("last_qty", [4, 10])
def test_cancel_session_disabled_timeout_ends_on_fill(last_qty):
    # #306:enable_timeout=false 的 cancel session 语义 = "拿到确定的 venue 回执即释放追踪"。
    # 成交(不分部分/全部)是确定回执 → 立即结束;cancel/match 竞态下 venue 抑制
    # OrderCancelRejected 时,成交是唯一能让 session 及时收口的回执,否则空耗 watchdog。
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm, qty=10)

    assert client._begin_cancel_session(order, enable_timeout=False) is True
    client._send_order_event(
        TestEventStubs.order_filled(order, instrument=pm, last_qty=Quantity.from_int(last_qty)),
    )

    assert not client._execution_active                   # false:成交回执即收尾,不分部分/全部


def test_cancel_session_disabled_timeout_ends_on_any_venue_ack():
    # #306:收口是 ack 语义、非成交特判 —— 任一到达本漏斗的 venue 回执(此处用非终态、非成交的
    # OrderAccepted)都让 enable_timeout=false 的 cancel session 结束。
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm, qty=10)

    assert client._begin_cancel_session(order, enable_timeout=False) is True
    client._send_order_event(TestEventStubs.order_accepted(order))

    assert not client._execution_active


def test_disabled_timeout_ends_cancel_session_on_request_ack(caplog):
    caplog.set_level(logging.INFO, logger="session-test")
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home")
    cache.add_instrument(pm)
    order = _order(
        factory,
        pm,
        tags=tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-1",
                pair_id="pair-1",
                leg_key="pm:home:0",
                expected_legs=("pm:home:0",),
                enable_timeout=False,
            ),
        ),
    )

    assert client._begin_cancel_session(order, enable_timeout=False) is True
    client._ack_cancel_session(order.client_order_id, "V-1")

    assert not client._execution_active
    assert "Execution cancel session accepted" in caplog.text
    assert "tracking ends on ack" in caplog.text


@pytest.mark.parametrize("enable_timeout", [None, True])
def test_cancel_request_ack_keeps_session_active_when_timeout_enabled_or_missing(enable_timeout):
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home")
    cache.add_instrument(pm)
    tags = None
    if enable_timeout is not None:
        tags = tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-1",
                pair_id="pair-1",
                leg_key="pm:home:0",
                expected_legs=("pm:home:0",),
                enable_timeout=enable_timeout,
            ),
        )
    order = _order(factory, pm, tags=tags)

    assert client._begin_cancel_session(order, enable_timeout=enable_timeout) is True
    client._ack_cancel_session(order.client_order_id, "V-1")

    assert client._execution_active


def test_cancel_only_does_not_inherit_original_order_enable_timeout():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home")
    cache.add_instrument(pm)
    order = _order(
        factory,
        pm,
        tags=tags_from_meta(
            OpportunityMeta(
                opportunity_id="opp-1",
                pair_id="pair-1",
                leg_key="pm:home:0",
                expected_legs=("pm:home:0",),
                enable_timeout=False,
            ),
        ),
    )

    # 残单 cancel-only 没有撤单命令参数，必须按缺省语义等待撤单终态。
    assert client._begin_cancel_session(order, enable_timeout=None) is True
    client._ack_cancel_session(order.client_order_id, "V-1")

    assert client._execution_active


def test_base_cancel_only_tracks_residual_until_cancel_terminal():
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    client = FakeTrackedCancelClient(clock, msgbus, cache, 30.0)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=clock,
    )
    residual = _order(factory, pm)
    cache.add_residual(residual)

    ArbExecutionSessionMixin._cancel_residual_orders(client, pm.id, [residual])

    # #261:撤单 session 同样计入 `_execution_active` —— 它是 barrier 全局 ≤1 判定的派生源之一,
    # 撤单在飞期间不应放行新机会。
    assert client._execution_active

    client._send_order_event(TestEventStubs.order_canceled(residual))

    assert not client._execution_active


def test_base_cancel_only_skips_residual_with_active_cancel_session(caplog):
    """交叉场景:cancel-only 撤残单时,其中一条残单已有在飞 cancel session。

    per-order dedup(`_begin_cancel_session` 按 client_order_id)→ 已在飞的那条被跳过(不重复撤、不再 create_task),
    另一条正常开 session 撤;整体不被阻塞。多腿机会准入后中途冒出的撤单 session 即走此路。
    """
    caplog.set_level(logging.INFO, logger="session-test")
    clock = TestClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = _FakeCache()
    client = FakeTrackedCancelClient(clock, msgbus, cache, 30.0)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=clock)
    r_active = _order(factory, pm)   # 已有在飞撤单的残单
    r_fresh = _order(factory, pm)    # 未撤过的残单(client_order_id 不同)

    assert client._begin_cancel_session(r_active) is True   # 预置:r_active 撤单已在飞

    scheduled = []                    # 记录 create_task,避免真跑 loop
    def _rec(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()
    client._loop = SimpleNamespace(create_task=_rec)

    ArbExecutionSessionMixin._cancel_residual_orders(client, pm.id, [r_active, r_fresh])

    assert len(scheduled) == 1                                   # 只为 r_fresh 起了撤单任务
    assert r_fresh.client_order_id in client._active_sessions    # r_fresh 新开 session
    assert r_active.client_order_id in client._active_sessions   # r_active 原 session 仍在(未重开)
    assert "skip duplicate cancel" in caplog.text               # r_active 被 dedup 跳过


# ── 超时(NT clock 绝对超时 → 结束,不补救）───────────────────────
def test_timeout_ends_session():
    client, clock, cache, published, factory = _harness(timeout_secs=30.0)
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)
    client._begin_session(_cmd(order))
    assert client._execution_active

    for handler in clock.advance_time(secs_to_nanos(31.0)):
        handler.handle()                                  # 触发超时 callback

    assert not client._execution_active
    assert published == []                               # #108:execution.* 退役


# ── ref-count(两腿并发 → count>0 直到都结束）─────────────────────
def test_refcount_two_concurrent_legs():
    client, clock, cache, published, factory = _harness()
    pm_h = pm_instrument("match_1", "home", token="h"); cache.add_instrument(pm_h)
    pm_a = pm_instrument("match_1", "away", token="a"); cache.add_instrument(pm_a)
    o1 = _order(factory, pm_h); o2 = _order(factory, pm_a)
    client._begin_session(_cmd(o1)); client._begin_session(_cmd(o2))
    assert client._execution_active

    client._send_order_event(TestEventStubs.order_canceled(o1))
    assert client._execution_active                       # 还剩一条在飞
    client._send_order_event(TestEventStubs.order_canceled(o2))
    assert not client._execution_active


# ── watchdog 与 session 置位(置位一定有出口）─────────────────────
# #261:pair 闸已退出执行段,本组只验 session 自身的建立/收口不变量。
def test_alert_failure_leaves_no_half_built_session():
    """set_time_alert 抛(`_begin_order_session` 内唯一可能抛的操作)→ session 不得半建立。

    顺序纪律:watchdog 先 arm,再做纯 dict 置位。若反过来,`_active_sessions` 会留下一条
    没有看门狗的条目 —— 终态不来时永远清不掉,`_execution_active` 恒 True,barrier 从此
    拒绝所有新机会(#261 后该派生量直接决定全局闸)。
    """
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)

    class _AlertBoomClock:  # Cython TestClock.set_time_alert_ns 只读,代理使其抛
        def __init__(self, real): self._real = real
        def timestamp_ns(self): return self._real.timestamp_ns()
        def set_time_alert_ns(self, **kwargs): raise RuntimeError("clock alert failed")
    client._clock = _AlertBoomClock(clock)

    with pytest.raises(RuntimeError):
        client._begin_session(_cmd(order))
    assert not client._execution_active                   # 没有悬挂 session
    assert client._active_sessions == {}


def test_begin_session_arms_watchdog():
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)

    assert client._begin_session(_cmd(order)) is True
    assert client._execution_active
    assert client._clock.timer_count >= 1                 # watchdog 已 arm


def test_submit_order_builds_session_synchronously():
    """#261 承重前提:`submit_order` 返回时 session 必须已存在。

    barrier 在 `_release` 里同步派发各腿,派发与 pop ctx 之间没有 `await`;若 session 留到
    `_submit_order` 协程里才建,派生态会出现空窗,队列里连着的 `[A1,A2,B1,B2]` 会让两个
    机会双双执行。故此处验证的是**同步性**,不是"最终会建"。
    """
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    order = _order(factory, pm)

    assert not client._execution_active
    client.submit_order(_cmd(order))
    # 未跑任何 event loop 迭代,session 就已就位
    assert client._execution_active
    assert client._submit_order_dispatched == [order.client_order_id]   # 且确实下发给了 NT


def test_submit_order_cancel_only_does_not_dispatch():
    """残留挂单 → cancel-only:reject 本单且**不**下发给 NT。"""
    client, clock, cache, published, factory = _harness()
    pm = pm_instrument("match_1", "home"); cache.add_instrument(pm)
    residual = _order(factory, pm)
    cache.add_residual(residual)
    order = _order(factory, pm)

    client.submit_order(_cmd(order))
    assert client._submit_order_dispatched == []          # 没下发
    assert client.rejected                                # 但已 reject 本单
