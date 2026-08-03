"""OrbitExchExecutionClient —— 离线可测部分(BALANCE→account_state / modify 拒绝 / submit session 门控)。

完整集成(真 browser/executor、NT→executor 翻译、CURRENT_BETS item→事件、reports)经 /live-test 验。
"""

import asyncio
from types import SimpleNamespace

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig

from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.common.control import SetArbitrageParamsCommand
from src.arbitrage.common.control import TOPIC_ARBITRAGE_PARAMS
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.orbitexch.execution import current_bets_to_positions
from nautilus_trader.adapters.orbitexch.execution import oe_balance_to_account_balances


def _client(*, config=None, market_order_enabled=False):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    liveness = VenueExecutionLiveness()
    return OrbitExchExecutionClient(
        loop=asyncio.new_event_loop(),
        browser_manager=None,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=clock,
        instrument_provider=InstrumentProvider(),
        config=config or OrbitExchExecClientConfig(username="u", password="p"),
        venue_liveness=liveness,
        market_order_enabled=market_order_enabled,
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
    assert b.currency == USD
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


def test_on_general_frame_balance_normalized_to_usd():
    c = _client()
    c._fx = 1.3
    captured = {}
    c.generate_account_state = lambda *, balances, margins, reported, ts_event, info=None: captured.update(
        balances=balances, reported=reported,
    )
    c._on_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
    assert captured["balances"][0].total.as_double() == pytest.approx(48.74)


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


def test_arbitrage_fx_command_updates_oe_client():
    c = _client()
    c._msgbus.publish(topic=TOPIC_ARBITRAGE_PARAMS, msg=SetArbitrageParamsCommand(fx=1.31))
    assert c._current_fx() == pytest.approx(1.31)


# ── 登录后弹窗 ────────────────────────────────────────────────────
class _FakePopup:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.waits = []

    async def wait_for(self, *, state, timeout):
        self.waits.append((state, timeout))
        if self.raises:
            raise TimeoutError("popup not visible")


class _FakeLocator:
    def __init__(self, popup):
        self.first = popup


class _FakeMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))


class _FakePage:
    def __init__(self, popup):
        self.popup = popup
        self.mouse = _FakeMouse()
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return _FakeLocator(self.popup)


def test_dismiss_post_login_popup_clicks_main_page_when_popup_visible():
    c = _client()
    popup = _FakePopup()
    c._page = _FakePage(popup)

    _run(c._dismiss_post_login_popup())

    assert c._page.selectors == ['div[class*="_postLoginPopup_"]']
    assert popup.waits == [("visible", 7000)]
    assert c._page.mouse.clicks == [(24, 160)]


def test_dismiss_post_login_popup_timeout_continues_without_click():
    c = _client()
    popup = _FakePopup(raises=True)
    c._page = _FakePage(popup)

    _run(c._dismiss_post_login_popup())

    assert popup.waits == [("visible", 7000)]
    assert c._page.mouse.clicks == []


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
def _fake_result(
    *,
    success: bool,
    message: str = "",
    venue_response: dict | None = None,
    venue_order_id: str | None = None,
) -> dict:
    return {
        "success": success,
        "message": message,
        "venue_response": venue_response or {},
        "venue_order_id": venue_order_id,
    }


def test_submit_order_rejects_on_executor_failure():
    c = _client()
    events = {}
    c._begin_session = lambda command: True          # 跳过残留检测
    c.generate_order_rejected = lambda *, strategy_id, instrument_id, client_order_id, reason, ts_event: events.update(rej=reason)
    c.generate_order_accepted = lambda **k: events.update(acc=True)

    async def _fail(order):
        return _fake_result(success=False, message="venue rejected")
    c._place_via_executor = _fail

    order = SimpleNamespace(strategy_id="S", instrument_id="I", client_order_id=SimpleNamespace(value="O-1"))
    _run(c._submit_order(SimpleNamespace(order=order)))
    assert events.get("rej") == "venue rejected" and "acc" not in events


def test_submit_order_transport_exception_keeps_inflight_session():
    """结果未知时不伪造拒单,保留 SUBMITTED/session 给 NT inflight reconcile。"""
    c = _client()
    events = {}
    ended = []
    c._begin_session = lambda command: True
    c.generate_order_rejected = lambda *, strategy_id, instrument_id, client_order_id, reason, ts_event: events.update(rej=reason)
    c.generate_order_accepted = lambda **k: events.update(acc=True)
    c._end_session = lambda coid: ended.append(coid)

    async def _boom(order):
        raise TimeoutError("playwright page crashed")
    c._place_via_executor = _boom

    coid = SimpleNamespace(value="O-1")
    order = SimpleNamespace(strategy_id="S", instrument_id="I", client_order_id=coid)
    _run(c._submit_order(SimpleNamespace(order=order)))
    assert "rej" not in events
    assert "acc" not in events
    assert ended == []


def test_submit_order_transport_result_keeps_inflight_session():
    c = _client()
    rejected = []
    c._begin_session = lambda command: True
    c.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    async def _unknown(order):
        return _fake_result(
            success=False,
            message="connection reset",
            venue_response={"_transport_error": True},
        )

    c._place_via_executor = _unknown
    order = SimpleNamespace(
        strategy_id="S",
        instrument_id="I",
        client_order_id=SimpleNamespace(value="O-1"),
    )
    _run(c._submit_order(SimpleNamespace(order=order)))

    assert rejected == []


