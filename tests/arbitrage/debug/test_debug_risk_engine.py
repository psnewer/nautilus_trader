"""DebugArbitrageLiveRiskEngine —— Q11.2:`skip_check_size` 跳过 NT 父类 `_check_order`,
只跑应用层 `_check_balance` + `_check_profit_gates`。

对应用例:debug-risk.{1-4}
"""

import asyncio

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.arbitrage.debug.config import DebugConfig
from src.arbitrage.debug.config import DebugOverride
from src.arbitrage.debug.risk import DebugArbitrageLiveRiskEngine
from src.arbitrage.risk.config import ArbRiskParams
from src.arbitrage.risk.portfolio import ArbitragePortfolio


def _engine(debug: DebugConfig):
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = TestComponentStubs.cache()
    portfolio = ArbitragePortfolio(msgbus=msgbus, cache=cache, clock=clock)
    portfolio.configure_arb(share=100.0, fx=1.0)
    engine = DebugArbitrageLiveRiskEngine(
        loop=asyncio.new_event_loop(),
        portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock,
        config=LiveRiskEngineConfig(),
        debug=debug,
    )
    engine.configure_arb(ArbRiskParams())
    return engine, portfolio


def _debug_with_skip(active: bool) -> DebugConfig:
    cfg = DebugConfig(enabled=True)
    cfg.overrides["skip_check_size"] = DebugOverride(name="skip_check_size", enabled=active, value=True)
    return cfg


# ── debug-risk.1: skip_check_size 未激活 → 走 super(父类 _check_order)──
def test_skip_inactive_delegates_to_super():
    """debug 总开关 enabled 但 skip_check_size override 未激活 → 走父类(NT min/qty/GTD + 应用层)。"""
    engine, _ = _engine(_debug_with_skip(active=False))
    calls = {"super": 0, "balance": 0, "gates": 0}

    def fake_super(instrument, order):
        calls["super"] += 1
        return True

    # 偷换 super._check_order(直接 patch 实例)
    # 这里更可靠的方式:验证调用流向 —— 把 _check_balance + _check_profit_gates 也 patch,
    # 看在 skip 未激活时 super 路径会跑(它内部不调本类 _check_balance)
    engine._check_balance = lambda *a, **k: (calls.__setitem__("balance", calls["balance"] + 1) or True)
    engine._check_profit_gates = lambda *a, **k: (calls.__setitem__("gates", calls["gates"] + 1) or True)
    # 用 patch ArbitrageLiveRiskEngine._check_order(super 类方法)
    from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
    import unittest.mock as _mock
    with _mock.patch.object(ArbitrageLiveRiskEngine, "_check_order", side_effect=fake_super) as super_mock:
        result = engine._check_order(instrument=object(), order=object())
    assert result is True
    assert super_mock.call_count == 1                    # super 走了
    # skip 未激活时本子类不直接跑 balance/gates(由 super 内部决定)
    assert calls["balance"] == 0 and calls["gates"] == 0


# ── debug-risk.2: skip 激活 + balance + gates 都过 → True,super 不走 ─
def test_skip_active_runs_app_layer_only_and_returns_true():
    engine, _ = _engine(_debug_with_skip(active=True))
    super_called = {"v": 0}
    from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
    import unittest.mock as _mock

    engine._check_balance = lambda *a, **k: True
    engine._check_profit_gates = lambda *a, **k: True
    with _mock.patch.object(ArbitrageLiveRiskEngine, "_check_order",
                            side_effect=lambda *a, **k: super_called.__setitem__("v", super_called["v"] + 1) or True):
        assert engine._check_order(instrument=object(), order=object()) is True
    assert super_called["v"] == 0                       # super 没被调


# ── debug-risk.3: skip 激活 + 余额拒 → False,gates 不跑(短路)─────
def test_skip_active_balance_fails_short_circuits():
    engine, _ = _engine(_debug_with_skip(active=True))
    calls = {"balance": 0, "gates": 0}
    engine._check_balance = lambda *a, **k: (calls.__setitem__("balance", calls["balance"] + 1) or False)
    engine._check_profit_gates = lambda *a, **k: (calls.__setitem__("gates", calls["gates"] + 1) or True)
    assert engine._check_order(instrument=object(), order=object()) is False
    assert calls["balance"] == 1 and calls["gates"] == 0  # gates 短路未跑


# ── debug-risk.4: skip 激活 + 余额过 + gates 拒 → False ──────────
def test_skip_active_gates_fail_returns_false():
    engine, _ = _engine(_debug_with_skip(active=True))
    engine._check_balance = lambda *a, **k: True
    engine._check_profit_gates = lambda *a, **k: False
    assert engine._check_order(instrument=object(), order=object()) is False


# ── 边界:DebugConfig 总开关关 → 子类等同生产(走 super)──────────
def test_debug_disabled_acts_like_production():
    cfg = DebugConfig(enabled=False)
    cfg.overrides["skip_check_size"] = DebugOverride(name="skip_check_size", enabled=True, value=True)
    engine, _ = _engine(cfg)
    from src.arbitrage.risk.engine import ArbitrageLiveRiskEngine
    import unittest.mock as _mock
    with _mock.patch.object(ArbitrageLiveRiskEngine, "_check_order", return_value=True) as super_mock:
        engine._check_order(instrument=object(), order=object())
    assert super_mock.call_count == 1                    # 总开关关 → 走 super
