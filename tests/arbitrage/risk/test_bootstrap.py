"""bootstrap 导入名替换 + ArbContext 注入通道 + wire(risk-6.9.1 + execution launcher)。"""

import asyncio
from types import SimpleNamespace

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

import src.arbitrage.bootstrap as bootstrap
from src.arbitrage.common.leg_settled import LegSettledRegistry
from src.arbitrage.execution import ArbLiveExecutionEngine
from src.arbitrage.risk import ArbitragePortfolio
from src.arbitrage.risk import ArbitrageLiveRiskEngine
from src.arbitrage.risk import ArbRiskParams


def test_install_replaces_kernel_module_names():
    bootstrap.install_arbitrage_engines()
    import nautilus_trader.system.kernel as k
    assert k.Portfolio is ArbitragePortfolio
    assert k.LiveRiskEngine is ArbitrageLiveRiskEngine
    assert k.LiveExecutionEngine is ArbLiveExecutionEngine


def _arb_node():
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("T-000"), clock=clock)
    cache = TestComponentStubs.cache()
    pf = ArbitragePortfolio(msgbus=msgbus, cache=cache, clock=clock)
    eng = ArbitrageLiveRiskEngine(
        loop=asyncio.new_event_loop(), portfolio=pf, msgbus=msgbus,
        cache=cache, clock=clock, config=LiveRiskEngineConfig(),
    )
    return SimpleNamespace(kernel=SimpleNamespace(portfolio=pf, risk_engine=eng))


def test_wire_injects_params_and_returns_shared_registry():
    node = _arb_node()
    registry = LegSettledRegistry()
    params = ArbRiskParams(share=200.0, fx=1.3, match_tp=0.07)

    returned = bootstrap.wire_arbitrage_runtime(node, params=params, leg_settled=registry)

    assert returned is registry                          # 返回同一份供 execution 复用
    assert node.kernel.portfolio._share == 200.0
    assert node.kernel.portfolio._fx == 1.3
    assert node.kernel.portfolio._settled is registry
    assert node.kernel.risk_engine._params.match_tp == 0.07


def test_wire_raises_when_install_skipped():
    # kernel.portfolio 是普通 Portfolio(没先 install)→ 早失败
    clock = LiveClock()
    from nautilus_trader.portfolio.portfolio import Portfolio
    plain = Portfolio(
        msgbus=MessageBus(trader_id=TraderId("T-000"), clock=clock),
        cache=TestComponentStubs.cache(),
        clock=clock,
    )
    node = SimpleNamespace(kernel=SimpleNamespace(portfolio=plain, risk_engine=None))
    with pytest.raises(RuntimeError, match="install_arbitrage_engines"):
        bootstrap.wire_arbitrage_runtime(node)


# ── ArbContext 注入通道(给自定义 exec factory)──────────────────
def test_prepare_get_reset_arb_context():
    bootstrap.reset_arb_context()
    assert bootstrap.get_arb_context().leg_settled is None
    reg = LegSettledRegistry()
    ctx = bootstrap.prepare_arb_context(
        leg_settled=reg, pm_health_interval_secs=8.0,
    )
    assert ctx.leg_settled is reg
    assert bootstrap.get_arb_context() is ctx                # 进程级单例
    assert ctx.pm_health_interval_secs == 8.0
    bootstrap.reset_arb_context()
    assert bootstrap.get_arb_context().leg_settled is None


def test_wire_reuses_context_registry_when_none_passed():
    # launcher 顺序:prepare_arb_context(reg) → factory 用 reg → wire 不传 leg_settled → 用同份
    bootstrap.reset_arb_context()
    reg = LegSettledRegistry()
    bootstrap.prepare_arb_context(leg_settled=reg)
    node = _arb_node()
    returned = bootstrap.wire_arbitrage_runtime(node)         # 不传 leg_settled
    assert returned is reg                                    # 复用 context 那份(execution 也用同份)
    assert node.kernel.portfolio._settled is reg
    bootstrap.reset_arb_context()
