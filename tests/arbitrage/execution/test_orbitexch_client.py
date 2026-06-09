"""OrbitExchExecutionClient —— 离线可测部分(BALANCE→account_state / modify 拒绝 / submit session 门控)。

完整集成(真 browser/executor、NT→executor 翻译、CURRENT_BETS item→事件、reports)经 /live-test 验。
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig

from src.arbitrage.common.leg_settled import LegSettledRegistry
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.orbitexch.execution import oe_balance_to_account_balances


def _client():
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    return OrbitExchExecutionClient(
        loop=asyncio.new_event_loop(),
        browser_manager=None,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=OrbitExchExecClientConfig(username="u", password="p"),
        leg_settled=LegSettledRegistry(),
    )


def _run(coro):
    """运行 async client 方法,并恢复旧测试依赖的 main-thread 默认 loop。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── 纯映射 ─────────────────────────────────────────────────────────
def test_balance_mapping_total_equals_free():
    bals = oe_balance_to_account_balances(37.49)
    b = bals[0]
    assert b.total == b.free                       # WS 已净挂单 → total=free
    assert b.total.as_double() == pytest.approx(37.49)
    assert b.currency == GBP
    assert b.locked.as_double() == 0.0


# ── general 帧 → 账户状态 ──────────────────────────────────────────
def test_on_general_frame_balance_generates_account_state():
    c = _client()
    captured = {}
    c.generate_account_state = lambda *, balances, margins, reported, ts_event, info=None: captured.update(
        balances=balances, reported=reported,
    )
    c._on_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
    assert captured["reported"] is True
    assert captured["balances"][0].total.as_double() == pytest.approx(37.49)


def test_on_general_frame_unknown_ignored():
    c = _client()
    calls = []
    c.generate_account_state = lambda **k: calls.append(k)
    c._on_general_frame({"SOMETHING": 1})
    assert calls == []


def test_on_general_frame_null_balance_no_account_state():
    c = _client()
    calls = []
    c.generate_account_state = lambda **k: calls.append(k)
    c._on_general_frame({"BALANCE": {"balance": None}})
    assert calls == []


# ── modify 拒绝 ────────────────────────────────────────────────────
def test_modify_order_is_rejected():
    c = _client()
    rejected = {}
    c.generate_order_modify_rejected = lambda *, strategy_id, instrument_id, client_order_id, venue_order_id, reason, ts_event: rejected.update(reason=reason)
    from nautilus_trader.model.identifiers import ClientOrderId
    cmd = SimpleNamespace(
        client_order_id=ClientOrderId("O-1"),
        strategy_id="S", instrument_id="I", venue_order_id=None,
    )
    _run(c._modify_order(cmd))
    assert "does not support" in rejected["reason"]


# ── submit session 门控 + 结果映射(注入 fake _place_via_executor)──
@dataclass
class _FakeResult:
    success: bool
    order: object = None
    message: str = ""


def test_submit_order_rejects_on_executor_failure():
    c = _client()
    events = {}
    c._begin_session = lambda command: True          # 跳过残留检测
    c.generate_order_rejected = lambda *, strategy_id, instrument_id, client_order_id, reason, ts_event: events.update(rej=reason)
    c.generate_order_accepted = lambda **k: events.update(acc=True)

    async def _fail(order):
        return _FakeResult(success=False, message="venue rejected")
    c._place_via_executor = _fail

    order = SimpleNamespace(strategy_id="S", instrument_id="I", client_order_id=SimpleNamespace(value="O-1"))
    _run(c._submit_order(SimpleNamespace(order=order)))
    assert events.get("rej") == "venue rejected" and "acc" not in events


def test_submit_order_cancel_only_discards():
    c = _client()
    placed = []
    c._begin_session = lambda command: False         # cancel-only:丢弃
    c._place_via_executor = lambda order: placed.append(order)
    _run(c._submit_order(SimpleNamespace(order=SimpleNamespace())))
    assert placed == []                              # 未下单


def test_cancel_order_passes_market_id_from_current_bets():
    """Gap C live 暴露:cancel_order 只带 venue_order_id 会被 executor 拒绝 missing market_id。"""
    c = _client()
    captured = {}

    class _FakeExecutor:
        async def cancel_order(self, order, page):
            captured["order"] = order
            return SimpleNamespace(success=True, message="ok")

    c._executor = _FakeExecutor()
    c._page = object()
    c._current_bets = {
        "221972467": {
            "offerId": "221972467",
            "marketId": "1.258977638",
            "selectionId": "8266399",
            "sizeRemaining": "7.00",
        },
    }
    c.generate_order_canceled = lambda *args, **kwargs: captured.update(canceled=True)

    _run(c._cancel_order(SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1-258977638-8266399-None.ORBITEXCH"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=VenueOrderId("221972467"),
    )))

    legacy = captured["order"]
    assert legacy.venue_order_id == "221972467"
    assert legacy.market_id == "1.258977638"
    assert legacy.selection_id == "8266399"
    assert captured["canceled"] is True


# ── Tier 2(真成交)matched 帧 → generate_order_filled ──────────────
def test_on_current_bets_matched_fires_generate_order_filled():
    """Gap C Tier 2(2026-06-09 live offerId=222016509 的**真实 matched 帧值**:
    `sizeMatched=7.00`/`averagePrice=2.3`)→ `_on_current_bets` → `generate_order_filled`
    (`last_qty=delta`, `last_px=avg`)。需 cache 有 order + voi 索引——生产由 ExecEngine apply
    `OrderAccepted` 建该索引;`gapc_fill_probe` 无 ExecEngine 故事件路径未触发(探针局限,非 bug),
    此处离线补全。`liquidity_side=MAKER` 无条件硬编码,**已评估无害**(#83):OE 无 maker/taker 概念、
    fill commission=0、rebate 不读此字段 → 纯名义。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import LiquiditySide
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=4290403)
    c._cache.add_instrument(inst)
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock())
    order = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    voi = VenueOrderId("222016509")
    c._cache.add_venue_order_id(order.client_order_id, voi)

    captured: list = []
    c.generate_order_filled = lambda **kw: captured.append(kw)
    c._on_current_bets([{
        "offerId": "222016509", "marketId": inst.market_id, "selectionId": "4290403",
        "side": "BACK", "sizePlaced": "7.00", "sizeMatched": "7.00",
        "sizeRemaining": "0.00", "averagePrice": "2.3", "price": "1.01",
    }])

    assert len(captured) == 1
    f = captured[0]
    assert f["venue_order_id"] == voi
    assert f["last_qty"].as_double() == 7.0       # delta = sizeMatched - prev(0)
    assert f["last_px"].as_double() == 2.3        # averagePrice(成交均价,非 1.01 限价)
    assert f["liquidity_side"] == LiquiditySide.MAKER    # 硬编码假设
