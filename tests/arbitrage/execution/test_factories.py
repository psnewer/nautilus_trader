"""Arb 执行客户端 factory —— ArbContext 注入通道 + 自定义 factory create。

PM factory.create 需要 PM 链上 creds(私钥等)→ 跑全调用要 /live-test;此处只测**没填
ArbContext 时的早失败保护**(launcher 漏调 prepare_arb_context 不让 factory 静默用 None)。
OE factory.create 离线可走通(PlaywrightBrowserManager 构造不真启浏览器)。
"""

import asyncio

import pytest

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from nautilus_trader.adapters.orbitexch.config import OrbitExchExecClientConfig

from src.arbitrage.bootstrap import ArbContext
from src.arbitrage.bootstrap import prepare_arb_context
from src.arbitrage.bootstrap import reset_arb_context
from src.arbitrage.common.leg_settled import LegSettledRegistry
from nautilus_trader.adapters.orbitexch.execution import OrbitExchExecutionClient
from nautilus_trader.adapters.orbitexch.factories import ArbOrbitExchLiveExecClientFactory
from nautilus_trader.adapters.polymarket.arb_factories import ArbPolymarketLiveExecClientFactory


@pytest.fixture(autouse=True)
def _clear_ctx():
    reset_arb_context()
    yield
    reset_arb_context()


def _harness():
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = TestComponentStubs.cache()
    return asyncio.new_event_loop(), msgbus, cache, clock


# ── 早失败保护(launcher 漏调 prepare_arb_context)──────────────────
def test_pm_factory_raises_when_context_unset():
    loop, msgbus, cache, clock = _harness()
    # 用最小 PolymarketExecClientConfig 占位 —— 早失败发生在读 ctx,在构造 http_client 之前
    cfg = _dummy_pm_config()
    with pytest.raises(RuntimeError, match="prepare_arb_context"):
        ArbPolymarketLiveExecClientFactory.create(loop, "POLYMARKET", cfg, msgbus, cache, clock)


def test_oe_factory_raises_when_context_unset():
    loop, msgbus, cache, clock = _harness()
    cfg = OrbitExchExecClientConfig(username="u", password="p")
    with pytest.raises(RuntimeError, match="prepare_arb_context"):
        ArbOrbitExchLiveExecClientFactory.create(loop, "ORBITEXCH", cfg, msgbus, cache, clock)


# ── OE factory 全程走通(stub 上下文)──────────────────────────────
def test_oe_factory_create_with_context_returns_arb_client():
    loop, msgbus, cache, clock = _harness()
    registry = LegSettledRegistry()
    prepare_arb_context(leg_settled=registry, oe_session_timeout_secs=45.0)
    cfg = OrbitExchExecClientConfig(username="u", password="p")

    client = ArbOrbitExchLiveExecClientFactory.create(loop, "ORBITEXCH", cfg, msgbus, cache, clock)

    assert isinstance(client, OrbitExchExecutionClient)
    assert client._leg_settled is registry                # 同份 registry 跨组件共享
    assert client._session_timeout_ns == int(45.0 * 1e9)  # context 传值生效
    assert client._browser_manager is not None
    assert client._config is cfg


def _dummy_pm_config():
    """构造一个最小 PolymarketExecClientConfig 用于"早失败"测试 —— 不会走到读它的字段。"""
    from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
    return PolymarketExecClientConfig(
        private_key="0x" + "0" * 64,
        api_key="k", api_secret="s", passphrase="p", funder="0x" + "0" * 40,
    )
