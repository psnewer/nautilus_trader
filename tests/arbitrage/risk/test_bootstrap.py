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
from src.arbitrage.common.venue_liveness import VenueExecutionLiveness
from src.arbitrage.common.params import ArbitrageParams
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


def test_install_replaces_node_kernel_binding():
    """#259:必须替 `live.node` 里的绑定 —— node 已在 import 时取走名字,替 kernel 模块无效。"""
    bootstrap.install_arbitrage_engines()
    import nautilus_trader.live.node as n
    assert n.NautilusKernel is bootstrap.ArbNautilusKernel


def test_arb_kernel_continues_startup_when_reconciliation_fails(monkeypatch):
    """#259:对账失败不再中止启动(否则 `trader.start()` 永不执行,全 actor 卡 READY)。

    失败的 venue 由 VenueExecutionLiveness 隔离(Risk 拒整个机会),并在下一轮连续对账成功后自愈。
    """
    logged = []
    kernel = bootstrap.ArbNautilusKernel.__new__(bootstrap.ArbNautilusKernel)
    kernel._log = SimpleNamespace(warning=logged.append)

    async def failing_super(self):
        return False

    monkeypatch.setattr(
        bootstrap._kernel.NautilusKernel,
        "_await_execution_reconciliation",
        failing_super,
    )

    assert asyncio.run(kernel._await_execution_reconciliation()) is True
    assert any("continuing startup" in msg for msg in logged)


def test_arb_kernel_passes_through_successful_reconciliation(monkeypatch):
    """成功路径不加噪声:返 True 且不打 warning。"""
    logged = []
    kernel = bootstrap.ArbNautilusKernel.__new__(bootstrap.ArbNautilusKernel)
    kernel._log = SimpleNamespace(warning=logged.append)

    async def ok_super(self):
        return True

    monkeypatch.setattr(
        bootstrap._kernel.NautilusKernel,
        "_await_execution_reconciliation",
        ok_super,
    )

    assert asyncio.run(kernel._await_execution_reconciliation()) is True
    assert logged == []


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


def test_wire_injects_params_and_returns_shared_liveness():
    node = _arb_node()
    liveness = VenueExecutionLiveness()
    params = ArbRiskParams(match_tp=0.07)
    arbitrage_params = ArbitrageParams(share=200.0, fx=1.3)

    returned = bootstrap.wire_arbitrage_runtime(
        node,
        params=params,
        arbitrage_params=arbitrage_params,
        venue_liveness=liveness,
    )

    assert returned is liveness                          # 返回同一份供 execution/risk 复用
    assert node.kernel.portfolio._share == 200.0
    assert node.kernel.risk_engine._params.match_tp == 0.07
    assert node.kernel.risk_engine._arb_venue_liveness is liveness


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
    assert bootstrap.get_arb_context().venue_liveness is None
    liveness = VenueExecutionLiveness()
    ctx = bootstrap.prepare_arb_context(
        venue_liveness=liveness,
        session_timeout_secs_by_venue={"POLYMARKET": 8.0},
    )
    assert ctx.venue_liveness is liveness
    assert bootstrap.get_arb_context() is ctx                # 进程级单例
    assert ctx.session_timeout_secs_by_venue["POLYMARKET"] == 8.0
    bootstrap.reset_arb_context()
    assert bootstrap.get_arb_context().venue_liveness is None


def test_ctx_map_get_set():
    bootstrap.reset_arb_context()
    ctx = bootstrap.get_arb_context()

    assert bootstrap.ctx_map_get(ctx, "session_timeout_secs_by_venue", "POLYMARKET", 30.0) == 30.0
    with pytest.raises(RuntimeError, match=r"session_timeout_secs_by_venue\['POLYMARKET'\] is required"):
        bootstrap.ctx_map_require(ctx, "session_timeout_secs_by_venue", "POLYMARKET")

    bootstrap.ctx_map_set(ctx, "session_timeout_secs_by_venue", "POLYMARKET", 45.0)

    assert bootstrap.ctx_map_get(ctx, "session_timeout_secs_by_venue", "POLYMARKET", 30.0) == 45.0
    assert bootstrap.ctx_map_require(ctx, "session_timeout_secs_by_venue", "POLYMARKET") == 45.0
    bootstrap.reset_arb_context()


def test_ctx_map_get_or_create_writes_once():
    bootstrap.reset_arb_context()
    ctx = bootstrap.get_arb_context()
    calls = []

    first = bootstrap.ctx_map_get_or_create(
        ctx,
        "browser_manager_by_venue",
        "ORBITEXCH",
        lambda: calls.append("created") or object(),
    )
    second = bootstrap.ctx_map_get_or_create(
        ctx,
        "browser_manager_by_venue",
        "ORBITEXCH",
        lambda: calls.append("created-again") or object(),
    )

    assert first is second
    assert calls == ["created"]
    assert ctx.browser_manager_by_venue["ORBITEXCH"] is first
    bootstrap.reset_arb_context()


def test_prepare_arb_context_rejects_removed_venue_specific_fields():
    bootstrap.reset_arb_context()

    with pytest.raises(TypeError, match="pm_session_timeout_secs"):
        bootstrap.prepare_arb_context(pm_session_timeout_secs=8.0)


def test_wire_reuses_context_liveness_when_none_passed():
    # launcher 顺序:prepare_arb_context(liveness) → factory 用 liveness → wire 不传 → 用同份
    bootstrap.reset_arb_context()
    liveness = VenueExecutionLiveness()
    bootstrap.prepare_arb_context(venue_liveness=liveness)
    node = _arb_node()
    returned = bootstrap.wire_arbitrage_runtime(node)         # 不传 venue_liveness
    assert returned is liveness                               # 复用 context 那份(execution/risk 也用同份)
    assert node.kernel.risk_engine._arb_venue_liveness is liveness
    bootstrap.reset_arb_context()