def test_submit_order_cancel_only_discards():
    c = _client()
    placed = []
    # #261:cancel-only 的判定挪到**同步** `submit_order` —— session 必须同步建立,否则
    # barrier 的全局 ≤1 派生态在 `_release` 之后有空窗。`_submit_order` 不再自己判。
    from unittest.mock import patch
    from nautilus_trader.live.execution_client import LiveExecutionClient

    dispatched = []
    c._begin_session = lambda command: False         # cancel-only:丢弃
    c._place_via_executor = lambda order: placed.append(order)
    with patch.object(LiveExecutionClient, "submit_order",
                      lambda self, cmd: dispatched.append(cmd)):
        c.submit_order(SimpleNamespace(order=SimpleNamespace()))
    assert dispatched == []                          # 未下发给 NT(不 create_task)
    assert placed == []                              # 未下单


def test_submit_order_builds_session_before_dispatch():
    """#261 承重前提:session 同步建立后才交 NT create_task。"""
    from unittest.mock import patch
    from nautilus_trader.live.execution_client import LiveExecutionClient

    c = _client()
    order_seq = []
    dispatched = []
    c._begin_session = lambda command: order_seq.append("session") or True
    with patch.object(LiveExecutionClient, "submit_order",
                      lambda self, cmd: order_seq.append("dispatch") or dispatched.append(cmd)):
        c.submit_order(SimpleNamespace(order=SimpleNamespace()))
    assert order_seq == ["session", "dispatch"]      # 顺序不可颠倒
    assert len(dispatched) == 1


def test_submit_order_success_registers_pending_accept_not_immediate_ack():
    """ack 不再由 place 回执触发(见 `_on_current_bets`);回执成功只登记待确认表。"""
    c = _client()
    events = {}
    c._begin_session = lambda command: True
    c.generate_order_accepted = lambda **kwargs: events.update(accepted=kwargs)
    c.generate_order_rejected = lambda **kwargs: events.update(rejected=kwargs)

    async def _place(order):
        return _fake_result(success=True, venue_order_id="OE-OFFER-1")
    c._place_via_executor = _place

    order = SimpleNamespace(
        strategy_id="S", instrument_id="I", client_order_id=ClientOrderId("O-1"),
    )
    _run(c._submit_order(SimpleNamespace(order=order)))

    assert "accepted" not in events
    assert "rejected" not in events
    assert c._pending_accept["OE-OFFER-1"] == ClientOrderId("O-1")


def test_place_via_executor_emits_submitted_before_venue_request():
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=8266399)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    calls = []

    class _Executor:
        async def place_order(self, request, page):
            calls.append("request")
            return _fake_result(success=True, venue_order_id="OE-OFFER-1")

    c._executor = _Executor()
    c._page = object()
    c.generate_order_submitted = lambda **kwargs: calls.append("submitted")

    _run(c._place_via_executor(order))

    assert calls == ["submitted", "request"]


def test_place_via_executor_market_lay_uses_worst_book_price():
    from nautilus_trader.adapters.orbitexch.data import oe_runner_to_book_deltas
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from tests.arbitrage.risk._factories import oe_instrument

    c = _client(market_order_enabled=True)
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=8266399)
    c._cache.add_instrument(inst)
    book = OrderBook(inst.id, BookType.L2_MBP)
    book.apply_deltas(
        oe_runner_to_book_deltas(
            inst.id,
            {
                "back": [{"price": 2.1, "size": 10}],
                "lay": [{"price": 2.0, "size": 10}, {"price": 4.0, "size": 10}],
            },
            1,
        ),
    )
    c._cache.add_order_book(book)
    order = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    ).limit(inst.id, OrderSide.SELL, inst.make_qty(7), inst.make_price(2.0))
    captured = {}

    class _Executor:
        async def place_order(self, request, page):
            captured["request"] = request
            return _fake_result(success=True, venue_order_id="OE-OFFER-1")

    c._executor = _Executor()
    c._page = object()
    c.generate_order_submitted = lambda **kwargs: None

    _run(c._place_via_executor(order))

    assert captured["request"].price == pytest.approx(4.0)


def test_place_via_executor_timeout_releases_page_lock():
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    c._order_io_timeout_secs = 0.001
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=8266399)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))

    class _Executor:
        async def place_order(self, legacy, page):
            await asyncio.Event().wait()

    c._executor = _Executor()
    c._page = object()
    c.generate_order_submitted = lambda **kwargs: None

    with pytest.raises(asyncio.TimeoutError):
        _run(c._place_via_executor(order))
    assert not c._page_lock.locked()


