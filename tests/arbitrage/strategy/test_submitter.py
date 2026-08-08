"""`make_submitter` 构造 NT Order，并委托 `Strategy.submit_order`。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import PositionId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.orders import LimitOrder

from src.arbitrage.strategy.actor import make_submitter


def _fake_instrument(size_precision=2, price_precision=2):
    return SimpleNamespace(size_precision=size_precision, price_precision=price_precision)


def _pm_like_instrument(size_precision=6, order_size_increment=0.01):
    # PM 双轨精度:持仓精度存 token 真值(6),下单网格经 info 传(0.01)。
    return SimpleNamespace(
        size_precision=size_precision,
        price_precision=2,
        info={"order_size_increment": order_size_increment},
    )


def _build():
    """共享 fixture:返 (submit, cache_mock, submitted_orders)。"""
    cache = MagicMock()
    submitted = []
    order_factory = OrderFactory(
        trader_id=TraderId("TEST-001"),
        strategy_id=StrategyId("ARB-EVAL-001"),
        clock=TestClock(),
    )
    submit = make_submitter(
        cache=cache,
        order_factory=order_factory,
        submit_order=submitted.append,
        log=MagicMock(),
    )
    return submit, cache, submitted


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_submit_builds_limit_order_and_delegates_to_strategy():
    """spec → LimitOrder(side/qty/price/precision)→ Strategy.submit_order。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument(size_precision=3, price_precision=2)
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({
        "instrument_id": iid,
        "side": "BUY",
        "qty": 5.625,   # 3 位精度刚好
        "price": 4.0,
    }))

    cache.instrument.assert_called_once_with(iid)
    assert len(submitted) == 1
    order = submitted[0]
    assert isinstance(order, LimitOrder)
    assert order.strategy_id == StrategyId("ARB-EVAL-001")
    assert order.instrument_id == iid
    assert order.side == OrderSide.BUY
    assert float(order.quantity) == 5.625
    assert float(order.price) == 4.0
    assert order.tags == ["arb:intent=arbitrage"]


def test_submit_with_sell_side():
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({"instrument_id": iid, "side": "SELL", "qty": 1.0, "price": 2.5}))

    assert submitted[0].side == OrderSide.SELL


def test_submit_passes_inventory_position_id_to_native_strategy_submit():
    cache = MagicMock()
    cache.instrument.return_value = _fake_instrument()
    submit_order = MagicMock()
    order_factory = OrderFactory(
        trader_id=TraderId("TEST-001"),
        strategy_id=StrategyId("ARB-EVAL-001"),
        clock=TestClock(),
    )
    submit = make_submitter(
        cache=cache,
        order_factory=order_factory,
        submit_order=submit_order,
        log=MagicMock(),
    )

    _run(submit({
        "instrument_id": "X-1.POLYMARKET",
        "side": "SELL",
        "qty": 5.0,
        "price": 0.8,
        "position_id": "X-1.POLYMARKET-EXTERNAL",
    }))

    order = submit_order.call_args.args[0]
    assert order.side == OrderSide.SELL
    assert submit_order.call_args.kwargs == {
        "position_id": PositionId("X-1.POLYMARKET-EXTERNAL"),
    }


def test_submit_marks_recovery_intent_tag():
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("POLYMARKET"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 0.5, "intent": "recovery"}))

    assert submitted[0].tags == ["arb:intent=recovery"]


def test_submit_writes_opportunity_metadata_tags():
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("POLYMARKET"))

    _run(submit({
        "instrument_id": iid,
        "side": "BUY",
        "qty": 1.0,
        "price": 0.5,
        "intent": "recovery",
        "opportunity_id": "opp-1",
        "pair_id": "pair-1",
        "leg_key": "pm:home:0",
        "expected_legs": ("pm:home:0", "oe:away:1"),
        "positions_digest": "positions-1",
        "enable_timeout": False,
        "market": True,
    }))

    assert submitted[0].tags == [
        "arb:opportunity_id=opp-1",
        "arb:pair_id=pair-1",
        "arb:leg_key=pm:home:0",
        "arb:expected_legs=pm:home:0,oe:away:1",
        "arb:intent=recovery",
        "arb:positions_digest=positions-1",
        "arb:enable_timeout=false",
        "arb:market=true",
    ]


def test_submit_skips_when_instrument_missing():
    """cache.instrument 返 None → warning + skip,不 raise,不 send。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = None
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 4.0}))

    assert submitted == []


def test_submit_accepts_string_instrument_id_from_strategy_specs():
    """PlaceBetsAction legs 保留 str 视图;submitter 边界转 NT InstrumentId 再查 cache / 出单。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument(size_precision=2, price_precision=2)

    iid = "1-258949524-4290403-None.ORBITEXCH"

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 2.5}))

    from nautilus_trader.model.identifiers import InstrumentId

    iid_obj = InstrumentId.from_str(iid)
    cache.instrument.assert_called_once_with(iid_obj)
    assert submitted[0].instrument_id == iid_obj


def test_submit_floors_pm_sell_qty_to_order_grid():
    """PM 持仓精度存真值(6),下单量 floor 到 info['order_size_increment']=0.01:
    SELL 平仓 17.046151 → 17.04(≤ 真实持有、绝不超卖,也不留 sub-0.01 尾量 #280)。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = _pm_like_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("0xabc-123"), Venue("POLYMARKET"))

    _run(submit({"instrument_id": iid, "side": "SELL", "qty": 17.046151, "price": 0.35}))

    assert len(submitted) == 1
    assert float(submitted[0].quantity) == 17.04


def test_submit_floors_pm_buy_qty_to_order_grid():
    """BUY 入场 28.6615 → 28.66(floor 到 0.01,防 6 位小数下单量重踩 #280)。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = _pm_like_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("0xabc-123"), Venue("POLYMARKET"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 28.6615, "price": 0.49}))

    assert float(submitted[0].quantity) == 28.66


def test_submit_no_floor_without_order_grid_key():
    """OE/SE instrument 不带 order_size_increment → 不 floor,原样按 size_precision 量化。"""
    submit, cache, submitted = _build()
    cache.instrument.return_value = _fake_instrument(size_precision=3)
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 5.625, "price": 4.0}))

    assert float(submitted[0].quantity) == 5.625
