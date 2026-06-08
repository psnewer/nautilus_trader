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