def test_cancel_order_passes_market_id_from_current_bets():
    """Gap C live 暴露:cancel_order 只带 venue_order_id 会被 executor 拒绝 missing market_id。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=8266399)
    c._cache.add_instrument(inst)
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock())
    order = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    voi = VenueOrderId("221972467")
    c._cache.add_venue_order_id(order.client_order_id, voi)
    captured = {}

    class _FakeExecutor:
        async def cancel_order(self, market_id, venue_order_id, page):
            captured["market_id"] = market_id
            captured["venue_order_id"] = venue_order_id
            return _fake_result(success=True)

    c._executor = _FakeExecutor()
    c._page = object()
    c._current_bets = {
        "221972467": {
            "offerId": "221972467",
            "marketId": inst.market_id,
            "selectionId": "8266399",
            "sizeRemaining": "7.00",
        },
    }
    c.generate_order_canceled = lambda *args, **kwargs: captured.update(canceled=True, canceled_kwargs=kwargs)
    c._ack_cancel_session = lambda coid, venue_order_id: captured.update(
        cancel_ack=(coid, venue_order_id),
    )

    _run(c._cancel_order(SimpleNamespace(
        strategy_id="S",
        instrument_id=inst.id,
        client_order_id=order.client_order_id,
        venue_order_id=voi,
    )))

    assert captured["venue_order_id"] == "221972467"
    assert captured["market_id"] == inst.market_id
    assert captured["cancel_ack"] == (order.client_order_id, voi)
    assert "canceled" not in captured

    c._on_current_bets([])                            # 新快照中订单消失 → 撤单完成
    assert captured["canceled"] is True
    assert captured["canceled_kwargs"]["client_order_id"] == order.client_order_id


def test_cancel_order_transport_failure_keeps_order_state():
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.model.identifiers import TraderId
    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=8266399)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"),
        strategy_id=StrategyId("S-000"),
        clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    voi = VenueOrderId("221972467")
    c._cache.add_venue_order_id(order.client_order_id, voi)
    rejected = []

    class _UnknownExecutor:
        async def cancel_order(self, market_id, venue_order_id, page):
            return _fake_result(
                success=False,
                message="connection reset",
                venue_response={"_transport_error": True},
            )

    c._executor = _UnknownExecutor()
    c._page = object()
    c._begin_cancel_session = lambda nt_order: True
    c.generate_order_cancel_rejected = lambda *args, **kwargs: rejected.append((args, kwargs))

    _run(c._cancel_order(SimpleNamespace(
        strategy_id=order.strategy_id,
        instrument_id=inst.id,
        client_order_id=order.client_order_id,
        venue_order_id=voi,
    )))

    assert rejected == []


def test_cancel_order_business_rejection_preserves_executor_message():
    c = _client()
    rejected = []

    class _RejectedExecutor:
        async def cancel_order(self, market_id, venue_order_id, page):
            return _fake_result(success=False, message="offer already settled")

    c._executor = _RejectedExecutor()
    c._page = object()
    c._begin_cancel_session = lambda nt_order: True
    c.generate_order_cancel_rejected = lambda *args, **kwargs: rejected.append((args, kwargs))

    _run(c._cancel_order(SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1-1-1-None.ORBITEXCH"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=VenueOrderId("111"),
    )))

    assert rejected[0][0][4] == "offer already settled"


def test_cancel_residual_one_reuses_existing_session():
    c = _client()
    captured = {}

    async def cancel_one(
        strategy_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        *,
        session_started=False,
    ):
        captured.update(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            session_started=session_started,
        )

    c._cancel_one = cancel_one
    residual = SimpleNamespace(
        strategy_id="S",
        instrument_id="I",
        client_order_id="COID-1",
        venue_order_id="VOI-1",
    )

    _run(c._cancel_residual_one(residual))

    assert captured["session_started"] is True


# ── #105 页锁:并发碰页操作串行 ──────────────────────────────────
def test_page_lock_serializes_concurrent_page_ops():
    """#105:两笔并发 cancel 走 OE ExecClient 页锁 → executor 调用永不并发(max_concurrent==1)。
    NT 命令均 create_task 并发,页锁在资源层串行碰页操作,避免同页并发 placeBets/cancel 丢回执。"""
    c = _client()
    state = {"in_flight": 0, "max_concurrent": 0}

    class _SlowExecutor:
        async def cancel_order(self, market_id, venue_order_id, page):
            state["in_flight"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["in_flight"])
            await asyncio.sleep(0.02)        # 制造重叠窗口:无锁则 max_concurrent==2
            state["in_flight"] -= 1
            return _fake_result(success=True)

    c._executor = _SlowExecutor()
    c._page = object()
    c._current_bets = {}
    c.generate_order_canceled = lambda *a, **k: None
    c.generate_order_cancel_rejected = lambda *a, **k: None

    iid = InstrumentId.from_str("1-1-1-None.ORBITEXCH")

    async def _two():
        await asyncio.gather(
            c._cancel_order(SimpleNamespace(
                strategy_id="S", instrument_id=iid,
                client_order_id=ClientOrderId("O-1"), venue_order_id=VenueOrderId("111"))),
            c._cancel_order(SimpleNamespace(
                strategy_id="S", instrument_id=iid,
                client_order_id=ClientOrderId("O-2"), venue_order_id=VenueOrderId("222"))),
        )

    _run(_two())
    assert state["max_concurrent"] == 1      # 页锁串行 → 永不并发


# ── Tier 2(真成交)matched 帧 → generate_order_filled ──────────────
def test_on_current_bets_matched_fires_generate_order_filled():
    """Gap C Tier 2(2026-06-09 live offerId=222016509 的**真实 matched 帧值**:
    `sizeMatched=7.00`/`averagePrice=2.3`)→ `_on_current_bets` → `generate_order_filled`
    (`last_qty=sizeMatched`, `last_px=avg`)。需 cache 有 order + voi 索引——生产由 ExecEngine apply
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
    assert f["last_qty"].as_double() == 7.0       # sizeMatched 是累计成交量
    assert f["last_px"].as_double() == 2.3        # averagePrice(成交均价,非 1.01 限价)
    assert f["liquidity_side"] == LiquiditySide.MAKER    # 硬编码假设


