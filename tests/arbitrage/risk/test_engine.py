"""ArbitrageLiveRiskEngine 拦截:cpdef 覆盖派发 + 自 emit deny + 单场 profit gates + 余额。"""

import asyncio

import pytest

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import LiveClock
from nautilus_trader.config import ExecEngineConfig
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.mocks.exec_clients import MockExecutionClient
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.strategy import Strategy

from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
from src.arbitrage.risk.portfolio import OutcomeExposure
from src.arbitrage.risk.portfolio import ArbitragePortfolio
from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from tests.arbitrage.risk._factories import oe_account_state
from tests.arbitrage.risk._factories import oe_instrument
from tests.arbitrage.risk._factories import pm_account_state
from tests.arbitrage.risk._factories import pm_instrument


class _Ctx:
    """共享构造:cache + portfolio + ArbitrageLiveRiskEngine。"""

    def __init__(self, params: ArbRiskParams | None = None, *, venue_liveness=None):
        self.clock = LiveClock()
        self.trader_id = TraderId("TESTER-000")
        self.loop = asyncio.new_event_loop()
        self.msgbus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.cache = TestComponentStubs.cache()
        self.portfolio = ArbitragePortfolio(msgbus=self.msgbus, cache=self.cache, clock=self.clock)
        self.portfolio.configure_arb(share=100.0, fx=1.0)
        self.engine = ArbitrageLiveRiskEngine(
            loop=self.loop,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            config=LiveRiskEngineConfig(),
        )
        self.engine.configure_arb(params or ArbRiskParams(), venue_liveness=venue_liveness)


class _DuckOrder:
    def __init__(self, instrument_id, account_id=None, price=None, qty=None, tags=None):
        self.instrument_id = instrument_id
        self.account_id = account_id
        self._price = price
        self.price = price
        self.leaves_qty = qty
        self.tags = tags or []
        self.client_order_id = "COID-DUCK"

    @property
    def has_price(self):
        return self._price is not None


# ── 单场 profit gates(risk-6.7.2 / 6.7.3 / 6.7.4）────────────────────
def _gate_ctx(params, *, exposures=None):
    ctx = _Ctx(params)
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    # #34:pair_id 来自 PairRegistry,非 info["competition"]。模拟 matching 已注册。
    registry = PairRegistry()
    registry.register("match_X", [pm.id])
    ctx.portfolio.configure_arb(pair_registry=registry)
    ctx.portfolio.outcome_exposures = lambda pair_id, account_id=None: exposures or {}
    denials = []
    ctx.engine._deny_order = lambda order, reason: denials.append(reason)
    order = _DuckOrder(pm.id, AccountId("POLYMARKET-001"))
    return ctx, order, denials


def test_match_tp_gate_denies():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, match_tp=0.05),
        exposures={
            "home": OutcomeExposure(net_profit=1.20, liability=0.0),
            "away": OutcomeExposure(net_profit=1.30, liability=0.0),
        },
    )
    assert ctx.engine._check_profit_gates(order) is False
    assert any("match_tp" in d for d in denials)


def test_match_tp_gate_passes_when_any_outcome_not_above_threshold():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, match_tp=0.05),
        exposures={
            "home": OutcomeExposure(net_profit=1.20, liability=0.0),
            "away": OutcomeExposure(net_profit=1.00, liability=0.0),
        },
    )
    assert ctx.engine._check_profit_gates(order) is True
    assert denials == []


def test_match_sl_gate_denies():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, match_sl=-0.05),
        exposures={
            "home": OutcomeExposure(net_profit=-1.20, liability=2.0),
            "away": OutcomeExposure(net_profit=-1.30, liability=2.0),
        },
    )
    assert ctx.engine._check_profit_gates(order) is False
    assert any("match_sl" in d for d in denials)


def test_match_sl_gate_passes_when_any_outcome_not_below_threshold():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, match_sl=-0.05),
        exposures={
            "home": OutcomeExposure(net_profit=-1.20, liability=2.0),
            "away": OutcomeExposure(net_profit=-1.00, liability=2.0),
        },
    )
    assert ctx.engine._check_profit_gates(order) is True
    assert denials == []


