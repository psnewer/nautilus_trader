"""Q11.3 SkipExecution{PM,OE}Client `_submit_order` 分支行为。

不构造真 NT ExecutionClient(需 ClobClient/browser_manager/msgbus 等);
用 `_RecorderClient` 假基类替代 `_submit_order` + `generate_order_*` 验证:
- skip 未激活 → super()._submit_order 被调
- skip 激活 → super 不调,Accepted + Filled 各 1 次,参数取自 order
"""

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from py_clob_client_v2.exceptions import PolyApiException

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.currencies import USDC_POS

from nautilus_trader.adapters.polymarket.arb_execution import ArbPolymarketExecutionClient

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.config import MockCategory
from src.arbitrage.debug.config import MockDataItem
from src.arbitrage.debug.execution_clients import _mock_fill
from src.arbitrage.debug.execution_clients import _mock_submit
from src.arbitrage.debug.execution_clients import _mock_submit_with_timeline
from src.arbitrage.debug.execution_clients import SkipExecutionPolymarketClient
from src.arbitrage.debug.timeline import TimelineExecutor


def _run(coro):
    """本环境不依赖 pytest-asyncio;用本地 runner 执行 coroutine。"""
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── _mock_fill 行为(纯函数,绕过子类继承)─────────────────────

class _Recorder:
    """记录 generate_order_* 调用 + 提供 _clock。"""

    def __init__(self):
        self.accepted = []
        self.filled = []
        self.rejected = []
        self.canceled = []
        self.expired = []
        self._clock = MagicMock()
        self._clock.timestamp_ns.return_value = 12345

    def generate_order_accepted(self, **kw): self.accepted.append(kw)
    def generate_order_filled(self, **kw): self.filled.append(kw)
    def generate_order_rejected(self, **kw): self.rejected.append(kw)
    def generate_order_canceled(self, **kw): self.canceled.append(kw)
    def generate_order_expired(self, **kw): self.expired.append(kw)


class _FakeClock:
    """最小 NT Clock 替身:记录 time alert,测试手动触发。"""

    def __init__(self):
        self.now = 1_000
        self.alerts = []

    def timestamp_ns(self):
        return self.now

    def set_time_alert_ns(self, *, name, alert_time_ns, callback):
        self.alerts.append((name, alert_time_ns, callback))

    def trigger_next(self):
        name, alert_time_ns, callback = self.alerts.pop(0)
        self.now = alert_time_ns
        callback(MagicMock(name=name))


def _make_order(price="0.4", qty="10"):
    """模拟 NT Order:提供 _mock_fill 用的属性。"""
    o = MagicMock()
    o.client_order_id = MagicMock(); o.client_order_id.value = "CID-1"
    o.strategy_id = "STRAT-1"
    o.instrument_id = "PM.X"
    o.side = "BUY"
    o.order_type = "LIMIT"
    o.quantity = qty
    o.price = price
    return o


def test_mock_fill_emits_accepted_and_filled_in_order():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    _mock_fill(rec, cmd, USDC_POS)
    assert len(rec.accepted) == 1 and len(rec.filled) == 1
    # venue_order_id 一致(Accepted 与 Filled 同一个 mock id)
    assert rec.accepted[0]["venue_order_id"].value == "MOCK-CID-1"
    assert rec.filled[0]["venue_order_id"].value == "MOCK-CID-1"


def test_mock_fill_pm_uses_usdc():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    _mock_fill(rec, cmd, USDC_POS)
    assert rec.filled[0]["quote_currency"] is USDC_POS
    assert rec.filled[0]["commission"].as_decimal() == Decimal("0")


def test_mock_fill_oe_uses_usd():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    _mock_fill(rec, cmd, USD)
    assert rec.filled[0]["quote_currency"] is USD


def test_mock_fill_market_order_uses_fallback_price():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order(price=None)
    _mock_fill(rec, cmd, USDC_POS)
    # 没 price → 0.5 兜底(测试场景不关心精度)
    assert str(rec.filled[0]["last_px"]) == "0.5"


def test_mock_fill_uses_order_price_when_present():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order(price="0.37")
    _mock_fill(rec, cmd, USDC_POS)
    assert str(rec.filled[0]["last_px"]) == "0.37"


def test_mock_submit_starts_session_then_mock_fills(monkeypatch):
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    calls = []
    rec._begin_session = lambda command: calls.append(("begin", command)) or True
    monkeypatch.setattr("src.arbitrage.debug.execution_clients._mock_fill", lambda client, command, ccy: calls.append(("fill", command, ccy)))

    _mock_submit(rec, cmd, USDC_POS)

    assert calls == [("begin", cmd), ("fill", cmd, USDC_POS)]


