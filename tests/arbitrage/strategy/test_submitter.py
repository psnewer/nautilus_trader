"""`make_submitter(cache, msgbus, clock, trader_id, log)` 真出 NT `SubmitOrder` cmd 到 msgbus。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_submit_builds_limit_order_and_sends_to_risk_engine():
    """spec → LimitOrder(side/qty/price/precision)→ SubmitOrder cmd → msgbus.send("RiskEngine.execute", cmd)。"""
    submit, cache, msgbus = _build()
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
    msgbus.send.assert_called_once()
    args, kwargs = msgbus.send.call_args
    # send(target, cmd) — first positional 是 topic
    topic = args[0] if args else kwargs.get("endpoint")
    cmd = args[1] if len(args) > 1 else kwargs.get("msg")
    assert topic == "RiskEngine.execute"
    assert isinstance(cmd, SubmitOrder)
    assert isinstance(cmd.order, LimitOrder)
    assert cmd.order.instrument_id == iid
    assert cmd.order.side == OrderSide.BUY
    assert float(cmd.order.quantity) == 5.625
    assert float(cmd.order.price) == 4.0
    assert cmd.order.tags == ["arb:intent=arbitrage"]


def test_submit_with_sell_side():
    submit, cache, msgbus = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({"instrument_id": iid, "side": "SELL", "qty": 1.0, "price": 2.5}))

    cmd = msgbus.send.call_args.args[1]
    assert cmd.order.side == OrderSide.SELL


def test_submit_marks_recovery_intent_tag():
    submit, cache, msgbus = _build()
    cache.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("POLYMARKET"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 0.5, "intent": "recovery"}))

    cmd = msgbus.send.call_args.args[1]
    assert cmd.order.tags == ["arb:intent=recovery"]


def test_submit_writes_opportunity_metadata_tags():
    submit, cache, msgbus = _build()
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
    }))

    cmd = msgbus.send.call_args.args[1]
    assert cmd.order.tags == [
        "arb:opportunity_id=opp-1",
        "arb:pair_id=pair-1",
        "arb:leg_key=pm:home:0",
        "arb:expected_legs=pm:home:0,oe:away:1",
        "arb:intent=recovery",
    ]


def test_submit_skips_when_instrument_missing():
    """cache.instrument 返 None → warning + skip,不 raise,不 send。"""
    submit, cache, msgbus = _build()
    cache.instrument.return_value = None
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 4.0}))

    msgbus.send.assert_not_called()


def test_submit_accepts_string_instrument_id_from_strategy_specs():
    """PlaceBetsAction legs 保留 str 视图;submitter 边界转 NT InstrumentId 再查 cache / 出单。"""
    submit, cache, msgbus = _build()
    cache.instrument.return_value = _fake_instrument(size_precision=2, price_precision=2)

    iid = "1-258949524-4290403-None.ORBITEXCH"

    _run(submit({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 2.5}))

    from nautilus_trader.model.identifiers import InstrumentId

    iid_obj = InstrumentId.from_str(iid)
    cache.instrument.assert_called_once_with(iid_obj)
    cmd = msgbus.send.call_args.args[1]
    assert cmd.order.instrument_id == iid_obj


# ── #260:提交计数(pair 闸的所有权交接判据)────────────────────
def test_submitted_count_starts_at_zero_and_counts_only_real_sends():
    """`submitted_count` 只在 `msgbus.send` 真的发生后 ++。

    `cache.instrument` 返 None 的 skip 分支不能计数 —— 否则 `_evaluate_and_fire` 会误判
    "已交出所有权"而不释放 pair 闸,该 pair 永久停止评估(#260 泄漏的成因之一)。
    """
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue

    submit, cache, msgbus = _build()
    assert submit.submitted_count == 0                 # 工厂置初值

    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))
    spec = {"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 2.0}

    cache.instrument.return_value = None               # 冷启动 / 未订阅 → skip,不 send
    _run(submit(spec))
    assert msgbus.send.call_count == 0
    assert submit.submitted_count == 0

    cache.instrument.return_value = _fake_instrument()
    _run(submit(spec))
    _run(submit(spec))
    assert msgbus.send.call_count == 2
    assert submit.submitted_count == 2


def test_each_make_submitter_call_has_independent_count():
    """`_make_submitter()` 每轮评估新建一份 → 计数按轮隔离,不跨轮累加。"""
    first, cache_a, _ = _build()
    cache_a.instrument.return_value = _fake_instrument()
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    iid = InstrumentId(Symbol("X-1"), Venue("ORBITEXCH"))
    _run(first({"instrument_id": iid, "side": "BUY", "qty": 1.0, "price": 2.0}))
    assert first.submitted_count == 1

    second, _, _ = _build()
    assert second.submitted_count == 0                 # 新一轮从 0 起