def test_pending_accept_acks_on_first_current_bets_sighting_unmatched():
    """ack 不看是否成交:offerId 首次出现即 ack,即便 sizeMatched=0(纯挂单)。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=4290403)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    c._pending_accept["222016509"] = order.client_order_id
    accepted = []
    c.generate_order_accepted = lambda **kw: accepted.append(kw)

    c._on_current_bets([{
        "offerId": "222016509", "marketId": inst.market_id, "selectionId": "4290403",
        "side": "BACK", "sizePlaced": "7.00", "sizeMatched": "0.00",
        "sizeRemaining": "7.00", "averagePrice": "0", "price": "1.01",
    }])

    assert len(accepted) == 1
    assert accepted[0]["client_order_id"] == order.client_order_id
    assert accepted[0]["venue_order_id"] == VenueOrderId("222016509")
    assert "222016509" not in c._pending_accept  # 弹出,防重复 ack


def test_pending_accept_acks_during_reload_quiet_frame():
    """ack 与 fill 派生的静默帧规则无关(#255 只管 fill),reload 首帧也照常 ack。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=4290403)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    c._pending_accept["222016509"] = order.client_order_id
    c._reload_frame_pending = True
    accepted = []
    c.generate_order_accepted = lambda **kw: accepted.append(kw)

    c._on_current_bets([{
        "offerId": "222016509", "marketId": inst.market_id, "selectionId": "4290403",
        "side": "BACK", "sizeMatched": "0.00", "sizeRemaining": "7.00",
    }])

    assert len(accepted) == 1
    assert "222016509" not in c._pending_accept


def test_pending_accept_same_frame_fill_resolves_via_newly_acked():
    """同一帧内 offerId 既是首次 ack 又已开始成交:fill 派生要靠 `newly_acked` 兜底
    解析 client_order_id(cache 索引还没跟上刚 enqueue 的 accepted 事件)。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=4290403)
    c._cache.add_instrument(inst)
    order = OrderFactory(
        trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock(),
    ).limit(inst.id, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cache.add_order(order)
    c._pending_accept["222016509"] = order.client_order_id
    accepted = []
    filled = []
    c.generate_order_accepted = lambda **kw: accepted.append(kw)
    c.generate_order_filled = lambda **kw: filled.append(kw)

    c._on_current_bets([{
        "offerId": "222016509", "marketId": inst.market_id, "selectionId": "4290403",
        "side": "BACK", "sizePlaced": "7.00", "sizeMatched": "7.00",
        "sizeRemaining": "0.00", "averagePrice": "2.3", "price": "1.01",
    }])

    assert len(accepted) == 1
    assert len(filled) == 1
    assert filled[0]["client_order_id"] == order.client_order_id
    assert filled[0]["last_qty"].as_double() == pytest.approx(7.0)


def test_on_current_bets_fill_uses_cumulative_raw_matched_and_clamps_remaining():
    """成交判断使用 OE 原始 GBP 累计 sizeMatched,NT last_qty 按剩余量裁剪。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    c._fx = 1.3
    inst = oe_instrument("ATP Stuttgart 2026", "home", selection_id=4290403)
    c._cache.add_instrument(inst)
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock())
    order = factory.limit(inst.id, OrderSide.BUY, inst.make_qty(9.1), inst.make_price(1.01))
    c._cache.add_order(order)
    voi = VenueOrderId("222016509")
    c._cache.add_venue_order_id(order.client_order_id, voi)

    captured: list = []
    c.generate_order_filled = lambda **kw: captured.append(kw)
    bet = {
        "offerId": "222016509", "marketId": inst.market_id, "selectionId": "4290403",
        "side": "BACK", "sizePlaced": "7.00", "sizeMatched": "7.00",
        "sizeRemaining": "0.00", "averagePrice": "2.3", "price": "1.01",
    }

    bet = dict(bet, sizeMatched="8.00")
    c._on_current_bets([bet])

    assert len(captured) == 1
    assert captured[0]["last_qty"].as_double() == pytest.approx(9.1)


# ── #105 持仓聚合 current_bets_to_positions(纯函数,§4.3bis(2))──────
def _pos_bet(market, sel, side, matched, avg, remaining=0.0):
    return {
        "offerId": f"{market}-{sel}-{side}-{int(matched)}", "marketId": market,
        "selectionId": sel, "side": side, "sizeMatched": matched,
        "averagePrice": avg, "sizeRemaining": remaining,
    }