def test_mock_submit_cancel_only_skips_mock_fill(monkeypatch):
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    calls = []
    rec._begin_session = lambda command: calls.append(("begin", command)) or False
    monkeypatch.setattr("src.arbitrage.debug.execution_clients._mock_fill", lambda *args: calls.append(("fill", args)))

    _mock_submit(rec, cmd, USDC_POS)

    assert calls == [("begin", cmd)]


def test_mock_submit_with_timeline_falls_back_to_immediate_fill(monkeypatch):
    rec = _Recorder()
    rec._debug = DebugConfig(enabled=True)
    cmd = MagicMock(); cmd.order = _make_order()
    calls = []
    rec._begin_session = lambda command: calls.append(("begin", command)) or True
    monkeypatch.setattr("src.arbitrage.debug.execution_clients._mock_fill", lambda client, command, ccy: calls.append(("fill", command, ccy)))

    _mock_submit_with_timeline(rec, cmd, USDC_POS)

    assert calls == [("begin", cmd), ("fill", cmd, USDC_POS)]


def test_mock_submit_with_timeline_uses_timeline_executor(monkeypatch):
    rec = _Recorder()
    rec._debug = DebugConfig(enabled=True)
    rec._debug.mock_data["timeline"] = MockDataItem(
        id="timeline",
        category=MockCategory.TIMELINE,
        enabled=True,
        data={"steps": [{"event": "ACCEPT"}]},
    )
    cmd = MagicMock(); cmd.order = _make_order()
    calls = []

    class FakeTimelineExecutor:
        def __init__(self, client, debug, quote_currency):
            calls.append(("init", client, debug, quote_currency))

        def has_timeline(self, order):
            calls.append(("has", order))
            return True

        def execute(self, command):
            calls.append(("execute", command))

    monkeypatch.setattr("src.arbitrage.debug.execution_clients.TimelineExecutor", FakeTimelineExecutor)
    monkeypatch.setattr("src.arbitrage.debug.execution_clients._mock_fill", lambda *args: calls.append(("fill", args)))

    _mock_submit_with_timeline(rec, cmd, USDC_POS)

    assert calls == [
        ("init", rec, rec._debug, USDC_POS),
        ("has", cmd.order),
        ("execute", cmd),
    ]


def _timeline_debug(steps):
    cfg = DebugConfig(enabled=True)
    cfg.mock_data["timeline"] = MockDataItem(
        id="timeline",
        category=MockCategory.TIMELINE,
        enabled=True,
        data={"steps": steps},
    )
    return cfg


def _timeline_client(steps):
    rec = _Recorder()
    rec._clock = _FakeClock()
    rec._debug = _timeline_debug(steps)
    rec._begin_session = lambda command: True
    rec._cache = None
    return rec


def test_timeline_executor_partial_fill_then_fills_remaining():
    rec = _timeline_client([
        {"event": "ACCEPT"},
        {"event": "PARTIAL_FILL", "delay_ms": 5, "fill_pct": 0.4},
        {"event": "FILL", "delay_ms": 7},
    ])
    cmd = MagicMock(); cmd.order = _make_order(price="0.37", qty="10")

    TimelineExecutor(rec, rec._debug, USDC_POS).execute(cmd)
    assert len(rec.accepted) == 1
    assert rec.filled == []

    rec._clock.trigger_next()
    assert str(rec.filled[0]["last_qty"]) == "4.00"

    rec._clock.trigger_next()
    assert str(rec.filled[1]["last_qty"]) == "6.00"
    assert str(rec.filled[1]["last_px"]) == "0.37"


def test_timeline_executor_reject_terminates_sequence():
    rec = _timeline_client([
        {"event": "ACCEPT"},
        {"event": "REJECT", "delay_ms": 5, "reject_reason": "NO_BALANCE"},
        {"event": "FILL", "delay_ms": 5},
    ])
    cmd = MagicMock(); cmd.order = _make_order()

    TimelineExecutor(rec, rec._debug, USD).execute(cmd)
    rec._clock.trigger_next()

    assert len(rec.accepted) == 1
    assert rec.rejected[0]["reason"] == "NO_BALANCE"
    assert rec.filled == []
    assert rec._clock.alerts == []


@pytest.mark.parametrize(
    ("event", "bucket"),
    [("CANCEL", "canceled"), ("EXPIRE", "expired")],
)
def test_timeline_executor_terminal_cancel_and_expire(event, bucket):
    rec = _timeline_client([
        {"event": "ACCEPT"},
        {"event": event, "delay_ms": 5},
        {"event": "FILL", "delay_ms": 5},
    ])
    cmd = MagicMock(); cmd.order = _make_order()

    TimelineExecutor(rec, rec._debug, USD).execute(cmd)
    rec._clock.trigger_next()

    assert len(rec.accepted) == 1
    assert len(getattr(rec, bucket)) == 1
    assert rec.filled == []
    assert rec._clock.alerts == []