def test_global_sl_no_longer_blocks():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, global_sl=-0.10),
        exposures={
            "home": OutcomeExposure(net_profit=0.0, liability=0.0),
            "away": OutcomeExposure(net_profit=0.0, liability=0.0),
        },
    )
    ctx.portfolio.global_min_rebate_sum = lambda account_id=None: -999.0
    assert ctx.engine._check_profit_gates(order) is True
    assert denials == []


def test_gates_pass_when_within_thresholds():
    ctx, order, denials = _gate_ctx(
        ArbRiskParams(share=22.5, match_tp=0.05, match_sl=-0.05, global_sl=-0.10),
        exposures={
            "home": OutcomeExposure(net_profit=0.50, liability=1.0),
            "away": OutcomeExposure(net_profit=-0.50, liability=1.0),
        },
    )
    assert ctx.engine._check_profit_gates(order) is True
    assert denials == []


def test_liveness_gate_checks_all_expected_leg_venues():
    liveness = VenueExecutionLiveness(("POLYMARKET", "ORBITEXCH"))
    liveness.mark_order_alive("POLYMARKET")
    liveness.mark_position_alive("POLYMARKET")
    ctx = _Ctx(venue_liveness=liveness)
    pm = pm_instrument("match_X", "home")
    order = _DuckOrder(
        pm.id,
        tags=[
            "arb:opportunity_id=opp-1",
            "arb:pair_id=pair-1",
            "arb:leg_key=pm:home:0",
            "arb:expected_legs=pm:home:0,oe:away:1",
        ],
    )
    denials = []
    ctx.engine._deny_order = lambda order, reason: denials.append(reason)

    assert ctx.engine._check_required_venues_alive(order) is False
    assert denials and "ORBITEXCH" in denials[0]


def test_liveness_gate_passes_when_required_venues_alive():
    liveness = VenueExecutionLiveness(("POLYMARKET", "ORBITEXCH"))
    for venue in ("POLYMARKET", "ORBITEXCH"):
        liveness.mark_order_alive(venue)
        liveness.mark_position_alive(venue)
    ctx = _Ctx(venue_liveness=liveness)
    pm = pm_instrument("match_X", "home")
    order = _DuckOrder(
        pm.id,
        tags=[
            "arb:opportunity_id=opp-1",
            "arb:pair_id=pair-1",
            "arb:leg_key=pm:home:0",
            "arb:expected_legs=pm:home:0,oe:away:1",
        ],
    )

    assert ctx.engine._check_required_venues_alive(order) is True


# ── 余额 venue 非对称(risk-6.3b）──────────────────────────────────
def test_balance_pm_self_deducts_open_orders():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))
    ctx.engine._pm_open_notional = lambda venue, currency: 60.0  # 在途挂单占 60
    denials = []
    ctx.engine._deny_order = lambda order, reason: denials.append(reason)
    order = _DuckOrder(pm.id, price=0.4, qty=Quantity.from_int(125))  # cost = 125*0.4 = 50 > (100-60)=40
    assert ctx.engine._check_balance(pm, order) is False
    assert any("Insufficient balance" in d for d in denials)


def test_balance_pm_passes_within_available():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))
    ctx.engine._pm_open_notional = lambda venue, currency: 60.0
    order = _DuckOrder(pm.id, price=0.2, qty=Quantity.from_int(100))   # cost = 20 < 40
    assert ctx.engine._check_balance(pm, order) is True


def test_balance_oe_trusts_free_no_extra_deduction():
    ctx = _Ctx()
    oe = oe_instrument("match_X", "away", 2)
    ctx.cache.add_instrument(oe)
    ctx.cache.add_account(_oe_account(ctx, total=100, free=40))  # WS free 已含占用
    denials = []
    ctx.engine._deny_order = lambda order, reason: denials.append(reason)
    order = _DuckOrder(oe.id, price=2.5, qty=Quantity.from_int(50))    # OE cost = size*fx = 50 > free 40
    assert ctx.engine._check_balance(oe, order) is False
    assert any("Insufficient balance" in d for d in denials)


def _pm_account(ctx, total):
    state = pm_account_state(total)
    from nautilus_trader.accounting.factory import AccountFactory
    return AccountFactory.create(state)


def _oe_account(ctx, total, free):
    from nautilus_trader.accounting.factory import AccountFactory
    return AccountFactory.create(oe_account_state(total, free))