def test_positions_single_back_long():
    out = current_bets_to_positions([_pos_bet("M1", "S1", "BACK", 10.0, 2.0)])
    assert len(out) == 1
    p = out[0]
    assert p["side"] == "LONG" and p["qty"] == pytest.approx(10.0) and p["avg_px"] == pytest.approx(2.0)


def test_positions_two_back_size_weighted_avg():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 100.0, 2.0), _pos_bet("M1", "S1", "BACK", 50.0, 2.2),
    ])
    p = out[0]
    assert p["qty"] == pytest.approx(150.0)
    assert p["avg_px"] == pytest.approx((100 * 2.0 + 50 * 2.2) / 150)


def test_positions_mixed_back_lay_dominant_side_avg():
    # BACK 100@2.0 + LAY 40@3.0 → net 60 LONG;avg_px=BACK 加权 2.0(LAY 只减 qty,不进 avg_px)
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 100.0, 2.0), _pos_bet("M1", "S1", "LAY", 40.0, 3.0),
    ])
    p = out[0]
    assert p["side"] == "LONG" and p["qty"] == pytest.approx(60.0) and p["avg_px"] == pytest.approx(2.0)


def test_positions_lay_dominant_short():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "LAY", 80.0, 3.0), _pos_bet("M1", "S1", "BACK", 30.0, 2.0),
    ])
    p = out[0]
    assert p["side"] == "SHORT" and p["qty"] == pytest.approx(50.0) and p["avg_px"] == pytest.approx(3.0)


def test_positions_net_zero_skipped():
    out = current_bets_to_positions([
        _pos_bet("M1", "S1", "BACK", 30.0, 2.0), _pos_bet("M1", "S1", "LAY", 30.0, 2.5),
    ])
    assert out == []                              # 完全对冲 → FLAT


def test_positions_unmatched_skipped():
    out = current_bets_to_positions([_pos_bet("M1", "S1", "BACK", 0.0, 0.0, remaining=7.0)])
    assert out == []                              # 无 matched 不计


def test_generate_position_status_reports_aggregates():
    from nautilus_trader.model.enums import PositionSide
    from nautilus_trader.model.objects import Quantity

    c = _client()
    c._current_bets = {
        "o1": _pos_bet("1.23", "8266", "BACK", 10.0, 2.0),
        "o2": _pos_bet("1.23", "8266", "BACK", 5.0, 2.2),
    }
    c._last_current_bets_ns = c._clock.timestamp_ns()
    c._mark_exec_frame()
    fake_inst = SimpleNamespace(
        id=InstrumentId.from_str("1-23-8266-None.ORBITEXCH"),
        make_qty=lambda v: Quantity(v, precision=2),
    )
    c._resolve_oe_instrument = lambda m, s: fake_inst

    reports = _run(c.generate_position_status_reports(SimpleNamespace()))
    assert len(reports) == 1
    assert reports.snapshot.is_current(c)
    r = reports[0]
    assert r.position_side == PositionSide.LONG
    assert float(r.quantity) == pytest.approx(15.0)
    assert float(r.avg_px_open) == pytest.approx((10 * 2.0 + 5 * 2.2) / 15)




# ── #105 A1 存活锚(_last_frame_ns / on_frame / _exec_ws_fresh)──────
def test_handler_on_frame_fires_for_heartbeat_and_data_not_empty():
    from nautilus_trader.adapters.orbitexch.websocket_handler import OrbitExchWebSocketHandler

    h = OrbitExchWebSocketHandler(page=None)
    hits = []
    h.on_frame(lambda: hits.append(1))
    h._on_frame_received("orders", "")           # empty → 不触发
    h._on_frame_received("orders", "h")          # SockJS 心跳 → 触发(关键:心跳计入存活)
    h._on_frame_received("orders", 'a["x"]')     # 业务帧 → 触发
    assert len(hits) == 2                         # 'h' + 'a',不含 empty


def test_exec_ws_fresh_lifecycle():
    c = _client()
    assert c._exec_ws_fresh() is False            # 尚未收过帧
    c._mark_exec_frame()
    assert c._exec_ws_fresh() is True             # 刚刷锚 → 新鲜
    c._last_frame_ns = c._clock.timestamp_ns() - (c._exec_ws_idle_timeout_ns + 1)
    assert c._exec_ws_fresh() is False            # 超 idle_timeout → 不新鲜


def test_exec_ws_orders_close_marks_stale():
    c = _client()
    c._mark_exec_frame()
    assert c._exec_ws_fresh() is True

    c._mark_exec_stale("close:orders")

    assert c._exec_ws_fresh() is False


def test_exec_ws_prices_close_does_not_mark_stale():
    c = _client()
    c._mark_exec_frame()
    assert c._exec_ws_fresh() is True

    c._mark_exec_stale("close:prices")

    assert c._exec_ws_fresh() is True


def test_exec_first_frame_resolves_connect_waiter():
    c = _client()
    fut = c._loop.create_future()
    c._first_frame_fut = fut

    c._mark_exec_frame()

    assert fut.done()
    assert c._exec_ws_fresh() is True