# ── PM skip connect 容忍 transport 级余额/USER WS 失败 ───────────────

def _skip_debug(active=True):
    cfg = DebugConfig(enabled=True)
    if active:
        cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    return cfg


class _LogRecorder:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)


def _skip_pm_client(debug):
    client = SkipExecutionPolymarketClient.__new__(SkipExecutionPolymarketClient)
    client._debug = debug
    client._log_rec = _LogRecorder()
    return client


def test_skip_pm_connect_tolerates_transport_polyapi_exception(monkeypatch):
    async def fail_transport(self):
        raise PolyApiException(error_msg="Request exception!")

    monkeypatch.setattr(ArbPolymarketExecutionClient, "_connect", fail_transport)
    monkeypatch.setattr(SkipExecutionPolymarketClient, "_log", property(lambda self: self._log_rec))
    client = _skip_pm_client(_skip_debug(active=True))

    _run(client._connect())  # 容忍:不抛(_health.start() 已随 PM HealthCheckLoop 退役删除)

    assert "tolerated transport failure" in client._log_rec.warnings[0]


def test_skip_pm_connect_tolerates_geoblock_preflight_failure(monkeypatch):
    async def fail_preflight(self):
        raise RuntimeError("Polymarket trading is geoblocked for the configured HTTP route")

    monkeypatch.setattr(ArbPolymarketExecutionClient, "_connect", fail_preflight)
    monkeypatch.setattr(SkipExecutionPolymarketClient, "_log", property(lambda self: self._log_rec))
    client = _skip_pm_client(_skip_debug(active=True))

    _run(client._connect())  # 容忍:不抛

    assert "tolerated preflight failure" in client._log_rec.warnings[0]


def test_skip_pm_connect_rethrows_api_level_polyapi_exception(monkeypatch):
    class _Resp:
        status_code = 401
        text = '{"error":"invalid api key"}'

        def json(self):
            return {"error": "invalid api key"}

    async def fail_api(self):
        raise PolyApiException(resp=_Resp(), error_msg={"error": "invalid api key"})

    monkeypatch.setattr(ArbPolymarketExecutionClient, "_connect", fail_api)
    monkeypatch.setattr(SkipExecutionPolymarketClient, "_log", property(lambda self: self._log_rec))
    client = _skip_pm_client(_skip_debug(active=True))

    with pytest.raises(PolyApiException):
        _run(client._connect())  # api-level(有 status_code)仍 re-raise,不容忍


# ── _submit_order 分支(模拟 super,验证调用路径)──────────────

class _FakeSkipPM:
    """伪 SkipExecutionPolymarketClient,只测试分支判断;脱离真基类。"""
    def __init__(self, debug):
        self._debug = debug
        self._clock = MagicMock(); self._clock.timestamp_ns.return_value = 1
        self.accepted = []; self.filled = []
        self.super_called = False

    def generate_order_accepted(self, **kw): self.accepted.append(kw)
    def generate_order_filled(self, **kw): self.filled.append(kw)

    async def _super_submit_order(self, command):
        self.super_called = True

    async def _submit_order(self, command):
        # 复制 SkipExecutionPolymarketClient._submit_order 逻辑
        from src.arbitrage.debug.execution_clients import _SKIP_KEY
        if self._debug.is_override_active(_SKIP_KEY):
            _mock_submit(self, command, USDC_POS)
            return
        await self._super_submit_order(command)

    def _begin_session(self, command):
        return True


def test_submit_order_skip_inactive_calls_super():
    cfg = DebugConfig(enabled=True)  # 总开关开,但没 skip_execution override
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    _run(client._submit_order(cmd))
    assert client.super_called and not client.accepted and not client.filled


def test_submit_order_skip_active_short_circuits():
    cfg = DebugConfig(enabled=True)
    cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    _run(client._submit_order(cmd))
    assert not client.super_called
    assert len(client.accepted) == 1 and len(client.filled) == 1


def test_submit_order_debug_disabled_calls_super():
    cfg = DebugConfig(enabled=False)  # 总开关关
    cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    _run(client._submit_order(cmd))
    # 总开关关 → is_override_active 返 False → 走 super
    assert client.super_called and not client.accepted


# ── 真类分支(PM/OE 对齐)──────────────────────────────────────
# `__new__` 跳过重基类 __init__(需 browser_manager/ClobClient/msgbus 等),只注入 `_debug`;
# monkeypatch 基类 async 方法当 super() 探针。测真实 SkipExecution{PM,OE}Client 分支(非复制逻辑)。

