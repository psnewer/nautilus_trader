"""Q11.3 PM/OE ExecutionClient factory 按 `ArbContext.debug_config` 分支。"""

from unittest.mock import MagicMock

import pytest

import src.arbitrage.bootstrap as bootstrap
from nautilus_trader.adapters.orbitexch import factories as oe_factories
from nautilus_trader.adapters.polymarket import arb_factories as pm_factories
from src.arbitrage.common.venues import ORBITEXCH
from src.arbitrage.common.venues import POLYMARKET
from src.arbitrage.debug import execution_clients as debug_exec
from src.arbitrage.debug.config import DebugConfig


@pytest.fixture(autouse=True)
def _reset_ctx():
    bootstrap.reset_arb_context()
    yield
    bootstrap.reset_arb_context()


def _pm_args():
    return dict(
        loop=MagicMock(), name="PM", config=MagicMock(),
        msgbus=MagicMock(), cache=MagicMock(), clock=MagicMock(),
    )


def _oe_args():
    return dict(
        loop=MagicMock(), name="OE", config=MagicMock(),
        msgbus=MagicMock(), cache=MagicMock(), clock=MagicMock(),
    )


# ── PM ──────────────────────────────────────────────────────

def _stub_pm(monkeypatch):
    monkeypatch.setattr(pm_factories, "get_polymarket_http_client", MagicMock())
    monkeypatch.setattr(pm_factories, "get_polymarket_instrument_provider", MagicMock())
    monkeypatch.setattr(pm_factories, "PolymarketWebSocketAuth", MagicMock())
    monkeypatch.setattr(pm_factories, "get_polymarket_api_key", MagicMock())
    monkeypatch.setattr(pm_factories, "get_polymarket_api_secret", MagicMock())
    monkeypatch.setattr(pm_factories, "get_polymarket_passphrase", MagicMock())


def test_pm_exec_factory_uses_production_when_no_debug(monkeypatch):
    prod, dbg = MagicMock(name="pm_prod"), MagicMock(name="pm_skip")
    _stub_pm(monkeypatch)
    monkeypatch.setattr(pm_factories, "ArbPolymarketExecutionClient", prod)
    monkeypatch.setattr(debug_exec, "SkipExecutionPolymarketClient", dbg)

    bootstrap.prepare_arb_context(
        venue_liveness=MagicMock(),
        session_timeout_secs_by_venue={POLYMARKET: 45.0},
        market_order_enabled=True,
    )
    pm_factories.ArbPolymarketLiveExecClientFactory.create(**_pm_args())

    assert prod.called and not dbg.called
    assert prod.call_args.kwargs["session_timeout_secs"] == 45.0
    assert prod.call_args.kwargs["market_order_enabled"] is True


def test_pm_exec_factory_requires_session_timeout_keyed_value(monkeypatch):
    _stub_pm(monkeypatch)
    monkeypatch.setattr(pm_factories, "ArbPolymarketExecutionClient", MagicMock())

    bootstrap.prepare_arb_context(venue_liveness=MagicMock())

    with pytest.raises(RuntimeError, match=r"session_timeout_secs_by_venue\['POLYMARKET'\] is required"):
        pm_factories.ArbPolymarketLiveExecClientFactory.create(**_pm_args())


def test_pm_exec_factory_uses_skip_when_debug_enabled(monkeypatch):
    prod, dbg = MagicMock(name="pm_prod"), MagicMock(name="pm_skip")
    _stub_pm(monkeypatch)
    monkeypatch.setattr(pm_factories, "ArbPolymarketExecutionClient", prod)
    monkeypatch.setattr(debug_exec, "SkipExecutionPolymarketClient", dbg)

    cfg = DebugConfig(enabled=True)
    bootstrap.prepare_arb_context(
        venue_liveness=MagicMock(),
        debug_config=cfg,
        session_timeout_secs_by_venue={POLYMARKET: 45.0},
    )
    pm_factories.ArbPolymarketLiveExecClientFactory.create(**_pm_args())

    assert dbg.called and not prod.called
    assert dbg.call_args.kwargs["debug"] is cfg


# ── OE ──────────────────────────────────────────────────────

def test_oe_exec_factory_uses_production_when_no_debug(monkeypatch):
    prod, dbg = MagicMock(name="oe_prod"), MagicMock(name="oe_skip")
    monkeypatch.setattr(oe_factories, "OrbitExchExecutionClient", prod)
    monkeypatch.setattr(debug_exec, "SkipExecutionOrbitExchClient", dbg)
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())

    bootstrap.prepare_arb_context(
        venue_liveness=MagicMock(),
        session_timeout_secs_by_venue={ORBITEXCH: 45.0},
        market_order_enabled=True,
    )
    oe_factories.ArbOrbitExchLiveExecClientFactory.create(**_oe_args())

    assert prod.called and not dbg.called
    assert prod.call_args.kwargs["session_timeout_secs"] == 45.0
    assert prod.call_args.kwargs["market_order_enabled"] is True


def test_oe_exec_factory_uses_skip_when_debug_enabled(monkeypatch):
    prod, dbg = MagicMock(name="oe_prod"), MagicMock(name="oe_skip")
    monkeypatch.setattr(oe_factories, "OrbitExchExecutionClient", prod)
    monkeypatch.setattr(debug_exec, "SkipExecutionOrbitExchClient", dbg)
    monkeypatch.setattr(oe_factories, "PlaywrightBrowserManager", MagicMock())

    cfg = DebugConfig(enabled=True)
    bootstrap.prepare_arb_context(
        venue_liveness=MagicMock(),
        debug_config=cfg,
        session_timeout_secs_by_venue={ORBITEXCH: 45.0},
    )
    oe_factories.ArbOrbitExchLiveExecClientFactory.create(**_oe_args())

    assert dbg.called and not prod.called
    assert dbg.call_args.kwargs["debug"] is cfg
