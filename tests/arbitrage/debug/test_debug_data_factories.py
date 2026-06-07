"""Q11.A:PM/OE DataClient factory 按 `ArbContext.debug_config` 分支。

对应用例:debug-bootstrap.*(数据层)。

策略:monkeypatch 工厂内引用的 NT 客户端类(production + debug)为 sentinel,
聚焦验证 factory **挑哪个类构造**;不真起 NT。
"""

from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.orbitexch import factories as oe_factories
from nautilus_trader.adapters.polymarket import arb_factories as pm_factories
from src.arbitrage.debug import data_clients as debug_dc
from src.arbitrage.debug.config import DebugConfig


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


def _common_nt_args():
    return dict(
        loop=MagicMock(),
        name="X",
        config=MagicMock(),
        msgbus=MagicMock(),
        cache=MagicMock(),
        clock=MagicMock(),
    )


# ── PM ──────────────────────────────────────────────────────

def test_pm_factory_uses_production_when_no_debug(monkeypatch):
    prod, dbg = MagicMock(name="pm_prod"), MagicMock(name="pm_debug")
    monkeypatch.setattr(pm_factories, "PolymarketDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugPolymarketDataClient", dbg)
    monkeypatch.setattr(pm_factories, "get_polymarket_http_client", MagicMock())
    monkeypatch.setattr(pm_factories, "ArbPolymarketInstrumentProvider", MagicMock())

    bootstrap.prepare_arb_context()  # debug_config=None
    pm_factories.ArbPolymarketLiveDataClientFactory.create(**_common_nt_args())

    assert prod.called and not dbg.called


def test_pm_factory_uses_production_when_debug_disabled(monkeypatch):
    prod, dbg = MagicMock(name="pm_prod"), MagicMock(name="pm_debug")
    monkeypatch.setattr(pm_factories, "PolymarketDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugPolymarketDataClient", dbg)
    monkeypatch.setattr(pm_factories, "get_polymarket_http_client", MagicMock())
    monkeypatch.setattr(pm_factories, "ArbPolymarketInstrumentProvider", MagicMock())

    bootstrap.prepare_arb_context(debug_config=DebugConfig(enabled=False))
    pm_factories.ArbPolymarketLiveDataClientFactory.create(**_common_nt_args())

    assert prod.called and not dbg.called


def test_pm_factory_uses_debug_when_enabled(monkeypatch):
    prod, dbg = MagicMock(name="pm_prod"), MagicMock(name="pm_debug")
    monkeypatch.setattr(pm_factories, "PolymarketDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugPolymarketDataClient", dbg)
    monkeypatch.setattr(pm_factories, "get_polymarket_http_client", MagicMock())
    monkeypatch.setattr(pm_factories, "ArbPolymarketInstrumentProvider", MagicMock())

    cfg = DebugConfig(enabled=True)
    bootstrap.prepare_arb_context(debug_config=cfg)
    pm_factories.ArbPolymarketLiveDataClientFactory.create(**_common_nt_args())

    assert dbg.called and not prod.called
    # debug=cfg 被传进去
    assert dbg.call_args.kwargs["debug"] is cfg


# ── OE ──────────────────────────────────────────────────────

def _oe_args():
    a = _common_nt_args()
    a.pop("name")  # OE factory create 不接 name
    return a


def test_oe_factory_uses_production_when_no_debug(monkeypatch):
    prod, dbg = MagicMock(name="oe_prod"), MagicMock(name="oe_debug")
    monkeypatch.setattr(oe_factories, "OrbitExchDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugOrbitExchDataClient", dbg)
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())

    bootstrap.prepare_arb_context()
    oe_factories.OrbitExchLiveDataClientFactory.create(name="OE", **_oe_args())

    assert prod.called and not dbg.called


def test_oe_factory_uses_production_when_debug_disabled(monkeypatch):
    prod, dbg = MagicMock(name="oe_prod"), MagicMock(name="oe_debug")
    monkeypatch.setattr(oe_factories, "OrbitExchDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugOrbitExchDataClient", dbg)
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())

    bootstrap.prepare_arb_context(debug_config=DebugConfig(enabled=False))
    oe_factories.OrbitExchLiveDataClientFactory.create(name="OE", **_oe_args())

    assert prod.called and not dbg.called


def test_oe_factory_uses_debug_when_enabled(monkeypatch):
    prod, dbg = MagicMock(name="oe_prod"), MagicMock(name="oe_debug")
    monkeypatch.setattr(oe_factories, "OrbitExchDataClient", prod)
    monkeypatch.setattr(debug_dc, "DebugOrbitExchDataClient", dbg)
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())

    cfg = DebugConfig(enabled=True)
    bootstrap.prepare_arb_context(debug_config=cfg)
    oe_factories.OrbitExchLiveDataClientFactory.create(name="OE", **_oe_args())

    assert dbg.called and not prod.called
    assert dbg.call_args.kwargs["debug"] is cfg
