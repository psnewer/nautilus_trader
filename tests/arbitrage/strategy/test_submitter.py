"""Slice 10a(#50):`make_submitter(cache, msgbus, clock, trader_id, log)` 真出 NT `SubmitOrder` cmd 到 msgbus。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.orders import LimitOrder

from src.arbitrage.strategy.actor import make_submitter


def _fake_instrument(size_precision=2, price_precision=2):
    return SimpleNamespace(size_precision=size_precision, price_precision=price_precision)


def _build():
    """共享 fixture:返 (submit, cache_mock, msgbus_mock)。"""
    cache = MagicMock()
    msgbus = MagicMock()
    clock = MagicMock(); clock.timestamp_ns = MagicMock(return_value=12345)
    submit = make_submitter(
        cache=cache, msgbus=msgbus, clock=clock,
        trader_id=TraderId("TEST-001"), log=MagicMock(),
    )
    return submit, cache, msgbus


@pytest.mark.asyncio
async def test_submit_builds_limit_order_and_sends_to_exec_engine():
    """spec → LimitOrder(side/qty/price/precision)→ SubmitOrder cmd → msgbus.send("ExecEngine.execute", cmd)。"""
    submit, cache, msgbus = _build()
    cache.instrument.return_value = _fake_instrument(size_precision=3, price_precision=2)
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    await submit({
        "instrument_id": iid,
        "side": "BUY",
        "qty": 5.625,   # 3 位精度刚好
        "price": 4.0,
    })

    cache.instrument.assert_called_once_with(iid)
    msgbus.send.assert_called_once()
    args, kwargs = msgbus.send.call_args
    # send(target, cmd) — first positional 是 topic
    topic = args[0] if args else kwargs.get("endpoint")
    cmd = args[1] if len(args) > 1 else kwargs.get("msg")
    assert topic == "ExecEngine.execute"
    assert isinstance(cmd, SubmitOrder)
    assert isinstance(cmd.order, LimitOrder)
    assert cmd.order.instrument_id == iid
    assert cmd.order.side == OrderSide.BUY
    assert float(cmd.order.quantity) == 5.625
    assert float(cmd.order.price) == 4.0


@pytest.mark.asyncio
async def test_submit_with_sell_side():
    submit, cache, msgbus = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    await submit({"instrument_id": iid, "side": "SELL", "qty": 1.0, "price": 2.5})

    cmd = msgbus.send.call_args.args[1]
    assert cmd.order.side == OrderSide.SELL


@pytest.mark.asyncio
async def test_submit_skips_when_instrument_missing():
    """cache.instrument 返 None → warning + skip,不 raise,不 send。"""
    submit, cache, msgbus = _build()
    cache.instrument.return_value = None
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    await submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 4.0})

    msgbus.send.assert_not_called()