def _register_pair(ctx, pair_id, instruments):
    registry = PairRegistry()
    registry.register(pair_id, [instrument.id for instrument in instruments])
    ctx.portfolio.configure_arb(pair_registry=registry)


# ── cpdef 覆盖派发 + 自 emit deny + 不泄漏(risk-6.7.1）──────────────
def test_check_order_override_dispatched_and_denies_on_real_submit_path():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))

    # exec 链:用于断言 deny 时不路由到 execution
    exec_engine = ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    venue = Venue("POLYMARKET")
    exec_client = MockExecutionClient(
        client_id=ClientId("POLYMARKET"), venue=venue, account_type=AccountType.CASH,
        base_currency=None, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock,
    )
    exec_engine.register_client(exec_client)

    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    # 强制 profit gate deny(只有我们的覆盖会这样拒)
    _register_pair(ctx, "match_X", [pm])
    ctx.portfolio.outcome_exposures = lambda pair_id, account_id=None: {
        "home": OutcomeExposure(net_profit=-2.0, liability=2.0),
        "away": OutcomeExposure(net_profit=-2.0, liability=2.0),
    }
    ctx.engine.configure_arb(ArbRiskParams(share=22.5, match_sl=-0.05), venue_liveness=None)

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4))
    cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                      order=order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    ctx.engine._handle_submit_order(cmd)  # 同步走真实 _check_order 派发链

    assert len(denied) >= 1, "deny 事件未发出(覆盖未派发或未自调 _deny_order)"
    assert exec_engine.command_count == 0, "deny 后订单仍泄漏到 execution"


def test_recovery_intent_skips_profit_gates_on_real_submit_path():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))

    exec_engine = ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    exec_client = MockExecutionClient(
        client_id=ClientId("POLYMARKET"), venue=Venue("POLYMARKET"), account_type=AccountType.CASH,
        base_currency=None, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock,
    )
    exec_engine.register_client(exec_client)
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    # 普通套利会被 profit gate 拦;recovery intent 只跳过 profit gates,应继续路由到 execution。
    _register_pair(ctx, "match_X", [pm])
    ctx.portfolio.outcome_exposures = lambda pair_id, account_id=None: {
        "home": OutcomeExposure(net_profit=-2.0, liability=2.0),
        "away": OutcomeExposure(net_profit=-2.0, liability=2.0),
    }
    ctx.engine.configure_arb(ArbRiskParams(share=22.5, match_sl=-0.05), venue_liveness=None)

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(
        pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4),
        tags=["arb:intent=recovery"],
    )
    cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                      order=order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    ctx.engine._handle_submit_order(cmd)

    assert denied == []
    assert exec_engine.command_count == 1


def test_recovery_intent_still_checks_balance():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=1))

    exec_engine = ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    exec_client = MockExecutionClient(
        client_id=ClientId("POLYMARKET"), venue=Venue("POLYMARKET"), account_type=AccountType.CASH,
        base_currency=None, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock,
    )
    exec_engine.register_client(exec_client)
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(
        pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4),
        tags=["arb:intent=recovery"],
    )
    cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                      order=order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    ctx.engine._handle_submit_order(cmd)

    assert len(denied) >= 1
    assert exec_engine.command_count == 0


def test_share_limit_adjusts_pm_size_before_execution():
    ctx = _Ctx(ArbRiskParams(max_leg_share=20.0))
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))
    _register_pair(ctx, "match_X", [pm])
    ctx.portfolio.outcome_shares = lambda pair_id, account_id=None: {"home": 15.0, "away": 0.0}

    exec_engine = ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    exec_client = MockExecutionClient(
        client_id=ClientId("POLYMARKET"), venue=Venue("POLYMARKET"), account_type=AccountType.CASH,
        base_currency=None, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock,
    )
    exec_engine.register_client(exec_client)

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(
        pm.id,
        OrderSide.BUY,
        Quantity.from_int(10),
        pm.make_price(0.4),
        tags=[
            "arb:opportunity_id=opp-1",
            "arb:pair_id=match_X",
            "arb:leg_key=pm:home:0",
            "arb:expected_legs=pm:home:0,oe:away:1",
        ],
    )
    cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                      order=order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    ctx.engine._handle_submit_order(cmd)

    assert exec_engine.command_count == 1
    routed = exec_client.commands[0]
    assert routed.order.quantity.as_double() == 5.0
    assert "arb:opportunity_id=opp-1" in routed.order.tags