import src.arbitrage.debug.execution_clients as debug_exec  # noqa: E402

from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient  # noqa: E402

from src.arbitrage.debug.execution_clients import SkipExecutionOrbitExchClient  # noqa: E402


def _skip_cfg(active: bool) -> DebugConfig:
    cfg = DebugConfig(enabled=True)
    if active:
        cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    return cfg


def _bare(cls, *, active: bool):
    """构造跳过重基类 init 的实例,仅注入 _debug。"""
    c = cls.__new__(cls)
    c._debug = _skip_cfg(active)
    return c


# ── OE: _submit_order 走 _mock_fill(USD)/ 透传 ──────────────
def test_oe_skip_submit_active_uses_mock_fill_usd(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=True)
    ccys, super_called = [], []
    begins = []
    c._begin_session = lambda command: begins.append(command) or True
    monkeypatch.setattr(debug_exec, "_mock_fill", lambda client, cmd, ccy: ccys.append(ccy))
    async def base_submit(self, command): super_called.append(command)
    monkeypatch.setattr(OrbitExchExecutionClient, "_submit_order", base_submit)
    cmd = MagicMock()
    _run(c._submit_order(cmd))
    assert begins == [cmd]
    assert ccys == [USD] and super_called == []  # OE mock 用 USD,不碰真 venue


def test_oe_skip_submit_cancel_only_skips_mock_fill(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=True)
    ccys = []
    c._begin_session = lambda command: False
    monkeypatch.setattr(debug_exec, "_mock_fill", lambda client, cmd, ccy: ccys.append(ccy))
    _run(c._submit_order(MagicMock()))
    assert ccys == []


def test_oe_skip_submit_inactive_calls_super(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=False)
    ccys, super_called = [], []
    monkeypatch.setattr(debug_exec, "_mock_fill", lambda *a: ccys.append(a))
    async def base_submit(self, command): super_called.append(command)
    monkeypatch.setattr(OrbitExchExecutionClient, "_submit_order", base_submit)
    _run(c._submit_order(MagicMock()))
    assert ccys == [] and len(super_called) == 1


# ── OE: cancel / cancel_all / residual no-op(skip)/ 透传 ────
def test_oe_skip_cancel_noop_when_active(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=True)
    called = []
    async def base(self, command): called.append(command)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_order", base)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_all_orders", base)
    _run(c._cancel_order(MagicMock()))
    _run(c._cancel_all_orders(MagicMock()))
    assert called == []  # mock 模式无真单可撤


def test_oe_skip_cancel_calls_super_when_inactive(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=False)
    called = []
    async def base(self, command): called.append(command)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_order", base)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_all_orders", base)
    _run(c._cancel_order(MagicMock()))
    _run(c._cancel_all_orders(MagicMock()))
    assert len(called) == 2


def test_oe_skip_residual_noop_when_active(monkeypatch):
    """OE 专属 _cancel_residual_one:skip 下 no-op(PM 无此方法)。"""
    c = _bare(SkipExecutionOrbitExchClient, active=True)
    called = []
    async def base(self, order): called.append(order)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_residual_one", base)
    _run(c._cancel_residual_one(MagicMock()))
    assert called == []


def test_oe_skip_residual_calls_super_when_inactive(monkeypatch):
    c = _bare(SkipExecutionOrbitExchClient, active=False)
    called = []
    async def base(self, order): called.append(order)
    monkeypatch.setattr(OrbitExchExecutionClient, "_cancel_residual_one", base)
    _run(c._cancel_residual_one(MagicMock()))
    assert len(called) == 1


# ── PM: cancel / cancel_all no-op(skip)/ 透传(对齐 OE,补 cancel 分支)──
def test_pm_skip_cancel_noop_when_active(monkeypatch):
    c = _bare(SkipExecutionPolymarketClient, active=True)
    called = []
    async def base(self, command): called.append(command)
    monkeypatch.setattr(ArbPolymarketExecutionClient, "_cancel_order", base)
    monkeypatch.setattr(ArbPolymarketExecutionClient, "_cancel_all_orders", base)
    _run(c._cancel_order(MagicMock()))
    _run(c._cancel_all_orders(MagicMock()))
    assert called == []


def test_pm_skip_cancel_calls_super_when_inactive(monkeypatch):
    c = _bare(SkipExecutionPolymarketClient, active=False)
    called = []
    async def base(self, command): called.append(command)
    monkeypatch.setattr(ArbPolymarketExecutionClient, "_cancel_order", base)
    monkeypatch.setattr(ArbPolymarketExecutionClient, "_cancel_all_orders", base)
    _run(c._cancel_order(MagicMock()))
    _run(c._cancel_all_orders(MagicMock()))
    assert len(called) == 2
