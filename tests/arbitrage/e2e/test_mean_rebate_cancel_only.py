"""当前主流程闭环:mean_rebate 下一轮机会触发残单 cancel-only。

不测试专门的 strategy recovery 状态机。这里只验证已实现的自然闭环:
PlaceBetsAction 重新 submit → Execution session 入口发现同 instrument 残留挂单 →
撤残留并丢弃本次 submit。
"""

import asyncio
import logging
from types import SimpleNamespace

from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Quantity

from src.arbitrage.common.pair_registry import PairRegistry
from src.arbitrage.execution.session import ArbExecutionSessionMixin
from src.arbitrage.strategy.actions.place_bets import PlaceBetsAction
from src.arbitrage.strategy.condition import EvalContext
from tests.arbitrage.risk._factories import pm_instrument


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Cache:
    def __init__(self):
        self._open = []
        self._inst = {}

    def add_instrument(self, inst):
        self._inst[inst.id] = inst

    def add_open(self, order):
        self._open.append(order)

    def orders_open(self, instrument_id=None):
        return [o for o in self._open if o.instrument_id == instrument_id]

    def instrument(self, instrument_id):
        return self._inst.get(instrument_id)


class _ExecutionClient(ArbExecutionSessionMixin):
    def __init__(self, cache):
        self._clock = TestClock()
        self._msgbus = MessageBus(trader_id=TraderId("T-000"), clock=self._clock)
        self._cache = cache
        self._log = logging.getLogger("e2e-mean-rebate-cancel-only")
        self.submitted = []
        self.cancels = []
        self.rejected = []
        self._init_arb_session(
            session_timeout_secs=30.0,
            pair_registry=PairRegistry(),
        )

    async def submit(self, order):
        if not self._begin_session(SimpleNamespace(order=order)):
            return
        self.submitted.append(order)

    def _cancel_residual_orders(self, instrument_id, residual):
        self.cancels.append((instrument_id, list(residual)))

    def generate_order_rejected(self, *, strategy_id, instrument_id, client_order_id, reason, ts_event):
        self.rejected.append((client_order_id, reason))

    def _send_order_event(self, event):
        pass


def _ctx_with_one_leg(submitter, instrument_id):
    ctx = EvalContext(pair_id="pair_X", submitter=submitter)
    ctx.scratch["legs"] = [{
        "instrument_id": instrument_id,
        "venue": "POLYMARKET",
        "side": "BUY",
        "role": "home",
        "price": 0.40,
        "prob": 0.40,
        "share_if_wins": 10.0,
    }]
    ctx.scratch["mean_rebate_rate"] = 0.10
    return ctx


def test_mean_rebate_next_opportunity_first_cancels_unmatched_residual():
    cache = _Cache()
    instrument = pm_instrument("match_1", "home")
    cache.add_instrument(instrument)
    client = _ExecutionClient(cache)
    factory = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=client._clock,
    )

    async def submitter(spec):
        order = factory.limit(
            instrument_id=spec["instrument_id"],
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(str(spec["qty"])),
            price=instrument.make_price(spec["price"]),
        )
        await client.submit(order)

    action = PlaceBetsAction()

    # 第一次机会:无残留,进入 submit+track。
    _run(action.execute(_ctx_with_one_leg(submitter, instrument.id)))
    assert len(client.submitted) == 1
    assert client.cancels == []

    # 模拟上一轮未成交单仍在 cache.orders_open。
    cache.add_open(client.submitted[0])

    # 下一轮机会:同一 action 重新 submit,session 入口先撤残留并丢弃本次新 submit。
    _run(action.execute(_ctx_with_one_leg(submitter, instrument.id)))

    assert len(client.submitted) == 1
    assert len(client.cancels) == 1
    assert client.cancels[0][0] == instrument.id
    assert client.cancels[0][1] == [client.submitted[0]]
    assert client.rejected
    assert "cancel-only session" in client.rejected[0][1]