def test_share_limit_adjusted_pm_size_then_native_min_size_denies():
    ctx = _Ctx(ArbRiskParams(max_leg_share=20.0))
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=100))
    _register_pair(ctx, "match_X", [pm])
    ctx.portfolio.outcome_shares = lambda pair_id, account_id=None: {"home": 16.0, "away": 0.0}

    exec_engine = ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    exec_client = MockExecutionClient(
        client_id=ClientId("POLYMARKET"), venue=Venue("POLYMARKET"), account_type=AccountType.CASH,
        base_currency=None, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock,
    )
    exec_engine.register_client(exec_client)
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda e: denied.append(e))

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4))
    cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                      order=order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    ctx.engine._handle_submit_order(cmd)

    assert len(denied) >= 1
    assert exec_engine.command_count == 0
    assert exec_client.commands == []


def test_share_limit_uses_same_multilateral_scale_for_pm_and_oe():
    ctx = _Ctx(ArbRiskParams(max_leg_share=20.0, fx=1.0))
    pm = pm_instrument("match_X", "home")
    oe = oe_instrument("match_X", "away", 2)
    ctx.cache.add_instrument(pm)
    ctx.cache.add_instrument(oe)
    _register_pair(ctx, "match_X", [pm, oe])
    ctx.portfolio.outcome_shares = lambda pair_id, account_id=None: {"home": 15.0, "away": 15.0}

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    tags = [
        "arb:opportunity_id=opp-1",
        "arb:pair_id=match_X",
        "arb:leg_key=pm:home:0",
        "arb:expected_legs=pm:home:0,oe:away:1",
    ]
    pm_order = strategy.order_factory.limit(pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4), tags=tags)
    oe_order = strategy.order_factory.limit(oe.id, OrderSide.BUY, Quantity.from_int(4), oe.make_price(2.5), tags=tags)
    pm_cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                         order=pm_order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())
    oe_cmd = SubmitOrder(trader_id=ctx.trader_id, strategy_id=strategy.id, position_id=None,
                         order=oe_order, command_id=UUID4(), ts_init=ctx.clock.timestamp_ns())

    adjusted_pm = ctx.engine._adjust_submit_order_for_share_limit(pm_cmd)
    adjusted_oe = ctx.engine._adjust_submit_order_for_share_limit(oe_cmd)

    assert adjusted_pm.order.quantity.as_double() == 5.0
    assert adjusted_oe.order.quantity.as_double() == 2.0
    assert adjusted_pm.order.tags == tags
    assert adjusted_oe.order.tags == tags


def test_opportunity_deny_publishes_domain_message():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ctx.cache.add_account(_pm_account(ctx, total=1))
    ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    domain_msgs = []
    ctx.msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=lambda msg: domain_msgs.append(msg))

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(
        pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4),
        tags=[
            "arb:opportunity_id=opp-1",
            "arb:pair_id=pair-1",
            "arb:leg_key=pm:home:0",
            "arb:expected_legs=pm:home:0,oe:away:1",
            "arb:intent=arbitrage",
        ],
    )

    ctx.engine._deny_order(order, "blocked")

    assert domain_msgs == [{
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "pm:home:0",
        "client_order_id": str(order.client_order_id),
        "reason": "blocked",
    }]


def test_non_opportunity_deny_does_not_publish_domain_message():
    ctx = _Ctx()
    pm = pm_instrument("match_X", "home")
    ctx.cache.add_instrument(pm)
    ExecutionEngine(msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock, config=ExecEngineConfig())
    domain_msgs = []
    ctx.msgbus.subscribe(topic=RISK_LEG_DENIED_TOPIC, handler=lambda msg: domain_msgs.append(msg))

    strategy = Strategy()
    strategy.register(trader_id=ctx.trader_id, portfolio=ctx.portfolio, msgbus=ctx.msgbus, cache=ctx.cache, clock=ctx.clock)
    order = strategy.order_factory.limit(pm.id, OrderSide.BUY, Quantity.from_int(10), pm.make_price(0.4))

    ctx.engine._deny_order(order, "blocked")

    assert domain_msgs == []
