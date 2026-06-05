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

from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.currencies import USDC_POS

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.execution_clients import _mock_fill


# ── _mock_fill 行为(纯函数,绕过子类继承)─────────────────────

class _Recorder:
    """记录 generate_order_* 调用 + 提供 _clock。"""

    def __init__(self):
        self.accepted = []
        self.filled = []
        self._clock = MagicMock()
        self._clock.timestamp_ns.return_value = 12345

    def generate_order_accepted(self, **kw): self.accepted.append(kw)
    def generate_order_filled(self, **kw): self.filled.append(kw)


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


def test_mock_fill_oe_uses_gbp():
    rec = _Recorder()
    cmd = MagicMock(); cmd.order = _make_order()
    _mock_fill(rec, cmd, GBP)
    assert rec.filled[0]["quote_currency"] is GBP


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
            _mock_fill(self, command, USDC_POS)
            return
        await self._super_submit_order(command)


@pytest.mark.asyncio
async def test_submit_order_skip_inactive_calls_super():
    cfg = DebugConfig(enabled=True)  # 总开关开,但没 skip_execution override
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    await client._submit_order(cmd)
    assert client.super_called and not client.accepted and not client.filled


@pytest.mark.asyncio
async def test_submit_order_skip_active_short_circuits():
    cfg = DebugConfig(enabled=True)
    cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    await client._submit_order(cmd)
    assert not client.super_called
    assert len(client.accepted) == 1 and len(client.filled) == 1


@pytest.mark.asyncio
async def test_submit_order_debug_disabled_calls_super():
    cfg = DebugConfig(enabled=False)  # 总开关关
    cfg.overrides["skip_execution"] = DebugOverride(name="skip_execution", enabled=True, value=True)
    client = _FakeSkipPM(debug=cfg)
    cmd = MagicMock(); cmd.order = _make_order()
    await client._submit_order(cmd)
    # 总开关关 → is_override_active 返 False → 走 super
    assert client.super_called and not client.accepted