def test_connect_ready_waits_for_balance_and_current_bets_signals():
    c = _client()
    captured = {}
    c.generate_account_state = lambda *, balances, margins, reported, ts_event, info=None: captured.update(
        balances=balances,
        reported=reported,
    )

    async def _run_wait():
        loop = asyncio.get_running_loop()
        c._balance_ready_fut = loop.create_future()
        c._current_bets_ready_fut = loop.create_future()
        task = asyncio.create_task(c._wait_for_initial_business_state())
        await asyncio.sleep(0)
        assert not task.done()
        c._on_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
        assert not task.done()
        c._on_current_bets([])
        await task

    _run(_run_wait())

    assert captured["balances"][0].total.as_double() == pytest.approx(37.49)
    assert c._balance_reported is True
    assert c._last_current_bets_ns > 0


def test_connect_ready_consumes_signals_received_before_wait_starts():
    c = _client()

    async def _run_wait():
        loop = asyncio.get_running_loop()
        c._balance_ready_fut = loop.create_future()
        c._current_bets_ready_fut = loop.create_future()
        c.generate_account_state = lambda **kwargs: None

        # 模拟首次导航/登录期间业务帧先到，登录结束后才进入等待。
        c._on_general_frame({"BALANCE": {"balance": "37.49", "avBalance": None}})
        c._on_current_bets([])
        await asyncio.wait_for(c._wait_for_initial_business_state(), timeout=0.1)

    _run(_run_wait())


# ── #105 A2 reload-then-report 机制(_reload_exec_page / _ensure_exec_snapshot_fresh)──
class _FakePageReload:
    def __init__(self, on_reload=None):
        self.reload_count = 0
        self._on_reload = on_reload

    async def reload(self, **kwargs):
        self.reload_count += 1
        if self._on_reload is not None:
            self._on_reload()              # 模拟 reload 后 CURRENT_BETS 重推


def test_reload_current_bets_wait_budget_uses_page_timeout():
    c = _client(config=OrbitExchExecClientConfig(username="u", password="p", page_timeout=4321))
    assert c._reload_bets_wait_ns == 4_321_000_000


def test_ensure_fresh_skips_reload_when_current_bets_and_ws_fresh():
    c = _client()
    c._page = _FakePageReload()
    c._on_current_bets([])                            # 已有完整快照
    c._mark_exec_frame()                            # WS 新鲜
    assert _run(c._ensure_exec_snapshot_fresh()) is True
    assert c._page.reload_count == 0                # 新鲜 → 不 reload


def test_ensure_fresh_reloads_when_ws_fresh_but_no_current_bets():
    c = _client()
    c._page = _FakePageReload(on_reload=lambda: c._on_current_bets([]))
    c._mark_exec_frame()                            # 只有 WS 帧,还没有订单快照
    assert _run(c._ensure_exec_snapshot_fresh()) is True
    assert c._page.reload_count == 1


def test_ensure_fresh_reloads_when_stale_and_succeeds():
    c = _client()
    c._page = _FakePageReload(on_reload=lambda: c._on_current_bets([]))   # reload → CURRENT_BETS 重推
    assert _run(c._ensure_exec_snapshot_fresh()) is True                  # stale → reload → 拿到真值
    assert c._page.reload_count == 1


def test_reload_exec_page_timeout_returns_false():
    c = _client()
    c._reload_bets_wait_ns = 50_000_000             # 0.05s 快超时
    c._page = _FakePageReload(on_reload=None)        # reload 不触发 CURRENT_BETS 重推
    assert _run(c._reload_exec_page()) is False      # CURRENT_BETS 未重推 → reconcile 失败


def test_ensure_fresh_single_flight_one_reload():
    c = _client()
    c._page = _FakePageReload(on_reload=lambda: c._on_current_bets([]))

    async def _two():
        return await asyncio.gather(
            c._ensure_exec_snapshot_fresh(), c._ensure_exec_snapshot_fresh(),
        )

    assert _run(_two()) == [True, True]
    assert c._page.reload_count == 1                 # single-flight → 只 reload 一次


# ── CURRENT_BETS → OE execution liveness 真值锚点 ─────────────────────
def test_on_current_bets_marks_oe_liveness_alive():
    c = _client()
    c._on_current_bets([])                            # 任一 CURRENT_BETS 帧(order 真值)
    assert c._venue_liveness.order_alive("ORBITEXCH")
    assert c._venue_liveness.position_alive("ORBITEXCH")
    assert c._venue_liveness.venue_alive("ORBITEXCH")


def test_query_order_forces_reload_without_pushing_reports():
    """#256:卡在飞仍是 dead → 强制 reload(同 #255 判定逻辑)→ alive,但不再对账
    (不构造/推送任何报告——状态同步交给 WS 监听在后续自然帧里做)。"""
    c = _client()
    calls = []
    c._venue_liveness = SimpleNamespace(
        mark_order_dead=lambda venue: calls.append(("dead", venue)),
        mark_position_dead=lambda venue: calls.append(("position_dead", venue)),
        mark_order_alive=lambda venue: calls.append(("alive", venue)),
        mark_position_alive=lambda venue: calls.append(("position_alive", venue)),
    )

    async def _fresh(*, force=False):
        calls.append(("fresh", force))
        return True

    c._ensure_exec_snapshot_fresh = _fresh

    _run(c._query_order(SimpleNamespace(client_order_id=ClientOrderId("O-1"))))

    assert calls == [
        ("dead", "ORBITEXCH"),
        ("position_dead", "ORBITEXCH"),
        ("fresh", True),
        ("alive", "ORBITEXCH"),
        ("position_alive", "ORBITEXCH"),
    ]
    assert not hasattr(c, "_push_reports_from_snapshot")


