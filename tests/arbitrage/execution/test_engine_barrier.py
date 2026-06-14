"""ArbLiveExecutionEngine opportunity barrier 单测。"""

from __future__ import annotations

import asyncio

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import ExecEngineConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.mocks.exec_clients import MockExecutionClient
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.trading.strategy import Strategy

from src.arbitrage.common.opportunity import RISK_LEG_DENIED_TOPIC
from src.arbitrage.common.pair_inflight import PairInFlightGate
from src.arbitrage.execution.engine import ArbLiveExecutionEngine
from tests.arbitrage.risk._factories import pm_instrument


class _Ctx:
    def __init__(self) -> None:
        self.clock = LiveClock()
        self.trader_id = TraderId("TESTER-000")
        self.loop = asyncio.new_event_loop()
        self.msgbus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.cache = TestComponentStubs.cache()
        self.instrument = pm_instrument("match_X", "home")
        self.cache.add_instrument(self.instrument)
        self.client = MockExecutionClient(
            client_id=ClientId("POLYMARKET"),
            venue=Venue("POLYMARKET"),
            account_type=AccountType.CASH,
            base_currency=None,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )
        self.gate = PairInFlightGate()
        self.engine = ArbLiveExecutionEngine(
            loop=self.loop,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            config=LiveExecEngineConfig(),
            pair_inflight=self.gate,
        )
        self.engine.register_client(self.client)
        self.portfolio = Portfolio(msgbus=self.msgbus, cache=self.cache, clock=self.clock)
        self.strategy = Strategy()
        self.strategy.register(
            trader_id=self.trader_id,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
        )

    def submit_cmd(self, leg_key: str, expected=("pm:home:0", "oe:away:1")) -> SubmitOrder:
        order = self.strategy.order_factory.limit(
            self.instrument.id,
            OrderSide.BUY,
            Quantity.from_int(10),
            self.instrument.make_price(0.4),
            tags=[
                "arb:opportunity_id=opp-1",
                "arb:pair_id=pair-1",
                f"arb:leg_key={leg_key}",
                f"arb:expected_legs={','.join(expected)}",
                "arb:intent=arbitrage",
            ],
        )
        return SubmitOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy.id,
            position_id=None,
            order=order,
            command_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
        )


def test_barrier_waits_until_all_legs_pass_before_release():
    ctx = _Ctx()
    first = ctx.submit_cmd("pm:home:0")
    second = ctx.submit_cmd("oe:away:1")

    ctx.engine._execute_command(first)
    assert len(ctx.client.commands) == 0

    ctx.engine._execute_command(second)
    assert len(ctx.client.commands) == 2


def test_barrier_deny_blocks_pending_leg_and_releases_pair_gate():
    ctx = _Ctx()
    ctx.gate.try_enter("pair-1", ctx.clock.timestamp_ns(), 10_000_000_000)
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")

    ctx.engine._execute_command(first)
    ctx.msgbus.publish(topic=RISK_LEG_DENIED_TOPIC, msg={
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "oe:away:1",
        "client_order_id": "x",
        "reason": "risk blocked",
    })

    assert len(ctx.client.commands) == 0
    assert denied
    assert not ctx.gate.is_in_flight("pair-1")


def test_barrier_timeout_blocks_pending_leg_and_releases_pair_gate():
    ctx = _Ctx()
    ctx.gate.try_enter("pair-1", ctx.clock.timestamp_ns(), 10_000_000_000)
    denied = []
    ctx.msgbus.subscribe(topic="events.order.*", handler=lambda event: denied.append(event))
    first = ctx.submit_cmd("pm:home:0")

    ctx.engine._execute_command(first)
    ctx.engine._on_opportunity_timeout(type("Evt", (), {"name": "arb_opp_timeout:opp-1"})())

    assert len(ctx.client.commands) == 0
    assert denied
    assert not ctx.gate.is_in_flight("pair-1")