def test_query_order_reload_failure_keeps_order_liveness_dead():
    c = _client()
    calls = []
    c._venue_liveness = SimpleNamespace(
        mark_order_dead=lambda venue: calls.append(("dead", venue)),
        mark_position_dead=lambda venue: calls.append(("position_dead", venue)),
        mark_order_alive=lambda venue: calls.append(("alive", venue)),
    )

    async def _stale(*, force=False):
        calls.append(("fresh", force))
        return False

    c._ensure_exec_snapshot_fresh = _stale

    _run(c._query_order(SimpleNamespace(client_order_id=ClientOrderId("O-1"))))

    assert calls == [
        ("dead", "ORBITEXCH"),
        ("position_dead", "ORBITEXCH"),
        ("fresh", True),
    ]


def test_on_current_bets_normal_frame_skips_report_push():
    """#255:常规帧只走事件路径,不推 order/position 报告(同帧双路 = 双计根因)。"""
    c = _client()
    calls = []
    c._emit_cancel_events_from_current_bets = lambda: calls.append("updates")
    c._build_order_report = lambda bet: calls.append("build_report") or "R-A"
    c._build_position_status_reports_from_current_bets = (
        lambda: calls.append("build_positions") or ["P-A"]
    )
    c._venue_liveness = SimpleNamespace(
        mark_order_alive=lambda venue: calls.append("order_alive"),
        mark_position_alive=lambda venue: calls.append("position_alive"),
    )

    c._on_current_bets([{"offerId": "A"}])

    assert calls == ["updates", "order_alive", "position_alive"]


def test_on_current_bets_reload_frame_is_quiet(monkeypatch):
    """#255:reload 后首帧静默 —— 不自派生 fill、不推报告、不标 alive(归触发方)。"""
    from nautilus_trader.adapters.orbitexch import execution as oe_exec

    c = _client()
    calls = []
    monkeypatch.setattr(
        oe_exec, "current_bets_to_fills", lambda bets: calls.append("fills") or [],
    )
    c._build_order_report = lambda bet: calls.append("build_report") or "R-A"
    c._send_order_status_report = lambda report: calls.append("send_report")
    c._build_position_status_reports_from_current_bets = lambda: calls.append("build_positions") or []
    c._venue_liveness = SimpleNamespace(
        mark_order_alive=lambda venue: calls.append("order_alive"),
        mark_position_alive=lambda venue: calls.append("position_alive"),
    )
    c._reload_frame_pending = True

    c._on_current_bets([{"offerId": "A"}])

    assert calls == []                          # 静默:无 fill、无报告、无 alive
    assert c._reload_frame_pending is False
    assert c._last_current_bets_ns > 0          # 快照完成时间仍推进(reload 等待依赖它)

    c._on_current_bets([{"offerId": "A"}])
    assert "fills" in calls                     # 下一常规帧恢复事件路径


def test_reload_exec_page_marks_next_frame_quiet(monkeypatch):
    """#255:任何 reload 入口(含拉取路径 stale-WS)都经 _reload_exec_page 置静默标记。"""
    from nautilus_trader.adapters.orbitexch import execution as oe_exec

    c = _client()
    fills_calls = []
    monkeypatch.setattr(
        oe_exec, "current_bets_to_fills", lambda bets: fills_calls.append("fills") or [],
    )

    class ReloadPage:
        async def reload(self, *, wait_until=None, timeout=None):
            assert c._reload_frame_pending is True  # 标记先于重推
            c._on_current_bets([{"offerId": "A"}])

    c._page = ReloadPage()

    assert _run(c._reload_exec_page()) is True
    assert fills_calls == []                    # reload 帧未自派生 fill
    assert c._reload_frame_pending is False


def test_cancel_io_timeout_releases_page_lock_and_keeps_pending():
    c = _client()
    c._order_io_timeout_secs = 0.001
    rejected = []

    class _Executor:
        async def cancel_order(self, market_id, venue_order_id, page):
            await asyncio.Event().wait()

    c._executor = _Executor()
    c._page = object()
    c.generate_order_cancel_rejected = lambda *args, **kwargs: rejected.append(args)

    _run(c._cancel_order(SimpleNamespace(
        strategy_id="S",
        instrument_id=InstrumentId.from_str("1-1-1-None.ORBITEXCH"),
        client_order_id=ClientOrderId("O-1"),
        venue_order_id=VenueOrderId("111"),
    )))

    assert rejected == []
    assert not c._page_lock.locked()


def test_reconcile_reports_without_current_bets_marks_oe_liveness_dead():
    c = _client()
    c._venue_liveness.mark_order_alive("ORBITEXCH")
    c._venue_liveness.mark_position_alive("ORBITEXCH")

    # #259:快照不可信 = 查询失败 → 抛(返空会被 NT 读成「venue 无挂单/持仓」,
    # 使跳过保护失效、连续对账合成成交抹平真实状态账面)。
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(c.generate_order_status_reports(SimpleNamespace()))
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(c.generate_order_status_report(SimpleNamespace(venue_order_id=None, client_order_id=None)))
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(c.generate_position_status_reports(SimpleNamespace()))

    assert not c._venue_liveness.order_alive("ORBITEXCH")
    assert not c._venue_liveness.position_alive("ORBITEXCH")
    assert not c._venue_liveness.venue_alive("ORBITEXCH")


def test_reconcile_reports_stale_snapshot_reload_failure_marks_dead():
    c = _client()
    c._on_current_bets([])                            # 历史快照曾经存在
    c._reload_bets_wait_ns = 50_000_000
    c._page = _FakePageReload(on_reload=None)         # stale 后 reload 失败

    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(c.generate_order_status_reports(SimpleNamespace()))
    with pytest.raises(RuntimeError, match="exec snapshot not fresh"):
        _run(c.generate_position_status_reports(SimpleNamespace()))

    assert c._page.reload_count == 2                  # order / position 各探一次,均失败
    assert not c._venue_liveness.order_alive("ORBITEXCH")
    assert not c._venue_liveness.position_alive("ORBITEXCH")
    assert not c._venue_liveness.venue_alive("ORBITEXCH")


def test_reconcile_reports_stale_snapshot_reload_success_stays_alive():
    c = _client()
    c._on_current_bets([])
    c._page = _FakePageReload(on_reload=lambda: c._on_current_bets([]))

    order_reports = _run(c.generate_order_status_reports(SimpleNamespace()))
    position_reports = _run(c.generate_position_status_reports(SimpleNamespace()))

    assert order_reports == []
    assert position_reports == []
    assert order_reports.snapshot.is_current(c)
    assert position_reports.snapshot.is_current(c)

    assert c._page.reload_count == 2
    assert c._venue_liveness.order_alive("ORBITEXCH")
    assert c._venue_liveness.position_alive("ORBITEXCH")
    assert c._venue_liveness.venue_alive("ORBITEXCH")


# ── #105 撤单纳入 exec_count(cancel-only 也由 exec_count→0 兜底,不靠 max-hold)──
async def _noop_cancel(order):
    pass


class _CollectLoop:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        return coro


def test_cancel_residual_tracked_keeps_execution_active_until_all_terminal():
    """#261:闸已退出执行段,撤单在飞改由 `_execution_active` 表达。

    这个量是 barrier 全局 ≤1 判定的派生源之一 —— 撤单还没到终态就放行新机会,
    新单会撞上正在被撤的残单。故必须"两条 cancel terminal 到齐"才落回 False。
    """
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.test_kit.stubs.events import TestEventStubs

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    c._pair_registry = SimpleNamespace(get=lambda x: "P1")
    loop = _CollectLoop()
    c._loop = loop
    c._cancel_residual_one = _noop_cancel
    inst = oe_instrument("ATP Stuttgart 2026", "home")
    iid = inst.id
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock())

    o1 = factory.limit(iid, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    o2 = factory.limit(iid, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    c._cancel_residual_orders(iid, [o1, o2])

    assert c._execution_active is True                         # 两个 cancel session 同步建立
    assert len(loop.tasks) == 2                                # 每条残单一个 tracked cancel task

    async def _drain():
        for coro in loop.tasks:
            await coro
    _run(_drain())

    assert c._execution_active is True                         # 撤单请求完成不代表撤单终态
    c._send_order_event(TestEventStubs.order_canceled(o1))
    assert c._execution_active is True
    c._send_order_event(TestEventStubs.order_canceled(o2))
    assert c._execution_active is False                        # 两条 cancel terminal 到齐才落回


def test_cancel_residual_execution_active_held_until_last_cancel():
    """撤单 task 还在跑时 `_execution_active` 不提前落回。"""
    from nautilus_trader.common.factories import OrderFactory
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.test_kit.stubs.events import TestEventStubs

    from tests.arbitrage.risk._factories import oe_instrument

    c = _client()
    c._pair_registry = SimpleNamespace(get=lambda x: "P1")
    loop = _CollectLoop()
    c._loop = loop
    c._cancel_residual_one = _noop_cancel
    inst = oe_instrument("ATP Stuttgart 2026", "home")
    iid = inst.id
    factory = OrderFactory(trader_id=TraderId("T-000"), strategy_id=StrategyId("S-000"), clock=LiveClock())
    o1 = factory.limit(iid, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))
    o2 = factory.limit(iid, OrderSide.BUY, inst.make_qty(7), inst.make_price(1.01))

    c._cancel_residual_orders(iid, [o1, o2])

    async def _drain_one():
        await loop.tasks[0]                                    # 只跑完第一个撤单
    _run(_drain_one())
    assert c._execution_active is True                         # 还有一个撤单没跑完
    c._send_order_event(TestEventStubs.order_canceled(o1))
    assert c._execution_active is True                         # 第二条 cancel terminal 未到
    loop.tasks[1].close()                                      # 测试只验证半程,收尾未 await coroutine
